"""采集节点业务规则：状态流转、容量口径与调度侧消费都收在这里。

调度侧从这里读取：
- 可调度节点结果（含剩余槽位），由节点状态、心跳新鲜度与容量共同决定；
- 容量释放结果，逐条消费并回写剩余槽位，保证槽位只归还一次。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.timeutil import iso, seconds_since

MODULE = "worker"
REQUIRED_FIELDS = ["节点编号", "主机地址", "并发槽位数"]
STATUS_ORDER = ["草稿", "注册中", "在线", "离线"]
ACTION_RULES = {"提交注册": "注册中", "标记在线": "在线", "标记离线": "离线"}
NEGATIVE_ACTIONS = ["标记离线"]

# 心跳超过该秒数视为不新鲜：即便状态仍是「在线」也不参与派发。
HEARTBEAT_TTL_SECONDS = 120
RELEASE_MODULE = "capacity_release"
ATTEMPT_MODULE = "render_attempt"
MANIFEST_MODULE = "dispatch_manifest"


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class WorkerService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("节点编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["并发槽位数"] = _int(values.get("并发槽位数"), 0)
        entry["已占用槽位"] = 0
        entry["剩余槽位"] = _int(values.get("并发槽位数"), 0)
        entry["上次心跳时间"] = None
        entry["节点状态标签"] = "刚刚登记"
        entry["可调度"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"工作节点 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集节点可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        with store.lock:
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            entry["上次心跳时间"] = iso()
            if target == "在线":
                entry["节点状态标签"] = "心跳正常"
                entry["可调度"] = True
            elif target == "离线":
                entry["节点状态标签"] = "节点离线"
                entry["可调度"] = False
                # 离线节点不再承接新派发，进行中的渲染统一结算，避免槽位悬挂。
                from app.services.renderledger import settle_node_offline

                settled = settle_node_offline(str(entry.get("节点编号") or ""))
                if settled:
                    return entry, f"工作节点已{action}，并结算 {len(settled)} 个渲染尝试"
            else:
                entry["节点状态标签"] = "注册未上线"
                entry["可调度"] = False
        return entry, f"工作节点已{action}"

    # ---- 调度侧读口 ----

    def schedulable_nodes(self) -> list[dict[str, Any]]:
        """可调度节点结果：状态在线 + 心跳新鲜 + 剩余槽位大于 0。"""
        nodes: list[dict[str, Any]] = []
        for row in store.rows(MODULE):
            slots = _int(row.get("并发槽位数"))
            occupied = _int(row.get("已占用槽位"))
            age = seconds_since(row.get("上次心跳时间"))
            fresh = age is not None and age <= HEARTBEAT_TTL_SECONDS
            online = row.get("status") == "在线"
            remaining = max(slots - occupied, 0)
            schedulable = bool(online and fresh and remaining > 0)
            row["剩余槽位"] = remaining
            row["可调度"] = schedulable
            if not online:
                reason = f"节点状态为{row.get('status')}"
            elif not fresh:
                reason = "心跳过期" if age is not None else "尚无心跳"
            elif remaining <= 0:
                reason = "没有剩余槽位"
            else:
                reason = "可派发"
            nodes.append(
                {
                    "节点标识": row.get("节点编号"),
                    "status": row.get("status"),
                    "并发槽位数": slots,
                    "已占用槽位": occupied,
                    "剩余槽位": remaining,
                    "心跳秒": age,
                    "心跳新鲜": fresh,
                    "可调度": schedulable,
                    "判定原因": reason,
                }
            )
        return nodes

    def consume_capacity_releases(self) -> dict[str, Any]:
        """读取容量释放结果并回写剩余槽位；同一条释放只生效一次。"""
        consumed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        with store.lock:
            for release in store.rows(RELEASE_MODULE):
                if release.get("已消费"):
                    skipped.append(
                        {
                            "释放编号": release.get("id"),
                            "作业编号": release.get("作业编号"),
                            "尝试版本": release.get("尝试版本"),
                            "原因": "已消费，不重复归还槽位",
                        }
                    )
                    continue
                node_code = str(release.get("节点标识") or "")
                worker = next(
                    (row for row in store.rows(MODULE) if row.get("节点编号") == node_code),
                    None,
                )
                if worker is None:
                    skipped.append(
                        {
                            "释放编号": release.get("id"),
                            "作业编号": release.get("作业编号"),
                            "尝试版本": release.get("尝试版本"),
                            "原因": f"节点 {node_code} 不在册，槽位无需归还",
                        }
                    )
                    continue
                slots = _int(worker.get("并发槽位数"))
                occupied = max(_int(worker.get("已占用槽位")) - _int(release.get("释放槽位"), 1), 0)
                release["已消费"] = True
                release["消费时间"] = iso()
                worker["已占用槽位"] = occupied
                worker["剩余槽位"] = max(slots - occupied, 0)
                consumed.append(
                    {
                        "释放编号": release.get("id"),
                        "作业编号": release.get("作业编号"),
                        "尝试版本": release.get("尝试版本"),
                        "节点标识": node_code,
                        "释放槽位": _int(release.get("释放槽位"), 1),
                        "已占用槽位": worker["已占用槽位"],
                        "剩余槽位": worker["剩余槽位"],
                        "释放原因": release.get("释放原因"),
                    }
                )
        return {
            "ok": True,
            "message": f"消费 {len(consumed)} 条容量释放" if consumed else "没有待消费的容量释放",
            "消费条数": len(consumed),
            "跳过条数": len(skipped),
            "items": consumed,
            "skipped": skipped,
            "nodes": self.schedulable_nodes(),
        }

    def mark_manifest(self, *, result_version: int, release_ids: list[int], result_count: int, release_count: int) -> dict[str, Any]:
        """把结果版本与消费进度写进调度侧清单，作为「结果已被消费」的凭据。"""
        rows = store.rows(MANIFEST_MODULE)
        if not rows:
            rows.append({"id": 1, "清单版本": 0})
        manifest = rows[-1]
        nodes = self.schedulable_nodes()
        manifest["清单版本"] = _int(manifest.get("清单版本")) + 1
        manifest["已消费结果版本"] = max(_int(manifest.get("已消费结果版本")), result_version)
        # 释放编号取「本清单历史水位」与「本次实际消费」的较大者：
        # 若释放已被节点侧单独消费，清单水位也不能倒退。
        consumed_ids = [release_id for release_id in release_ids if release_id]
        consumed_ids += [_int(row.get("id")) for row in store.rows(RELEASE_MODULE) if row.get("已消费")]
        manifest["已消费释放编号"] = max(
            _int(manifest.get("已消费释放编号")),
            max(consumed_ids, default=0),
        )
        manifest["消费结果条数"] = _int(manifest.get("消费结果条数")) + result_count
        manifest["消费释放条数"] = _int(manifest.get("消费释放条数")) + release_count
        manifest["同步时间"] = iso()
        manifest["可调度节点数"] = sum(1 for node in nodes if node["可调度"])
        manifest["剩余槽位合计"] = sum(node["剩余槽位"] for node in nodes if node["可调度"])
        return manifest

    def manifest(self) -> dict[str, Any]:
        rows = store.rows(MANIFEST_MODULE)
        manifest = dict(rows[-1]) if rows else {}
        manifest["节点容量"] = self.schedulable_nodes()
        return manifest

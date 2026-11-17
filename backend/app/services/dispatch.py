"""请求分配业务规则：状态流转、派发解释与调度侧结果消费都收在这里。

节点调度侧在这里做三件事：
1. 读取容量释放结果并把剩余槽位更新进可调度节点结果；
2. 把新的渲染结果版本同步进调度清单，确认结果已被消费；
3. 把同步结果落到一条调度业务记录上，便于按清单版本回溯。
"""
from __future__ import annotations

from typing import Any

from app.services import renderledger
from app.services.worker import WorkerService
from app.store import store
from app.timeutil import iso, seconds_since

MODULE = "dispatch"
REQUIRED_FIELDS = ["策略编号", "目标域名", "分配算法"]
STATUS_ORDER = ["草稿", "策略验证中", "已启用", "已停用"]
ACTION_RULES = {"提交策略验证": "策略验证中", "启用策略": "已启用", "停用策略": "已停用"}
NEGATIVE_ACTIONS = ["停用策略"]

RESULT_MODULE = "render_result"
RELEASE_MODULE = "capacity_release"
RECORD_MODULE = "dispatch_record"
MANIFEST_MODULE = "dispatch_manifest"

# 重排扣减：同一作业每被重排一次，派发得分扣 60 秒，避免长期占住队首。
REQUEUE_PENALTY_SECONDS = 60
# 尝试次数扣减：反复超时说明页面本身不稳，往后排。
ATTEMPT_PENALTY_SECONDS = 20


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class DispatchService:
    def __init__(self) -> None:
        self.worker_service = WorkerService()

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
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
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
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分配策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于请求分配可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"分配策略已{action}"

    # ---- 派发解释 ----

    def plan(self) -> dict[str, Any]:
        """下一次派发：排队公平性、等待时长与节点新鲜度共同解释。"""
        nodes = sorted(
            self.worker_service.schedulable_nodes(),
            key=lambda node: (not node["可调度"], node["心跳秒"] if node["心跳秒"] is not None else 10**9),
        )
        candidates: list[dict[str, Any]] = []
        with store.lock:
            for job in store.rows("rendertask"):
                if job.get("status") != "排队中":
                    continue
                attempt = renderledger.current_attempt(job)
                if attempt is None or attempt.get("status") != "排队中":
                    continue
                waited = seconds_since(attempt.get("入队时间")) or 0
                requeue = _int(job.get("重排次数"))
                attempts = _int(job.get("尝试次数"))
                score = waited - requeue * REQUEUE_PENALTY_SECONDS - max(attempts - 1, 0) * ATTEMPT_PENALTY_SECONDS
                threshold = renderledger.effective_timeout(job, attempt)
                candidates.append(
                    {
                        "作业id": _int(job.get("id")),
                        "作业编号": job.get("作业编号"),
                        "当前尝试版本": _int(job.get("当前尝试版本")),
                        "已等待秒": waited,
                        "重排次数": requeue,
                        "尝试次数": attempts,
                        "派发得分": score,
                        "生效阈值秒": threshold,
                        "剩余秒": max(threshold - waited, 0),
                        "等待策略": attempt.get("等待策略"),
                        "排队公平性": (
                            f"等待 {waited}s，重排 {requeue} 次扣 {requeue * REQUEUE_PENALTY_SECONDS}s，"
                            f"历史尝试 {attempts} 次扣 {max(attempts - 1, 0) * ATTEMPT_PENALTY_SECONDS}s，"
                            f"得分 {score}"
                        ),
                    }
                )
        candidates.sort(key=lambda item: (-item["派发得分"], item["作业id"]))
        target_node = next((node for node in nodes if node["可调度"]), None)
        head = candidates[0] if candidates else None
        if head is None:
            decision = "当前没有排队中的渲染作业，无需派发"
        elif target_node is None:
            decision = "有排队作业但没有可调度节点：节点需在线、心跳新鲜且剩余槽位大于 0"
        else:
            decision = (
                f"下一次派发 {head['作业编号']}（尝试 v{head['当前尝试版本']}）到 {target_node['节点标识']}："
                f"{head['排队公平性']}；节点心跳 {target_node['心跳秒']}s、剩余槽位 {target_node['剩余槽位']}"
            )
        return {
            "ok": True,
            "message": decision,
            "decision": decision,
            "candidates": candidates,
            "nodes": nodes,
            "可派发": head is not None and target_node is not None,
            "建议作业": head["作业编号"] if head else None,
            "建议节点": target_node["节点标识"] if target_node else None,
        }

    # ---- 调度侧消费 ----

    def sync(self) -> dict[str, Any]:
        """消费容量释放与新的渲染结果，并同步进调度清单与业务记录。"""
        with store.lock:
            release_report = self.worker_service.consume_capacity_releases()
            manifest_rows = store.rows(MANIFEST_MODULE)
            if not manifest_rows:
                manifest_rows.append({"id": 1, "清单版本": 0, "已消费结果版本": 0})
            manifest = manifest_rows[-1]
            consumed_version = _int(manifest.get("已消费结果版本"))

            fresh_results = [
                row
                for row in store.rows(RESULT_MODULE)
                if not row.get("调度消费") and _int(row.get("结果版本")) > consumed_version
            ]
            fresh_results.sort(key=lambda row: _int(row.get("结果版本")))
            # 结果版本是全局递增的，直接取水位可避免中间版本被别处消费后漏读。
            consumed_version = max(
                [consumed_version] + [_int(row.get("结果版本")) for row in fresh_results]
            )
            if not fresh_results and not release_report["items"]:
                # 没有新结果也没有待消费的释放：不推进清单版本，重复同步保持幂等；
                # 但仍刷新释放水位与节点容量，保证清单与节点侧消费结果一致。
                consumed_release_ids = [
                    _int(row.get("id")) for row in store.rows(RELEASE_MODULE) if row.get("已消费")
                ]
                manifest["已消费释放编号"] = max(
                    _int(manifest.get("已消费释放编号")),
                    max(consumed_release_ids, default=0),
                )
                nodes = self.worker_service.schedulable_nodes()
                manifest["可调度节点数"] = sum(1 for node in nodes if node["可调度"])
                manifest["剩余槽位合计"] = sum(node["剩余槽位"] for node in nodes if node["可调度"])
                return {
                    "ok": True,
                    "message": "没有待同步的渲染结果或容量释放，清单保持不变",
                    "manifest": dict(manifest),
                    "record": None,
                    "results": [],
                    "releases": release_report,
                    "nodes": nodes,
                }
            next_version = _int(manifest.get("清单版本")) + 1
            consumed_at = iso()
            effective_versions: list[int] = []
            for row in fresh_results:
                row["调度消费"] = True
                row["消费清单版本"] = next_version
                row["消费时间"] = consumed_at
                if row.get("生效"):
                    effective_versions.append(_int(row.get("结果版本")))

            release_ids = [item["释放编号"] for item in release_report["items"]]
            result_version_max = max(
                [consumed_version] + [_int(row.get("结果版本")) for row in fresh_results],
                default=consumed_version,
            )
            new_manifest = self.worker_service.mark_manifest(
                result_version=result_version_max,
                release_ids=release_ids,
                result_count=len(fresh_results),
                release_count=len(release_report["items"]),
            )
            nodes = self.worker_service.schedulable_nodes()
            record = {
                "id": max((_int(row.get("id")) for row in store.rows(RECORD_MODULE)), default=0) + 1,
                "清单版本": _int(new_manifest.get("清单版本")),
                "已消费结果版本": _int(new_manifest.get("已消费结果版本")),
                "消费结果条数": len(fresh_results),
                "消费释放条数": len(release_report["items"]),
                "跳过释放条数": len(release_report["skipped"]),
                "生效结果版本": effective_versions,
                "可调度节点数": sum(1 for node in nodes if node["可调度"]),
                "剩余槽位合计": sum(node["剩余槽位"] for node in nodes if node["可调度"]),
                "同步时间": consumed_at,
                "说明": (
                    f"消费结果 {len(fresh_results)} 条、容量释放 {len(release_report['items'])} 条，"
                    f"清单版本 {_int(new_manifest.get('清单版本'))}"
                ),
            }
            store.rows(RECORD_MODULE).append(record)
        return {
            "ok": True,
            "message": record["说明"],
            "manifest": new_manifest,
            "record": record,
            "results": [
                {
                    "结果版本": _int(row.get("结果版本")),
                    "作业编号": row.get("作业编号"),
                    "尝试版本": _int(row.get("尝试版本")),
                    "结果状态": row.get("结果状态"),
                    "生效": bool(row.get("生效")),
                    "调度消费": bool(row.get("调度消费")),
                    "消费清单版本": row.get("消费清单版本"),
                }
                for row in fresh_results
            ],
            "releases": release_report,
            "nodes": nodes,
        }

    def records(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = sorted(store.rows(RECORD_MODULE), key=lambda row: _int(row.get("id")), reverse=True)
        return rows[:limit]

    def manifest(self) -> dict[str, Any]:
        return self.worker_service.manifest()

"""渲染任务业务规则：状态流转、结果新鲜度、超时回收与队列口径都收在这里。

状态单向流转：草稿 → 排队中 → 渲染中 → 已完成 / 已超时 / 已失败 → 归档。
任何一次结算都会落一份结果；旧尝试的结果只归档，不覆盖当前尝试。
"""
from __future__ import annotations

from typing import Any

from app.services import renderledger
from app.services.worker import WorkerService
from app.store import store
from app.timeutil import iso
from app.policies import normalize_timeout

MODULE = "rendertask"
REQUIRED_FIELDS = ["作业编号", "目标URL", "浏览器版本"]
STATUS_ORDER = ["草稿", "排队中", "渲染中", "已完成", "已超时", "已失败", "已归档"]
ACTION_RULES = {
    "提交排队": "排队中",
    "启动渲染": "渲染中",
    "终止渲染": "已超时",
    "重新排队": "排队中",
    "提交渲染结果": "已完成",
}
NEGATIVE_ACTIONS = ["终止渲染"]
# 允许从这些状态重新进入排队；草稿是首次排队，终态才是重排。
REQUEUE_FROM = {"已超时", "已失败", "已完成"}
ARCHIVE_FROM = {"已完成", "已超时", "已失败"}
DEFAULT_WAIT_STRATEGY = "load"
DEFAULT_MAX_REQUEUE = 2


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class RendertaskService:
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
            rows = [row for row in rows if keyword in str(row.get("作业编号", ""))]
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
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["视口宽度"] = values.get("视口宽度") or "1440"
            entry["视口高度"] = values.get("视口高度") or "900"
            entry["等待策略"] = str(values.get("等待策略") or DEFAULT_WAIT_STRATEGY)
            entry["脚本注入"] = values.get("脚本注入") or ""
            entry["超时阈值秒"] = normalize_timeout(values.get("超时阈值秒"))
            entry["最大重排次数"] = _int(values.get("最大重排次数"), DEFAULT_MAX_REQUEUE)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["当前尝试版本"] = 0
            entry["尝试次数"] = 0
            entry["重排次数"] = 0
            entry["排队时间"] = None
            entry["分配节点"] = ""
            entry["生效结果版本"] = None
            entry["生效尝试版本"] = None
            entry["最近结局时间"] = None
            entry["最近结局原因"] = ""
            rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"渲染作业 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于渲染任务可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "启动渲染":
            return self._start(entry, values)
        if action == "终止渲染":
            return self._terminate(entry)
        if action == "提交渲染结果":
            return self._complete(entry, values)
        if action == "重新排队":
            return self._requeue(entry, values)
        return self._enqueue(entry, values)

    # ---- 动作实现 ----

    def _enqueue(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            status = str(entry.get("status"))
            attempt = renderledger.current_attempt(entry)
            if status in {"排队中", "渲染中"} and attempt is not None:
                return entry, f"渲染作业 {entry.get('作业编号')} 已在{status}，不重复入队"
            if status == "已归档":
                return None, f"渲染作业 {entry.get('作业编号')} 已归档，不能再次排队"
            if status in REQUEUE_FROM:
                attempt = renderledger.requeue_job(entry, reason="人工重新排队")
                return entry, (
                    f"渲染作业 {entry.get('作业编号')} 已重新排队（尝试 v{attempt['尝试版本']}，"
                    f"第 {entry.get('重排次数')} 次重排）"
                )
            attempt = renderledger.start_job_queue(entry)
            return entry, f"渲染作业 {entry.get('作业编号')} 已排队（尝试 v{attempt['尝试版本']}）"

    def _start(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        requested = str(values.get("节点标识") or "").strip()
        with store.lock:
            # 可调度口径统一由节点服务给出：状态在线 + 心跳新鲜 + 剩余槽位大于 0。
            nodes = self.worker_service.schedulable_nodes()
            available = next(
                (node for node in nodes if node["节点标识"] == requested and node["可调度"]),
                None,
            ) if requested else None
            if requested and available is None:
                node = next((item for item in nodes if item["节点标识"] == requested), None)
                reason = node["判定原因"] if node else "节点不在册"
                return None, f"节点 {requested} 不可承接渲染作业：{reason}"
            if available is None:
                available = next((node for node in nodes if node["可调度"]), None)
            if available is None:
                return None, "当前没有可调度节点（需在线、心跳新鲜且剩余槽位大于 0）"
            node_code = str(available["节点标识"])
            attempt, message = renderledger.start_job(entry, node=node_code)
            if attempt is None:
                return None, message
            worker = next(
                (row for row in store.rows("worker") if row.get("节点编号") == node_code),
                None,
            )
            if worker is not None:
                worker["已占用槽位"] = _int(worker.get("已占用槽位")) + 1
                worker["剩余槽位"] = max(_int(worker.get("并发槽位数")) - _int(worker.get("已占用槽位")), 0)
        return entry, message + f"，承接节点 {node_code}"

    def _terminate(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        result, message = renderledger.terminate_job(entry)
        if result is None:
            return None, message
        return entry, message

    def _complete(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        raw_version = values.get("尝试版本")
        attempt_version = _int(raw_version, 0) or None
        content = values.get("结果内容")
        content = content if isinstance(content, dict) else None
        idempotency_key = str(values.get("幂等键") or "").strip()
        result, _created, message = renderledger.record_completion(
            entry,
            attempt_version=attempt_version,
            content=content,
            idempotency_key=idempotency_key,
        )
        if result is None:
            return None, message
        return entry, message

    def _requeue(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            attempt = renderledger.current_attempt(entry)
            if attempt is not None and attempt.get("status") in renderledger.ACTIVE_STATUSES:
                return None, (
                    f"渲染作业 {entry.get('作业编号')} 当前尝试 v{attempt.get('尝试版本')} 仍在"
                    f"{attempt.get('status')}，请先终止或等待结算"
                )
            if str(entry.get("status")) == "已归档":
                return None, f"渲染作业 {entry.get('作业编号')} 已归档，不能重新排队"
            attempt = renderledger.requeue_job(entry, reason=str(values.get("原因") or "人工重排"))
            return entry, (
                f"渲染作业 {entry.get('作业编号')} 已重排为 v{attempt['尝试版本']}，"
                f"累计重排 {entry.get('重排次数')} 次"
            )

    # ---- 结果与核对 ----

    def results(self, job_id: int) -> list[dict[str, Any]]:
        rows = renderledger.results_of(job_id)
        for row in rows:
            row["输入核对"] = self.check_against_input(row)
        return rows

    def check_against_input(self, result: dict[str, Any]) -> dict[str, Any]:
        """把结果字段逐项对照输入快照，给出是否一致与差异明细。"""
        snapshot = result.get("输入快照") or {}
        checks: list[dict[str, Any]] = []
        for field in renderledger.RESULT_ECHO_FIELDS:
            expected = snapshot.get(field)
            actual = result.get(field)
            same = str(expected) == str(actual)
            checks.append(
                {
                    "字段": field,
                    "输入": expected,
                    "结果": actual,
                    "一致": same,
                }
            )
        mismatched = [item for item in checks if not item["一致"]]
        return {
            "ok": not mismatched,
            "checked": len(checks),
            "mismatched": [item["字段"] for item in mismatched],
            "items": checks,
        }

    def job_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        """作业视角：尝试链、结果链与当前生效结果，便于顺着调用链定位。"""
        job_id = _int(entry.get("id"))
        attempts = renderledger.attempts_of(job_id)
        results = renderledger.results_of(job_id)
        attempt = renderledger.current_attempt(entry)
        return {
            "作业": entry,
            "状态链": [status for status in STATUS_ORDER],
            "当前尝试": attempt,
            "尝试链": attempts,
            "结果链": results,
            "生效结果版本": entry.get("生效结果版本"),
            "生效尝试版本": entry.get("生效尝试版本"),
            "重排次数": _int(entry.get("重排次数")),
            "队列等待": {
                "排队时间": (attempt or {}).get("入队时间"),
                "生效阈值秒": renderledger.effective_timeout(entry, attempt),
                "等待策略": (attempt or {}).get("等待策略") or entry.get("等待策略"),
            },
            "核对": [self.check_against_input(row) for row in results],
        }

    def archive(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        status = str(entry.get("status"))
        if status not in ARCHIVE_FROM:
            return None, f"只有已结算的渲染作业才能归档，当前状态为{status}"
        if entry.get("pending"):
            return None, "渲染作业仍有待处理动作，不能归档"
        with store.lock:
            entry["status"] = "已归档"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["最近结局时间"] = iso()
            entry["最近结局原因"] = "归档"
        return entry, f"渲染作业 {entry.get('作业编号')} 已归档"

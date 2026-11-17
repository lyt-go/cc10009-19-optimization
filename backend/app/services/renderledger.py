"""渲染账本：尝试版本、渲染结果、超时回收与容量释放的唯一结算入口。

调用链上的三方（完成回调、超时回收、节点离线）都会走到这里：
- 同一尝试只允许结算一次，先到者留下结果，后来者拿到既有结果；
- 容量释放按尝试去重，重复终止不会反复归还槽位；
- 结果同时带「尝试版本」和全局递增的「结果版本」，旧尝试的回调只能归档为失效结果，
  不能覆盖重新排队后的新一轮渲染。
"""
from __future__ import annotations

from typing import Any

from app.policies import (
    DEFAULT_MAX_REQUEUE,
    RESULT_ECHO_FIELDS,
    normalize_timeout,
    to_int as _int,
    wait_grace,
)
from app.store import store
from app.timeutil import iso, now, parse, seconds_since

MODULE = "rendertask"
ATTEMPT_MODULE = "render_attempt"
RESULT_MODULE = "render_result"
RELEASE_MODULE = "capacity_release"

ACTIVE_STATUSES = {"排队中", "渲染中"}
TERMINAL_STATUSES = {"已完成", "已超时", "已失败"}
JOB_TERMINAL_STATUSES = TERMINAL_STATUSES | {"已归档"}


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((_int(row.get("id")) for row in rows), default=0) + 1


def effective_timeout(job: dict[str, Any], attempt: dict[str, Any] | None = None) -> int:
    """生效超时 = 渲染作业/尝试的超时阈值秒 + 等待策略宽限。"""
    base = _int((attempt or {}).get("超时阈值秒") or job.get("超时阈值秒"), 0)
    if base <= 0:
        base = normalize_timeout(job.get("超时阈值秒"))
    strategy = (attempt or {}).get("等待策略") or job.get("等待策略")
    grace = _int((attempt or {}).get("宽限秒"), wait_grace(strategy))
    return max(1, base) + max(0, grace)


def _job_rows() -> list[dict[str, Any]]:
    return store.rows(MODULE)


def _attempt_rows() -> list[dict[str, Any]]:
    return store.rows(ATTEMPT_MODULE)


def _result_rows() -> list[dict[str, Any]]:
    return store.rows(RESULT_MODULE)


def _release_rows() -> list[dict[str, Any]]:
    return store.rows(RELEASE_MODULE)


def find_job(job_id: int) -> dict[str, Any] | None:
    return store.find(MODULE, job_id)


def attempts_of(job_id: int) -> list[dict[str, Any]]:
    rows = [row for row in _attempt_rows() if _int(row.get("作业id")) == job_id]
    return sorted(rows, key=lambda row: _int(row.get("尝试版本")))


def current_attempt(job: dict[str, Any]) -> dict[str, Any] | None:
    version = _int(job.get("当前尝试版本"))
    for row in attempts_of(_int(job.get("id"))):
        if _int(row.get("尝试版本")) == version:
            return row
    return None


def result_of_attempt(attempt_id: int) -> dict[str, Any] | None:
    for row in _result_rows():
        if _int(row.get("尝试id")) == attempt_id:
            return row
    return None


def results_of(job_id: int) -> list[dict[str, Any]]:
    rows = [row for row in _result_rows() if _int(row.get("作业id")) == job_id]
    return sorted(rows, key=lambda row: _int(row.get("结果版本")))


def _next_result_version() -> int:
    return max((_int(row.get("结果版本")) for row in _result_rows()), default=0) + 1


def _seconds_between(start: object, end: object) -> int | None:
    left, right = parse(start), parse(end)
    if left is None or right is None:
        return None
    return max(int((right - left).total_seconds()), 0)


def _job_input(job: dict[str, Any]) -> dict[str, Any]:
    return {field: job.get(field) for field in RESULT_ECHO_FIELDS}


def _build_result(
    job: dict[str, Any],
    attempt: dict[str, Any],
    *,
    status: str,
    source: str,
    reason: str,
    effective: bool,
    invalid_reason: str,
    content: dict[str, Any] | None = None,
    idempotency_key: str = "",
    result_version: int | None = None,
) -> dict[str, Any]:
    """生成一份可对照输入逐项核对的渲染结果。"""
    content = content or {}
    ended = iso()
    started = attempt.get("开始时间") or attempt.get("入队时间")
    result = {
        "id": _next_id(_result_rows()),
        "结果版本": result_version or _next_result_version(),
        "作业id": _int(job.get("id")),
        "作业编号": job.get("作业编号"),
        "尝试id": _int(attempt.get("id")),
        "尝试版本": _int(attempt.get("尝试版本")),
        "结果状态": status,
        "来源": source,
        "结束原因": reason,
        "生效": effective,
        "失效原因": invalid_reason,
        "幂等键": idempotency_key,
        "节点标识": attempt.get("节点标识") or "",
        "目标URL": job.get("目标URL"),
        "浏览器版本": job.get("浏览器版本"),
        "视口宽度": job.get("视口宽度"),
        "视口高度": job.get("视口高度"),
        "等待策略": attempt.get("等待策略") or job.get("等待策略"),
        "脚本注入": job.get("脚本注入"),
        "超时阈值秒": _int(attempt.get("超时阈值秒") or job.get("超时阈值秒")),
        "宽限秒": _int(attempt.get("宽限秒"), wait_grace(attempt.get("等待策略") or job.get("等待策略"))),
        "生效阈值秒": effective_timeout(job, attempt),
        "入队时间": attempt.get("入队时间"),
        "开始时间": attempt.get("开始时间"),
        "结束时间": ended,
        "耗时秒": _seconds_between(started, ended) or 0,
        "页面标题": str(content.get("页面标题") or ""),
        "内容长度": max(_int(content.get("内容长度"), 0), 0),
        "内容摘要": str(content.get("内容摘要") or "")[:200],
        "内容哈希": str(content.get("内容哈希") or ""),
        "输入快照": _job_input(job),
        "提交时间": ended,
        "调度消费": False,
        "消费清单版本": None,
        "消费时间": None,
    }
    _result_rows().append(result)
    return result


def _release_capacity(
    job: dict[str, Any],
    attempt: dict[str, Any],
    *,
    reason: str,
    result_version: int | None,
) -> dict[str, Any] | None:
    """按尝试去重地登记一次容量释放；没有占用节点或已释放过则返回 None。"""
    if not attempt.get("节点标识"):
        return None
    if attempt.get("容量已释放"):
        return None
    released_at = iso()
    attempt["容量已释放"] = True
    attempt["容量释放时间"] = released_at
    attempt["释放原因"] = reason
    row = {
        "id": _next_id(_release_rows()),
        "作业id": _int(job.get("id")),
        "作业编号": job.get("作业编号"),
        "尝试id": _int(attempt.get("id")),
        "尝试版本": _int(attempt.get("尝试版本")),
        "节点标识": attempt.get("节点标识"),
        "释放槽位": 1,
        "释放原因": reason,
        "释放时间": released_at,
        "结果版本": result_version,
        "已消费": False,
        "消费时间": None,
        "清单版本": None,
    }
    _release_rows().append(row)
    return row


def _close_attempt(attempt: dict[str, Any], status: str, reason: str, result_version: int) -> None:
    attempt["status"] = status
    attempt["结束时间"] = iso()
    attempt["结束原因"] = reason
    attempt["结果版本"] = result_version
    attempt["生效"] = False


def _invalidate_effective_results(
    job: dict[str, Any],
    keep_version: int | None,
    *,
    reason: str = "被更新尝试版本的结果取代",
) -> None:
    for row in results_of(_int(job.get("id"))):
        if _int(row.get("结果版本")) == keep_version:
            continue
        if row.get("生效"):
            row["生效"] = False
            row["失效原因"] = reason


def _should_requeue(job: dict[str, Any]) -> bool:
    limit = _int(job.get("最大重排次数"), DEFAULT_MAX_REQUEUE)
    return _int(job.get("重排次数")) < max(limit, 0)


def _new_attempt(job: dict[str, Any]) -> dict[str, Any]:
    version = _int(job.get("当前尝试版本")) + 1
    strategy = job.get("等待策略")
    threshold = normalize_timeout(job.get("超时阈值秒"))
    grace = wait_grace(strategy)
    attempt = {
        "id": _next_id(_attempt_rows()),
        "作业id": _int(job.get("id")),
        "作业编号": job.get("作业编号"),
        "尝试版本": version,
        "节点标识": "",
        "status": "排队中",
        "超时阈值秒": threshold,
        "等待策略": strategy,
        "宽限秒": grace,
        "生效阈值秒": threshold + grace,
        "入队时间": iso(),
        "开始时间": None,
        "结束时间": None,
        "结束原因": "",
        "结果版本": None,
        "生效": False,
        "容量已释放": False,
        "容量释放时间": None,
        "释放原因": "",
        "重复终止次数": 0,
    }
    _attempt_rows().append(attempt)
    job["当前尝试版本"] = version
    job["尝试次数"] = _int(job.get("尝试次数")) + 1
    return attempt


def _requeue(job: dict[str, Any], reason: str) -> dict[str, Any]:
    job["重排次数"] = _int(job.get("重排次数")) + 1
    attempt = _new_attempt(job)
    job["status"] = "排队中"
    job["pending"] = True
    job["abnormal"] = False
    job["排队时间"] = attempt["入队时间"]
    job["分配节点"] = ""
    job["生效结果版本"] = None
    job["生效尝试版本"] = None
    job["最近结局时间"] = iso()
    job["最近结局原因"] = f"{reason}，第 {job['重排次数']} 次重排"
    # 新一轮开始后旧结果不再代表当前作业，等新尝试结算再产生生效结果。
    _invalidate_effective_results(job, keep_version=None, reason="已重新排队，等待当前尝试结算")
    return attempt


def _settle(
    job: dict[str, Any],
    attempt: dict[str, Any],
    *,
    status: str,
    source: str,
    reason: str,
    requeue: bool,
    content: dict[str, Any] | None = None,
    idempotency_key: str = "",
) -> tuple[dict[str, Any], bool, str]:
    """结算一次尝试：返回 (结果, 是否新建, 说明)。

    调用方必须持有 store.lock；这里再做一次终态判断，保证并发下只有一个赢家。
    """
    existing = result_of_attempt(_int(attempt.get("id")))
    if existing is not None:
        return existing, False, (
            f"尝试 v{attempt.get('尝试版本')} 已结算（{attempt.get('结束原因') or existing.get('结果状态')}），"
            f"沿用结果版本 {existing.get('结果版本')}，不重复写入"
        )
    if attempt.get("status") in TERMINAL_STATUSES:
        return {}, False, f"尝试 v{attempt.get('尝试版本')} 已结算，没有可写入的结果"

    attempt_version = _int(attempt.get("尝试版本"))
    job_version = _int(job.get("当前尝试版本"))
    job_terminal = str(job.get("status")) in JOB_TERMINAL_STATUSES
    stale = attempt_version != job_version or job_terminal

    if stale:
        # 旧页面回调 / 旧超时：只归档为失效结果，绝不改写当前作业状态与生效结果。
        if job_terminal:
            invalid_reason = f"作业已{job.get('status')}，旧尝试结果不改变当前结果"
        else:
            invalid_reason = f"尝试 v{attempt_version} 已被 v{job_version} 取代"
        if attempt.get("status") in ACTIVE_STATUSES:
            _close_attempt(attempt, "已失败", invalid_reason, 0)
        result = _build_result(
            job,
            attempt,
            status=status,
            source=source,
            reason=reason,
            effective=False,
            invalid_reason=invalid_reason,
            content=content,
            idempotency_key=idempotency_key,
        )
        attempt["结果版本"] = result["结果版本"]
        attempt["结束原因"] = invalid_reason
        _release_capacity(job, attempt, reason=invalid_reason, result_version=result["结果版本"])
        return result, True, f"尝试 v{attempt_version} 已失效：{invalid_reason}"

    # 当前尝试的第一次结算：先落结果，再决定是否重排。
    result = _build_result(
        job,
        attempt,
        status=status,
        source=source,
        reason=reason,
        effective=not requeue,
        invalid_reason="已重新排队，等待下一尝试" if requeue else "",
        content=content,
        idempotency_key=idempotency_key,
    )
    _close_attempt(attempt, status, reason, result["结果版本"])
    attempt["生效"] = not requeue
    _release_capacity(job, attempt, reason=reason, result_version=result["结果版本"])

    if not requeue:
        # 生效结果取代同一作业下的旧结果。
        _invalidate_effective_results(job, keep_version=result["结果版本"])

    job["最近结局时间"] = result["结束时间"]
    if requeue:
        _requeue(job, reason)
        job["最近结局原因"] = f"{reason}，第 {job['重排次数']} 次重排"
        return result, True, (
            f"尝试 v{attempt_version} {reason}，已回收并重排为 v{job['当前尝试版本']}，"
            f"结果版本 {result['结果版本']} 仅归档"
        )

    job["status"] = status
    job["pending"] = False
    job["abnormal"] = status != "已完成"
    job["分配节点"] = ""
    job["生效结果版本"] = result["结果版本"]
    job["生效尝试版本"] = attempt_version
    job["最近结局原因"] = reason
    _invalidate_effective_results(job, keep_version=result["结果版本"])
    return result, True, f"尝试 v{attempt_version} 结算为{status}，结果版本 {result['结果版本']} 生效"


def start_job_queue(job: dict[str, Any]) -> dict[str, Any]:
    """首次/再次进入排队：开启新的尝试版本并记录排队时刻。"""
    with store.lock:
        attempt = _new_attempt(job)
        job["status"] = "排队中"
        job["pending"] = True
        job["abnormal"] = False
        job["排队时间"] = attempt["入队时间"]
        job["分配节点"] = ""
        job["生效结果版本"] = None
        job["生效尝试版本"] = None
        job["最近结局原因"] = "提交排队"
        return attempt


def start_job(job: dict[str, Any], *, node: str = "") -> tuple[dict[str, Any] | None, str]:
    """把排队中的作业推进到渲染中；只允许当前排队尝试前进一次。"""
    with store.lock:
        attempt = current_attempt(job)
        if attempt is None:
            return None, f"渲染作业 {job.get('作业编号')} 还没有排队尝试，不能启动渲染"
        if str(job.get("status")) not in {"排队中", "渲染中"}:
            return None, f"渲染作业 {job.get('作业编号')} 当前状态为{job.get('status')}，不能启动渲染"
        if attempt.get("status") != "排队中":
            return None, f"渲染作业 {job.get('作业编号')} 当前尝试不是排队中，不能启动渲染"
        attempt["status"] = "渲染中"
        attempt["开始时间"] = iso()
        if node:
            attempt["节点标识"] = node
        job["status"] = "渲染中"
        job["pending"] = False
        job["abnormal"] = False
        job["分配节点"] = attempt.get("节点标识") or ""
        job["最近结局原因"] = "启动渲染"
        return attempt, f"渲染作业 {job.get('作业编号')} 已启动渲染（尝试 v{attempt['尝试版本']}）"


def terminate_job(job: dict[str, Any], *, reason: str = "人工终止") -> tuple[dict[str, Any] | None, str]:
    with store.lock:
        attempt = current_attempt(job)
        if attempt is None:
            return None, f"渲染作业 {job.get('作业编号')} 没有进行中的尝试，容量不会重复释放"
        existing = result_of_attempt(_int(attempt.get("id")))
        if existing is not None:
            # 重复终止：只记次数，不再归还槽位；把既有结果回给调用方保持幂等。
            attempt["重复终止次数"] = _int(attempt.get("重复终止次数")) + 1
            return existing, (
                f"渲染作业 {job.get('作业编号')} 已结算（结果版本 {existing.get('结果版本')}），"
                f"重复终止 {attempt['重复终止次数']} 次，容量不会再次释放"
            )
        if attempt.get("status") not in ACTIVE_STATUSES:
            return None, f"渲染作业 {job.get('作业编号')} 当前尝试不是进行中，容量不会重复释放"
        result, created, message = _settle(
            job,
            attempt,
            status="已超时",
            source="人工终止",
            reason=reason,
            requeue=False,
        )
        if not created and result:
            message += "，重复终止不会再次释放容量"
        return result, message


def record_completion(
    job: dict[str, Any],
    *,
    attempt_version: int | None = None,
    content: dict[str, Any] | None = None,
    idempotency_key: str = "",
) -> tuple[dict[str, Any] | None, bool, str]:
    """完成回调入口：校验尝试版本与当前尝试，旧回调只归档为失效结果。"""
    with store.lock:
        if attempt_version is not None:
            attempt = next(
                (
                    row
                    for row in attempts_of(_int(job.get("id")))
                    if _int(row.get("尝试版本")) == attempt_version
                ),
                None,
            )
            if attempt is None:
                return None, False, f"渲染作业 {job.get('作业编号')} 不存在尝试 v{attempt_version}"
        else:
            attempt = current_attempt(job)
            if attempt is None:
                return None, False, f"渲染作业 {job.get('作业编号')} 还没有排队记录，无法提交结果"
            # 不带尝试版本的回调只能落到正在渲染的尝试；排队中的新尝试还没出结果，
            # 不能被上一轮的完成回调顺手结算，否则一次竞争会留下两份结果。
            if attempt.get("status") != "渲染中":
                return None, False, (
                    f"渲染作业 {job.get('作业编号')} 当前尝试 v{attempt.get('尝试版本')} 处于"
                    f"{attempt.get('status')}，没有可接收结果的渲染中尝试；请带上尝试版本重试"
                )

        if idempotency_key:
            for row in results_of(_int(job.get("id"))):
                # 幂等键按尝试版本生效：同一尝试的重复提交只落一份结果，
                # 但重排后的新尝试用同一个键仍然允许写入自己的结果。
                if (
                    row.get("幂等键") == idempotency_key
                    and _int(row.get("尝试版本")) == _int(attempt.get("尝试版本"))
                ):
                    return row, False, (
                        f"幂等键「{idempotency_key}」已提交过，沿用结果版本 {row.get('结果版本')}"
                    )

        result, created, message = _settle(
            job,
            attempt,
            status="已完成",
            source="完成回调",
            reason="渲染完成",
            requeue=False,
            content=content,
            idempotency_key=idempotency_key,
        )
        return (result or None), created, message


def settle_node_offline(node_code: str) -> list[dict[str, Any]]:
    """节点离线：把该节点上的进行中尝试统一结算，先到者留下唯一结果。"""
    settled: list[dict[str, Any]] = []
    with store.lock:
        for attempt in list(_attempt_rows()):
            if attempt.get("节点标识") != node_code:
                continue
            if attempt.get("status") not in ACTIVE_STATUSES:
                continue
            job = find_job(_int(attempt.get("作业id")))
            if job is None:
                continue
            requeue = _should_requeue(job)
            result, created, message = _settle(
                job,
                attempt,
                status="已失败",
                source="节点离线",
                reason=f"节点 {node_code} 离线",
                requeue=requeue,
            )
            settled.append(
                {
                    "作业编号": job.get("作业编号"),
                    "尝试版本": attempt.get("尝试版本"),
                    "结果版本": (result or {}).get("结果版本"),
                    "是否重排": bool(requeue),
                    "新建": created,
                    "说明": message,
                }
            )
    return settled


def reap_timeouts(*, job_id: int | None = None, at_iso: str | None = None) -> dict[str, Any]:
    """超时回收：读取尝试、队列状态与节点容量，回收失效结果并校验当前尝试。"""
    at = parse(at_iso) if at_iso else now()
    at_text = iso(at)
    reaped: list[dict[str, Any]] = []
    with store.lock:
        jobs = [job for job in _job_rows() if job_id is None or _int(job.get("id")) == job_id]
        for job in jobs:
            # 已完成/已归档的作业不会再被超时改写：先看作业终态，再看尝试。
            if str(job.get("status")) in JOB_TERMINAL_STATUSES:
                continue
            attempt = current_attempt(job)
            if attempt is None or attempt.get("status") not in ACTIVE_STATUSES:
                continue
            if result_of_attempt(_int(attempt.get("id"))) is not None:
                continue
            threshold = effective_timeout(job, attempt)
            clock = attempt.get("开始时间") or attempt.get("入队时间")
            waited = seconds_since(clock, at=at) or 0
            if waited < threshold:
                continue
            queue_timeout = attempt.get("status") == "排队中"
            reason = "排队超时" if queue_timeout else "渲染超时"
            requeue = _should_requeue(job)
            result, created, message = _settle(
                job,
                attempt,
                status="已超时",
                source="超时回收",
                reason=reason,
                requeue=requeue,
            )
            reaped.append(
                {
                    "作业编号": job.get("作业编号"),
                    "尝试版本": attempt.get("尝试版本"),
                    "回收类型": reason,
                    "已等待秒": waited,
                    "超时阈值秒": _int(attempt.get("超时阈值秒")),
                    "等待策略": attempt.get("等待策略"),
                    "宽限秒": _int(attempt.get("宽限秒")),
                    "生效阈值秒": threshold,
                    "重排次数": _int(job.get("重排次数")),
                    "是否重排": requeue,
                    "当前尝试版本": _int(job.get("当前尝试版本")),
                    "结果版本": (result or {}).get("结果版本"),
                    "结果生效": (result or {}).get("生效"),
                    "新建结果": created,
                    "说明": message,
                }
            )
        payload = {
            "ok": True,
            "message": f"超时回收到 {len(reaped)} 个尝试" if reaped else "当前没有到达超时阈值的渲染尝试",
            "回收时间": at_text,
            "已回收": len(reaped),
            "items": reaped,
            "队列": queue_snapshot(),
            "节点容量": capacity_snapshot(),
            "当前尝试校验": verify_current_attempts(),
        }
    return payload


def requeue_job(job: dict[str, Any], *, reason: str = "人工重排") -> dict[str, Any]:
    """人工重新排队：不新建结果，只开启下一尝试版本并记录重排次数。"""
    with store.lock:
        attempt = _requeue(job, reason)
        job["最近结局时间"] = iso()
        return attempt


def queue_snapshot() -> dict[str, Any]:
    jobs = []
    for job in _job_rows():
        status = str(job.get("status"))
        if status not in {"排队中", "渲染中"}:
            continue
        attempt = current_attempt(job)
        if attempt is None:
            continue
        threshold = effective_timeout(job, attempt)
        clock = attempt.get("开始时间") or attempt.get("入队时间")
        waited = seconds_since(clock) or 0
        jobs.append(
            {
                "作业编号": job.get("作业编号"),
                "status": status,
                "当前尝试版本": _int(job.get("当前尝试版本")),
                "重排次数": _int(job.get("重排次数")),
                "排队时间": attempt.get("入队时间"),
                "已等待秒": waited,
                "生效阈值秒": threshold,
                "剩余秒": max(threshold - waited, 0),
                "等待策略": attempt.get("等待策略"),
                "超时阈值秒": _int(attempt.get("超时阈值秒")),
                "是否到期": waited >= threshold,
            }
        )
    return {
        "排队中": sum(1 for item in jobs if item["status"] == "排队中"),
        "渲染中": sum(1 for item in jobs if item["status"] == "渲染中"),
        "最长等待秒": max((item["已等待秒"] for item in jobs), default=0),
        "items": jobs,
    }


def capacity_snapshot() -> list[dict[str, Any]]:
    rows = []
    for worker in store.rows("worker"):
        slots = _int(worker.get("并发槽位数"))
        occupied = _int(worker.get("已占用槽位"))
        heartbeat_age = seconds_since(worker.get("上次心跳时间"))
        rows.append(
            {
                "节点标识": worker.get("节点编号"),
                "status": worker.get("status"),
                "并发槽位数": slots,
                "已占用槽位": occupied,
                "剩余槽位": max(slots - occupied, 0),
                "上次心跳时间": worker.get("上次心跳时间"),
                "心跳秒": heartbeat_age,
                "可调度": bool(worker.get("可调度")),
            }
        )
    return rows


def verify_current_attempts() -> dict[str, Any]:
    """校验每个进行中作业的当前尝试恰好指向最新的排队/渲染尝试。"""
    issues: list[dict[str, Any]] = []
    checked = 0
    for job in _job_rows():
        status = str(job.get("status"))
        if status not in {"排队中", "渲染中"}:
            continue
        checked += 1
        attempts = attempts_of(_int(job.get("id")))
        latest = attempts[-1] if attempts else None
        attempt = current_attempt(job)
        active = [row for row in attempts if row.get("status") in ACTIVE_STATUSES]
        if attempt is None:
            issues.append({"作业编号": job.get("作业编号"), "问题": "当前尝试版本没有对应尝试"})
            continue
        if latest is not None and _int(latest.get("尝试版本")) != _int(job.get("当前尝试版本")):
            issues.append({"作业编号": job.get("作业编号"), "问题": "当前尝试不是最新尝试版本"})
        if len(active) != 1:
            issues.append(
                {
                    "作业编号": job.get("作业编号"),
                    "问题": f"进行中尝试数量为 {len(active)}，应恰好为 1",
                }
            )
        if attempt.get("status") != status and not (status == "排队中" and attempt.get("status") == "排队中"):
            issues.append(
                {
                    "作业编号": job.get("作业编号"),
                    "问题": f"作业状态 {status} 与尝试状态 {attempt.get('status')} 不一致",
                }
            )
    return {"ok": not issues, "checked": checked, "issues": issues}

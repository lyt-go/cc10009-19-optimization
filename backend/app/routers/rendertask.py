"""渲染任务接口：维护渲染作业，覆盖排队、启动、终止、重排与结果核对。

注意静态路径（/export、/reap、/verify、/{entry_id}/results）必须注册在
动态路径 /{entry_id} 之前，否则会被当成 entry_id 解析。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import renderledger
from app.services.rendertask import RendertaskService

router = APIRouter(prefix="/api/rendertask", tags=["渲染任务"])

service = RendertaskService()

LIST_FIELDS = ["作业编号", "目标URL", "浏览器版本", "视口宽度", "视口高度", "等待策略", "脚本注入", "超时阈值秒"]
STATUSES = ["草稿", "排队中", "渲染中", "已完成", "已超时", "已失败", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按作业编号检索"),
    status: str | None = Query(default=None, description="草稿、排队中、渲染中、已完成、已超时、已失败、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按作业编号与状态过滤渲染任务列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出渲染任务清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "rendertask", "total": total, "items": items}


@router.get("/queue", response_model=dict)
def queue_snapshot() -> dict:
    """队列状态：排队/渲染中的尝试、等待时长与到期判断。"""
    return renderledger.queue_snapshot()


@router.get("/capacity", response_model=dict)
def capacity_snapshot() -> dict:
    """节点容量：槽位、占用、心跳与可调度判定。"""
    return {"items": renderledger.capacity_snapshot()}


@router.get("/verify", response_model=dict)
def verify_attempts() -> dict:
    """校验进行中的作业是否恰好指向唯一的当前尝试。"""
    return renderledger.verify_current_attempts()


@router.post("/reap", response_model=dict)
def reap_timeouts(
    job_id: int | None = Query(default=None, description="只回收指定作业；不传则扫描全部"),
    at: str | None = Query(default=None, description="按指定时间点判断超时，便于复核"),
) -> dict:
    """执行超时回收：结算到期尝试、登记容量释放，并回读队列与容量。"""
    report = renderledger.reap_timeouts(job_id=job_id, at_iso=at)
    return report


@router.get("/{entry_id}/results", response_model=dict)
def list_results(entry_id: int) -> dict:
    """读取该作业的全部渲染结果，并逐项对照输入快照。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"渲染作业 {entry_id} 不存在或已归档")
    results = service.results(entry_id)
    return {
        "作业编号": entry.get("作业编号"),
        "生效结果版本": entry.get("生效结果版本"),
        "生效尝试版本": entry.get("生效尝试版本"),
        "重排次数": entry.get("重排次数"),
        "total": len(results),
        "items": results,
    }


@router.post("/{entry_id}/archive", response_model=ActionResult)
def archive_entry(entry_id: int) -> ActionResult:
    """归档已结算的渲染作业；仍有待处理动作时拦下。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=f"渲染作业 {entry_id} 不存在或已归档")
    updated, message = service.archive(entry)
    if updated is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=updated)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条渲染作业明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"渲染作业 {entry_id} 不存在或已归档")
    return service.job_view(entry)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条渲染作业，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="渲染作业已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交排队、启动渲染、终止渲染、重新排队、提交渲染结果。"""
    action = payload.action_name()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

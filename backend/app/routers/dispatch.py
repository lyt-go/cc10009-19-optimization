"""请求分配接口：维护分配策略，并提供派发解释与调度侧同步接口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dispatch import DispatchService

router = APIRouter(prefix="/api/dispatch", tags=["请求分配"])

service = DispatchService()

LIST_FIELDS = ["策略编号", "目标域名", "分配算法", "权重因子", "节点亲和性", "负载阈值", "再平衡间隔", "溢出处理"]
STATUSES = ["草稿", "策略验证中", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、策略验证中、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤请求分配列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出请求分配清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dispatch", "total": total, "items": items}


@router.get("/plan", response_model=dict)
def dispatch_plan() -> dict:
    """下一次派发解释：排队公平性、等待时长与节点新鲜度的综合判断。"""
    return service.plan()


@router.post("/sync", response_model=dict)
def sync_results() -> dict:
    """调度侧同步：消费容量释放与新的渲染结果，并写入调度清单与业务记录。"""
    return service.sync()


@router.get("/records", response_model=dict)
def list_records(limit: int = Query(default=20, ge=1, le=200)) -> dict:
    """调度侧业务记录：每次同步的清单版本与消费明细。"""
    items = service.records(limit=limit)
    return {"total": len(items), "items": items}


@router.get("/manifest", response_model=dict)
def manifest() -> dict:
    """调度侧清单：已消费结果版本与节点容量。"""
    return service.manifest()


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条分配策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分配策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条分配策略，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="分配策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条分配策略执行提交策略验证、启用策略、停用策略；不允许的动作会被拦下并说明原因。"""
    action = payload.action_name()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

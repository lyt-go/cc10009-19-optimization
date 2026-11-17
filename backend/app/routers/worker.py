"""采集节点接口：维护工作节点，并提供调度侧读取的容量与释放消费接口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.worker import WorkerService

router = APIRouter(prefix="/api/worker", tags=["采集节点"])

service = WorkerService()

LIST_FIELDS = ["节点编号", "主机地址", "并发槽位数", "地域标签", "CPU使用率", "内存可用MB", "上次心跳时间", "节点状态标签"]
STATUSES = ["草稿", "注册中", "在线", "离线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按节点编号检索"),
    status: str | None = Query(default=None, description="草稿、注册中、在线、离线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按节点编号与状态过滤采集节点列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集节点清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "worker", "total": total, "items": items}


@router.get("/schedulable", response_model=dict)
def schedulable_nodes() -> dict:
    """可调度节点结果：在线、心跳新鲜、剩余槽位大于 0。"""
    nodes = service.schedulable_nodes()
    return {
        "items": nodes,
        "total": len(nodes),
        "可调度节点数": sum(1 for node in nodes if node["可调度"]),
        "剩余槽位合计": sum(node["剩余槽位"] for node in nodes if node["可调度"]),
    }


@router.post("/capacity/consume", response_model=dict)
def consume_capacity() -> dict:
    """读取容量释放结果并回写剩余槽位；重复调用不会重复归还。"""
    return service.consume_capacity_releases()


@router.get("/manifest", response_model=dict)
def manifest() -> dict:
    """调度侧清单：已消费结果版本、消费条数与当前节点容量。"""
    return service.manifest()


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条工作节点明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"工作节点 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条工作节点，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="工作节点已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条工作节点执行提交注册、标记在线、标记离线；不允许的动作会被拦下并说明原因。"""
    action = payload.action_name()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

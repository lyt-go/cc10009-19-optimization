"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import threading
from typing import Any

from app.scheduler_seed import (
    ATTEMPTS,
    CAPACITY_RELEASES,
    DISPATCH_MANIFEST,
    RENDERTASK_LEDGER,
    WORKER_CAPACITY,
)
from app.seed import SEED_ROWS

# 这些表是渲染/调度链路的内部账本，不作为独立业务模块出现在概览里。
INTERNAL_MODULES = {
    "render_attempt",
    "render_result",
    "capacity_release",
    "dispatch_manifest",
    "dispatch_record",
}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 渲染尝试、渲染结果、容量释放与调度侧清单是超时回收/派发的账本表。
        self._tables.setdefault("render_attempt", [dict(row) for row in ATTEMPTS])
        self._tables.setdefault("render_result", [])
        self._tables.setdefault("capacity_release", [dict(row) for row in CAPACITY_RELEASES])
        self._tables.setdefault("dispatch_manifest", [dict(row) for row in DISPATCH_MANIFEST])
        self._merge_worker_capacity()
        self._merge_rendertask_ledger()
        self._merge_attempt_clocks()
        self._prime_worker_occupancy()
        # 渲染尝试、结果、容量释放必须在同一把锁里结算：
        # 超时回收、完成回调、节点离线可能由不同请求线程同时触发。
        self.lock = threading.RLock()

    def _merge_worker_capacity(self) -> None:
        for row in self.rows("worker"):
            extra = WORKER_CAPACITY.get(int(row.get("id", 0)))
            if not extra:
                continue
            row.update(extra)
            row.setdefault("已占用槽位", 0)
            row.setdefault("剩余槽位", int(extra["并发槽位数"]))
            # 只有在线节点可调度；草稿/注册中/离线一律不可派发。
            row.setdefault("可调度", False)

    def _merge_rendertask_ledger(self) -> None:
        for row in self.rows("rendertask"):
            ledger = RENDERTASK_LEDGER.get(int(row.get("id", 0)))
            if ledger:
                row.update(ledger)
            row.setdefault("当前尝试版本", 0)
            row.setdefault("尝试次数", 0)
            row.setdefault("重排次数", 0)
            row.setdefault("排队时间", None)
            row.setdefault("分配节点", "")
            row.setdefault("生效结果版本", None)
            row.setdefault("生效尝试版本", None)
            row.setdefault("最近结局时间", None)
            row.setdefault("最近结局原因", "")
            raw_timeout = str(row.get("超时阈值秒") or "").strip()
            # 示例数据里的「超时阈值秒」是占位文案，落成可计算的秒数；显式数字则保留。
            if not raw_timeout.isdigit() or int(raw_timeout) <= 0:
                row["超时阈值秒"] = 300

    def _merge_attempt_clocks(self) -> None:
        """给示例尝试补齐等待策略衍生的宽限与生效阈值，口径与运行时一致。"""
        from app.policies import normalize_timeout, wait_grace

        for attempt in self.rows("render_attempt"):
            job = self.find("rendertask", int(attempt.get("作业id") or 0)) or {}
            strategy = attempt.get("等待策略") or job.get("等待策略")
            attempt["等待策略"] = strategy
            attempt["超时阈值秒"] = normalize_timeout(
                attempt.get("超时阈值秒") or job.get("超时阈值秒")
            )
            attempt["宽限秒"] = wait_grace(strategy)
            attempt["生效阈值秒"] = attempt["超时阈值秒"] + attempt["宽限秒"]

    def _prime_worker_occupancy(self) -> None:
        """按尝试账本回填占用槽位：进行中且已上节点的尝试占 1 格。"""
        active = {"排队中", "渲染中"}
        occupied: dict[str, int] = {}
        for attempt in self.rows("render_attempt"):
            node = str(attempt.get("节点标识") or "")
            if node and attempt.get("status") in active and not attempt.get("容量已释放"):
                occupied[node] = occupied.get(node, 0) + 1
        for row in self.rows("worker"):
            used = occupied.get(str(row.get("节点编号") or ""), 0)
            row["已占用槽位"] = used
            row["剩余槽位"] = max(int(row.get("并发槽位数") or 0) - used, 0)

    def module_names(self) -> list[str]:
        """业务模块列表；render_attempt 等内部账本表不参与概览口径。"""
        return sorted(name for name in self._tables if name not in INTERNAL_MODULES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()

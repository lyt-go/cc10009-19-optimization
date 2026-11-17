"""调度口径常量：超时阈值、等待策略宽限与结果核对字段。

单独成模块是因为数据仓库初始化时就要用这些口径，而 renderledger 依赖 store，
不能让 store 反向导入它。
"""
from __future__ import annotations

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_REQUEUE = 2

# 等待策略决定超时宽限：网络空闲类等待要给在途请求留出收尾时间。
WAIT_GRACE: list[tuple[tuple[str, ...], int]] = [
    (("networkidle0", "networkidle-0"), 20),
    (("networkidle", "network-idle", "网络空闲"), 15),
    (("domcontentloaded", "dom-ready", "dom就绪", "dom完成"), 5),
    (("load", "加载完成", "加载就绪"), 0),
]

# 结果里需要与输入快照逐项对照的字段。
RESULT_ECHO_FIELDS = [
    "目标URL",
    "浏览器版本",
    "视口宽度",
    "视口高度",
    "等待策略",
    "脚本注入",
    "超时阈值秒",
]


def to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def normalize_timeout(value: object) -> int:
    number = to_int(value, 0)
    return number if number > 0 else DEFAULT_TIMEOUT_SECONDS


def wait_grace(strategy: object) -> int:
    text = str(strategy or "").strip().lower()
    if not text:
        return 0
    for keywords, grace in WAIT_GRACE:
        if any(keyword in text for keyword in keywords):
            return grace
    return 0

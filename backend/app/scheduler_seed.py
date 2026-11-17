"""调度相关示例数据：节点容量、渲染尝试、容量释放与调度侧清单。

seed.py 保持各业务模块的原始字段；这里给节点补上可计算的容量/心跳，
并准备两条带尝试版本的渲染记录，方便起服务后直接看到回收与派发链路。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.timeutil import iso, now

_BASE = now()

# 节点容量：并发槽位数落成整数，心跳时间相对启动时刻生成，避免示例数据天然过期。
WORKER_CAPACITY: dict[int, dict[str, Any]] = {
    1: {"并发槽位数": 2, "上次心跳时间": iso(_BASE - timedelta(seconds=900)), "节点状态标签": "心跳过期"},
    2: {"并发槽位数": 4, "上次心跳时间": iso(_BASE - timedelta(seconds=120)), "节点状态标签": "注册未上线"},
    3: {"并发槽位数": 3, "上次心跳时间": iso(_BASE - timedelta(seconds=15)), "节点状态标签": "心跳正常"},
}

# 渲染作业的尝试账本字段：版本、重排次数、排队时刻与当前结局。
RENDERTASK_LEDGER: dict[int, dict[str, Any]] = {
    1: {
        "当前尝试版本": 0,
        "尝试次数": 0,
        "重排次数": 0,
        "排队时间": None,
        "分配节点": "",
        "生效结果版本": None,
        "生效尝试版本": None,
        "最近结局时间": None,
        "最近结局原因": "",
    },
    # 第二次排队：第一次尝试超时回收后重排，重排次数用于派发公平性扣减。
    2: {
        "当前尝试版本": 2,
        "尝试次数": 2,
        "重排次数": 1,
        "排队时间": iso(_BASE - timedelta(seconds=180)),
        "分配节点": "",
        "生效结果版本": None,
        "生效尝试版本": None,
        "最近结局时间": iso(_BASE - timedelta(seconds=240)),
        "最近结局原因": "超时回收",
        "等待策略": "networkidle",
        "超时阈值秒": 60,
    },
    # 长时间渲染中：超时阈值 120 秒，启动已 400 秒，首次回收即可命中。
    3: {
        "当前尝试版本": 1,
        "尝试次数": 1,
        "重排次数": 0,
        "排队时间": iso(_BASE - timedelta(seconds=400)),
        "分配节点": "WORK-0003",
        "生效结果版本": None,
        "生效尝试版本": None,
        "最近结局时间": None,
        "最近结局原因": "",
        "等待策略": "domcontentloaded",
        "超时阈值秒": 120,
    },
}

ATTEMPTS: list[dict[str, Any]] = [
    {
        "id": 1,
        "作业id": 2,
        "作业编号": "REND-0002",
        "尝试版本": 1,
        "节点标识": "WORK-0003",
        "status": "已超时",
        "超时阈值秒": 60,
        "入队时间": iso(_BASE - timedelta(seconds=600)),
        "开始时间": iso(_BASE - timedelta(seconds=540)),
        "结束时间": iso(_BASE - timedelta(seconds=300)),
        "结束原因": "超时回收",
        "结果版本": None,
        "生效": False,
        "容量已释放": True,
        "容量释放时间": iso(_BASE - timedelta(seconds=300)),
        "释放原因": "超时回收",
        "重复终止次数": 0,
    },
    {
        "id": 2,
        "作业id": 2,
        "作业编号": "REND-0002",
        "尝试版本": 2,
        "节点标识": "",
        "status": "排队中",
        "超时阈值秒": 60,
        "入队时间": iso(_BASE - timedelta(seconds=180)),
        "开始时间": None,
        "结束时间": None,
        "结束原因": "",
        "结果版本": None,
        "生效": False,
        "容量已释放": False,
        "容量释放时间": None,
        "释放原因": "",
        "重复终止次数": 0,
    },
    {
        "id": 3,
        "作业id": 3,
        "作业编号": "REND-0003",
        "尝试版本": 1,
        "节点标识": "WORK-0003",
        "status": "渲染中",
        "超时阈值秒": 120,
        "入队时间": iso(_BASE - timedelta(seconds=400)),
        "开始时间": iso(_BASE - timedelta(seconds=400)),
        "结束时间": None,
        "结束原因": "",
        "结果版本": None,
        "生效": False,
        "容量已释放": False,
        "容量释放时间": None,
        "释放原因": "",
        "重复终止次数": 0,
    },
]

# 第一次尝试结算时已经释放过容量，调度侧消费后据此更新剩余槽位。
CAPACITY_RELEASES: list[dict[str, Any]] = [
    {
        "id": 1,
        "作业id": 2,
        "作业编号": "REND-0002",
        "尝试版本": 1,
        "节点标识": "WORK-0003",
        "释放槽位": 1,
        "释放原因": "超时回收",
        "释放时间": iso(_BASE - timedelta(seconds=300)),
        "结果版本": None,
        "已消费": False,
        "消费时间": None,
    }
]

DISPATCH_MANIFEST: list[dict[str, Any]] = [
    {
        "id": 1,
        "清单版本": 1,
        "已消费结果版本": 0,
        "已消费释放编号": 0,
        "消费结果条数": 0,
        "消费释放条数": 0,
        "同步时间": iso(_BASE),
        "可调度节点数": 0,
        "剩余槽位合计": 0,
    }
]

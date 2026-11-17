"""时间工具：统一使用带时区的 ISO 时间，避免排队/超时比较出现 naive/aware 混用。"""
from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or now()).isoformat(timespec="seconds")


def parse(value: object) -> datetime | None:
    """把库里的时间字段解析成带时区的时间；空值或无法解析时返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def seconds_since(value: object, *, at: datetime | None = None) -> int | None:
    moment = parse(value)
    if moment is None:
        return None
    return max(int(((at or now()) - moment).total_seconds()), 0)

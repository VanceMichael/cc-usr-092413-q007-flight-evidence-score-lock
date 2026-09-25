"""时间解析与规范化工具。所有持久化时间统一为 UTC ISO-8601(带 Z)。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def parse_ts(value: str) -> datetime:
    """解析 ISO-8601 时间;无时区按 UTC 处理,返回 UTC aware datetime。"""
    if not isinstance(value, str):
        raise ValueError("时间必须是 ISO-8601 字符串")
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"无法解析时间: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    """规范化为 UTC ISO 字符串(词法序即时间序,可直接比较)。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_hash(payload: dict) -> str:
    """对规范化后的字典计算稳定内容摘要,用于重复包归并与冲突检测。"""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

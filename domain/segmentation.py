"""航段切分纯函数。

排序铁律:一律按设备时间(device_time)排序;设备会话与设备序号只用于
同刻仲裁和重启/序号回退诊断,绝不跨会话使用全局序号——设备重启或跨午夜
时序号会回退,继续按序号排序会把轨迹拼错。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .util import iso

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class TrackPoint:
    device_time: datetime
    device_session_id: str
    device_seq: int
    lat: float
    lon: float
    altitude_m: float


@dataclass(frozen=True)
class Gate:
    gate_id: str
    ord: int
    kind: str  # start / waypoint / end
    lat: float
    lon: float
    radius_m: float


@dataclass(frozen=True)
class NoFlyZone:
    zone_id: str
    lat: float
    lon: float
    radius_m: float
    ceiling_m: float  # 高度上限:水平进入且校准高度不高于 ceiling 即视为穿越


def segment_flight(
    points: list[TrackPoint],
    gates: list[Gate],
    zones: list[NoFlyZone],
    altitude_offset_m: float = 0.0,
) -> dict:
    """依据起终门、有效航段、禁飞区和校准偏移切分航段。

    返回 {legs, events, point_count, complete};legs 中每条航段都带
    status(valid / no_fly_violation / truncated)与可展示的理由。
    """
    events: list[dict] = []
    if not points:
        return {"legs": [], "events": events, "point_count": 0, "complete": False}

    ordered = sorted(points, key=lambda p: (p.device_time, p.device_session_id, p.device_seq))

    # 重启/序号回退诊断:仅记录,不打断航段——航段沿时间轴连续计算。
    run_boundaries: list[int] = []  # ordered 中新运行段起点下标
    for i in range(1, len(ordered)):
        prev, cur = ordered[i - 1], ordered[i]
        if cur.device_session_id != prev.device_session_id:
            run_boundaries.append(i)
            events.append({
                "type": "session_restart",
                "at": iso(cur.device_time),
                "detail": f"设备会话由 {prev.device_session_id} 变为 {cur.device_session_id},按设备时间继续排序",
            })
        elif cur.device_seq <= prev.device_seq:
            run_boundaries.append(i)
            events.append({
                "type": "sequence_rollback",
                "at": iso(cur.device_time),
                "detail": f"设备序号由 {prev.device_seq} 回退到 {cur.device_seq}(设备重启),按设备时间继续排序",
            })
    for i in range(1, len(ordered)):
        if ordered[i - 1].device_time.date() != ordered[i].device_time.date():
            events.append({
                "type": "midnight_crossing",
                "at": iso(ordered[i].device_time),
                "detail": "航迹跨午夜,按设备时间排序而非全局序号",
            })

    gates_sorted = sorted(gates, key=lambda g: g.ord)
    # 依次进入期望门的首个点半径即记为进门;只认顺序,跳门不计。
    entries: list[tuple[int, int]] = []  # (gate 下标, point 下标)
    expected = 0
    for idx, p in enumerate(ordered):
        if expected < len(gates_sorted):
            g = gates_sorted[expected]
            if haversine_m(p.lat, p.lon, g.lat, g.lon) <= g.radius_m:
                entries.append((expected, idx))
                expected += 1

    legs: list[dict] = []

    def boundary_notes(i0: int, i1: int) -> list[str]:
        notes = []
        if any(i0 < b <= i1 for b in run_boundaries):
            notes.append("跨越设备重启/序号回退点,已按设备时间排序")
        if ordered[i0].device_time.date() != ordered[i1].device_time.date():
            notes.append("跨午夜航段,按设备时间排序")
        return notes

    for k in range(len(entries) - 1):
        (g0, i0), (g1, i1) = entries[k], entries[k + 1]
        seg_points = ordered[i0 + 1 : i1 + 1]
        violation: NoFlyZone | None = None
        for p in seg_points:
            calibrated_alt = p.altitude_m + altitude_offset_m
            for z in zones:
                if calibrated_alt <= z.ceiling_m and haversine_m(p.lat, p.lon, z.lat, z.lon) <= z.radius_m:
                    violation = z
                    break
            if violation:
                break
        duration = (ordered[i1].device_time - ordered[i0].device_time).total_seconds()
        notes = boundary_notes(i0, i1)
        if violation:
            status = "no_fly_violation"
            reason = f"穿越禁飞区 {violation.zone_id}(校准高度不高于 {violation.ceiling_m}m)"
        else:
            status = "valid"
            reason = "有效航段"
        if notes:
            reason += ";" + ";".join(notes)
        legs.append({
            "ord": k,
            "from_gate_id": gates_sorted[g0].gate_id,
            "to_gate_id": gates_sorted[g1].gate_id,
            "entered_at": iso(ordered[i0].device_time),
            "exited_at": iso(ordered[i1].device_time),
            "duration_s": duration,
            "status": status,
            "reason": reason,
            "point_count": len(seg_points) + 1,
        })

    complete = bool(gates_sorted) and expected == len(gates_sorted)
    if not complete and entries:
        g0, i0 = entries[-1]
        legs.append({
            "ord": len(legs),
            "from_gate_id": gates_sorted[g0].gate_id,
            "to_gate_id": None,
            "entered_at": iso(ordered[i0].device_time),
            "exited_at": None,
            "duration_s": None,
            "status": "truncated",
            "reason": "航段未完成:设备会话结束或序号回退后证据终止,未进入下一门",
            "point_count": len(ordered) - i0,
        })

    return {"legs": legs, "events": events, "point_count": len(ordered), "complete": complete}

"""轨迹分段引擎。

关键不变量：
- 绝不用设备序号做全局排序：设备重启会让序号回退，跨午夜也会。
  排序键为 (设备会话启动次序, 设备时间)。
- 门穿越在相邻点之间按距离线性插值，得到穿越时刻。
- 航段有效性同时受走廊宽度、禁飞区、重启断点与当时校准版本约束。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .models import EventConfig

EARTH_R = 6_371_000.0


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _local_meters(lat: float) -> tuple[float, float]:
    return (
        math.radians(1.0) * EARTH_R,
        math.radians(1.0) * EARTH_R * math.cos(math.radians(lat)),
    )


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """等距近似平面距离，赛事尺度足够。"""
    lat0 = (a[0] + b[0]) / 2.0
    mlat, mlon = _local_meters(lat0)
    dx = (b[1] - a[1]) * mlon
    dy = (b[0] - a[0]) * mlat
    return math.hypot(dx, dy)


def cross_track_m(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """点 p 到直线 a-b 的垂直距离（米）。"""
    lat0 = (p[0] + a[0] + b[0]) / 3.0
    mlat, mlon = _local_meters(lat0)

    def xy(q: tuple[float, float]) -> tuple[float, float]:
        return (q[1] - a[1]) * mlon, (q[0] - a[0]) * mlat

    px, py = xy(p)
    bx, by = xy(b)
    length = math.hypot(bx, by)
    if length == 0:
        return math.hypot(px, py)
    return abs(bx * py - by * px) / length


@dataclass
class TrajPoint:
    device_session: str
    device_seq: Optional[int]
    device_time: datetime
    received_at: str
    lat: float
    lon: float
    altitude: Optional[float]
    point_summary: Optional[str] = None
    calibrated_altitude: Optional[float] = None
    calibration_id: Optional[str] = None
    order: int = 0


@dataclass
class Crossing:
    gate_id: str
    time: datetime
    session: str
    point_index: int


@dataclass
class LegResult:
    corridor_id: str
    from_gate: str
    to_gate: str
    adopted: bool
    reason: str
    seconds: Optional[float] = None
    calibration_ids: list[str] = field(default_factory=list)
    point_count: int = 0
    no_fly_hits: list[str] = field(default_factory=list)
    session_restart: bool = False


@dataclass
class SegmentReport:
    legs: list[LegResult]
    notes: list[str]
    ordered_points: list[TrajPoint]
    total_seconds: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "total_seconds": self.total_seconds,
            "legs": [
                {
                    "corridor_id": l.corridor_id,
                    "from_gate": l.from_gate,
                    "to_gate": l.to_gate,
                    "adopted": l.adopted,
                    "reason": l.reason,
                    "seconds": l.seconds,
                    "calibration_ids": l.calibration_ids,
                    "point_count": l.point_count,
                    "no_fly_hits": l.no_fly_hits,
                    "session_restart": l.session_restart,
                }
                for l in self.legs
            ],
            "notes": self.notes,
        }


def _order_sessions(points: list[TrajPoint]) -> dict[str, int]:
    """会话启动次序：会话内最早设备时间（再以最早接收时间破并）。

    重启产生新会话，序号归零/回退因此不会跨会话污染排序。
    """
    first: dict[str, tuple[datetime, str]] = {}
    for p in points:
        cur = first.get(p.device_session)
        key = (p.device_time, p.received_at)
        if cur is None or key < cur:
            first[p.device_session] = key
    sessions = sorted(first, key=lambda s: first[s])
    return {s: i for i, s in enumerate(sessions)}


def _calibration_at(config: EventConfig, t: datetime):
    hit = None
    for c in config.calibrations:
        vf = parse_ts(c.valid_from)
        vt = parse_ts(c.valid_to) if c.valid_to else None
        if t >= vf and (vt is None or t < vt):
            if hit is None or vf > parse_ts(hit.valid_from):
                hit = c
    return hit


def _interpolate_crossing(p0: TrajPoint, p1: TrajPoint, gate, target_dist: float) -> datetime:
    d0 = distance_m((p0.lat, p0.lon), (gate.lat, gate.lon))
    d1 = distance_m((p1.lat, p1.lon), (gate.lat, gate.lon))
    span = (d0 - d1) or 1.0
    frac = min(1.0, max(0.0, (d0 - target_dist) / span))
    return p0.device_time + (p1.device_time - p0.device_time) * frac


def segment(points: list[TrajPoint], config: EventConfig) -> SegmentReport:
    notes: list[str] = []
    if not points:
        return SegmentReport(legs=[], notes=["无轨迹点"], ordered_points=[])

    session_order = _order_sessions(points)
    points = sorted(
        points,
        key=lambda p: (session_order[p.device_session], p.device_time, p.received_at),
    )

    # 同一(会话, 设备时间)的点只算一个：保留先入证据的位置，后到证据补齐
    # 气压高度/摘要（证据补齐），绝不把同一时刻计成两个点。
    deduped: list[TrajPoint] = []
    for p in points:
        if deduped and deduped[-1].device_session == p.device_session \
                and deduped[-1].device_time == p.device_time:
            base = deduped[-1]
            if base.altitude is None and p.altitude is not None:
                base.altitude = p.altitude
            base.point_summary = base.point_summary or p.point_summary
            if p.device_seq is not None:
                base.device_seq = p.device_seq
        else:
            deduped.append(p)
    points = deduped
    for i, p in enumerate(points):
        p.order = i

    # 记录设备序号回退与会话切换，仅作证据事实，绝不参与排序。
    prev_by_session: dict[str, int] = {}
    prev_session: Optional[str] = None
    crossed_midnight = False
    for p in points:
        if prev_session is not None and p.device_session != prev_session:
            notes.append(
                f"设备重启/新会话 {p.device_session}（上一会话 {prev_session}），"
                f"于 {p.device_time.isoformat()} 起按新会话与设备时间续接"
            )
        if p.device_session == prev_session and prev_session is not None:
            day0 = points[i - 1].device_time.date()
            if p.device_time.date() != day0:
                crossed_midnight = True
        if p.device_seq is not None:
            last_seq = prev_by_session.get(p.device_session)
            if last_seq is not None and p.device_seq < last_seq:
                notes.append(
                    f"会话 {p.device_session} 内设备序号回退 "
                    f"{last_seq} -> {p.device_seq} @ {p.device_time.isoformat()}，已忽略序号改按设备时间排序"
                )
            prev_by_session[p.device_session] = p.device_seq
        prev_session = p.device_session
    if crossed_midnight:
        notes.append("轨迹跨午夜，全程使用完整设备时间戳排序，未使用归零序号")

    # 逐点应用当时校准版本。
    for p in points:
        cal = _calibration_at(config, p.device_time)
        if cal is not None:
            p.calibration_id = cal.id
            if p.altitude is not None:
                p.calibrated_altitude = cal.altitude_offset_m + p.altitude * cal.altitude_scale

    # 检测各门穿越事件（按时间顺序，只接受同会话相邻点）。
    gate_map = {g.id: g for g in config.gates}
    crossings: list[Crossing] = []
    used_gate: dict[str, datetime] = {}
    for i in range(1, len(points)):
        p0, p1 = points[i - 1], points[i]
        if p0.device_session != p1.device_session:
            continue  # 重启间隙不能插值出穿越
        for gid, gate in gate_map.items():
            d0 = distance_m((p0.lat, p0.lon), (gate.lat, gate.lon))
            d1 = distance_m((p1.lat, p1.lon), (gate.lat, gate.lon))
            if d0 > gate.radius_m >= d1 and p1.device_time > used_gate.get(gid, datetime.min.replace(tzinfo=p1.device_time.tzinfo)):
                crossings.append(
                    Crossing(gid, _interpolate_crossing(p0, p1, gate, gate.radius_m),
                             p1.device_session, i)
                )
                used_gate[gid] = p1.device_time
    crossings.sort(key=lambda c: c.time)

    legs: list[LegResult] = []
    total = 0.0
    any_adopted = False
    cursor = 0  # 单调消费门穿越事件，避免后一航段复用前面的穿越

    for corridor in config.corridors:
        leg = LegResult(
            corridor_id=corridor.id,
            from_gate=corridor.from_gate,
            to_gate=corridor.to_gate,
            adopted=False,
            reason="",
        )
        si = next(
            (i for i in range(cursor, len(crossings)) if crossings[i].gate_id == corridor.from_gate),
            None,
        )
        if si is None:
            leg.reason = f"未检测到起点门 {corridor.from_gate} 穿越"
            legs.append(leg)
            continue
        ei = next(
            (i for i in range(si + 1, len(crossings)) if crossings[i].gate_id == corridor.to_gate),
            None,
        )
        if ei is None:
            leg.reason = f"未检测到终点门 {corridor.to_gate} 穿越"
            legs.append(leg)
            cursor = si + 1
            continue
        start, end = crossings[si], crossings[ei]
        cursor = ei + 1

        seg_points = [p for p in points[start.point_index:end.point_index + 1]]
        leg.point_count = len(seg_points)
        leg.calibration_ids = sorted({p.calibration_id for p in seg_points if p.calibration_id})

        # 1) 重启断点
        sessions_in = {p.device_session for p in seg_points}
        leg.session_restart = start.session != end.session or len(sessions_in) > 1
        # 2) 走廊偏航
        a = (gate_map[corridor.from_gate].lat, gate_map[corridor.from_gate].lon)
        b = (gate_map[corridor.to_gate].lat, gate_map[corridor.to_gate].lon)
        off_track = [
            p for p in seg_points
            if cross_track_m((p.lat, p.lon), a, b) > corridor.half_width_m
        ]
        # 3) 禁飞区
        for nfz in config.no_fly_zones:
            if any(distance_m((p.lat, p.lon), (nfz.lat, nfz.lon)) <= nfz.radius_m for p in seg_points):
                leg.no_fly_hits.append(nfz.id)
        # 4) 航段中途穿越了起终门之外的其他门
        intermediate = [
            crossings[k].gate_id for k in range(si + 1, ei)
            if crossings[k].gate_id not in (corridor.from_gate, corridor.to_gate)
        ]

        problems = []
        if leg.session_restart:
            problems.append("航段内发生设备重启，证据不连续")
        if off_track:
            problems.append(
                f"{len(off_track)} 个轨迹点偏离有效航段走廊（>{corridor.half_width_m}m）"
            )
        if leg.no_fly_hits:
            problems.append(f"侵入禁飞区: {','.join(leg.no_fly_hits)}")
        if intermediate:
            problems.append(f"中途穿越其他门点: {','.join(intermediate)}")
        if end.time <= start.time:
            problems.append("终点穿越时刻不晚于起点")

        leg.seconds = (end.time - start.time).total_seconds()
        if problems:
            leg.reason = "；".join(problems)
        else:
            leg.adopted = True
            leg.reason = "采纳：门穿越完整、处于有效航段、未触禁飞区"
            if leg.calibration_ids:
                leg.reason += f"，采用校准 {','.join(leg.calibration_ids)}"
            total += leg.seconds
            any_adopted = True
        legs.append(leg)

    report = SegmentReport(legs=legs, notes=notes, ordered_points=points)
    report.total_seconds = total if any_adopted else None
    return report

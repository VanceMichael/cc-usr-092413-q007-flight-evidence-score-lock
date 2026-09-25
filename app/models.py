"""请求/响应模型与赛事配置。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


# --- 赛事几何配置 -----------------------------------------------------------

class Gate(BaseModel):
    id: str
    lat: float
    lon: float
    radius_m: float = 75.0          # 穿越判定半径


class Corridor(BaseModel):
    """有效航段走廊：起点门 -> 终点门，中点半径之外视为偏航。"""
    id: str
    from_gate: str
    to_gate: str
    half_width_m: float = 250.0    # 距起终门连线的最大允许横向距离


class NoFlyZone(BaseModel):
    id: str
    lat: float
    lon: float
    radius_m: float                # 圆形禁飞区


class Calibration(BaseModel):
    """校准版本：对气压高度的偏移/缩放，按生效时间切换。"""
    id: str
    valid_from: str                # ISO 设备时间
    valid_to: Optional[str] = None
    altitude_offset_m: float = 0.0
    altitude_scale: float = 1.0


class EventConfig(BaseModel):
    start_gate: str
    finish_gate: str
    gates: list[Gate]
    corridors: list[Corridor]
    no_fly_zones: list[NoFlyZone] = Field(default_factory=list)
    calibrations: list[Calibration] = Field(default_factory=list)

    def gate(self, gate_id: str) -> Gate:
        for g in self.gates:
            if g.id == gate_id:
                return g
        raise ValueError(f"未知门点: {gate_id}")


class CreateEvent(BaseModel):
    name: str
    config: EventConfig
    appeal_deadline: Optional[str] = None


# --- 证据数据包 -------------------------------------------------------------

class PointIn(BaseModel):
    device_session: str = Field(description="设备会话标识；设备重启后变化")
    device_seq: Optional[int] = Field(default=None, description="设备侧序号，可能回退")
    device_time: str
    lat: float
    lon: float
    altitude: Optional[float] = None
    point_summary: Optional[str] = None

    @field_validator("device_time")
    @classmethod
    def _parse_time(cls, v: str) -> str:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


class EvidencePacketIn(BaseModel):
    packet_id: str
    points: list[PointIn]
    summary: Optional[str] = None


# --- 成绩 -------------------------------------------------------------------

class SubmitScoreIn(BaseModel):
    judge_id: str
    note: Optional[str] = None


class ArbitrationIn(BaseModel):
    arbiter_id: str
    decision: Literal["confirmed", "rejected"]
    comment: Optional[str] = None
    seen_watermark: Optional[int] = Field(
        default=None, description="显式声明确认的证据水位；缺省取登记的查看水位"
    )


class AssessmentDecisionIn(BaseModel):
    approver_id: str
    decision: Literal["approved", "rejected"]
    comment: Optional[str] = None


# --- 响应 -------------------------------------------------------------------

class IngestResult(BaseModel):
    packet_id: str
    state: Literal["merged", "duplicate", "quarantined", "held"]
    evidence_seq: Optional[int] = None
    detail: str

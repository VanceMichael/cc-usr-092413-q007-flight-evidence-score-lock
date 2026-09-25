"""HTTP 接口层:竞速飞行证据补齐与成绩锁定服务。

鉴权采用请求头 X-User-Id / X-User-Role(player/uploader/referee/arbitrator/admin);
自批限制(选手、上传者、提交裁判)在服务层按身份强制校验。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Literal, NamedTuple

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from domain.service import DomainError, EvidenceService
from domain.util import parse_ts


# ---------- 请求模型 ----------

class TrackPointIn(BaseModel):
    point_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    device_session_id: str = Field(min_length=1)
    device_seq: int = Field(ge=0)
    device_time: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    altitude_m: float
    digest: str = Field(min_length=1)

    @field_validator("device_time")
    @classmethod
    def _check_device_time(cls, v: str) -> str:
        parse_ts(v)
        return v


class PacketIn(BaseModel):
    packet_id: str = Field(min_length=1)
    flight_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    player_id: str = Field(min_length=1)
    points: list[TrackPointIn] = Field(min_length=1)


class GateIn(BaseModel):
    gate_id: str = Field(min_length=1)
    ord: int = Field(ge=0)
    kind: Literal["start", "waypoint", "end"]
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: float = Field(gt=0)


class ZoneIn(BaseModel):
    zone_id: str = Field(min_length=1)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: float = Field(gt=0)
    ceiling_m: float


class CalibrationIn(BaseModel):
    calibration_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    valid_from: str
    altitude_offset_m: float = 0.0
    note: str | None = None

    @field_validator("valid_from")
    @classmethod
    def _check_valid_from(cls, v: str) -> str:
        parse_ts(v)
        return v


class LegDecisionIn(BaseModel):
    leg_id: str = Field(min_length=1)
    decision: Literal["adopted", "excluded"]
    reason: str | None = None


class ScoreIn(BaseModel):
    segmentation_id: str | None = None
    decisions: list[LegDecisionIn] = Field(min_length=1)


class ConfirmIn(BaseModel):
    waterline_seen: int = Field(ge=0)


class ResolveIn(BaseModel):
    resolution: Literal["keep_existing", "keep_incoming"]


class ReopenIn(BaseModel):
    reason: str = Field(min_length=1)


# ---------- 鉴权 ----------

class User(NamedTuple):
    user_id: str
    role: str


def current_user(
    x_user_id: str | None = Header(default=None),
    x_user_role: str | None = Header(default=None),
) -> User:
    if not x_user_id or not x_user_role:
        raise HTTPException(status_code=401, detail="缺少 X-User-Id / X-User-Role 请求头")
    return User(x_user_id, x_user_role)


def require(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail=f"需要角色: {'/'.join(roles)}")
        return user
    return dep


# ---------- 应用工厂 ----------

def create_app(service: EvidenceService | None = None) -> FastAPI:
    if service is None:
        service = EvidenceService(
            os.environ.get("EVIDENCE_DB_PATH", ":memory:"),
            quorum=int(os.environ.get("EVIDENCE_QUORUM", "2")),
            appeal_window_s=float(os.environ.get("EVIDENCE_APPEAL_WINDOW_S", str(72 * 3600))),
            auto_drain=os.environ.get("EVIDENCE_DEFER_JOBS", "") != "1",
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        service.recover()  # 恢复:中断任务重新入队续跑,申诉期限按绝对时间继续
        yield

    app = FastAPI(title="翼装竞速证据服务", lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(DomainError)
    async def domain_error_handler(_, exc: DomainError):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    # ---- 基础 ----

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    # ---- 赛道与校准配置 ----

    @app.post("/api/v1/events/{event_id}/gates", status_code=201)
    def upsert_gates(event_id: str, gates: list[GateIn],
                     user: User = Depends(require("referee", "admin"))):
        return service.upsert_gates(event_id, [g.model_dump() for g in gates])

    @app.post("/api/v1/events/{event_id}/no-fly-zones", status_code=201)
    def upsert_zones(event_id: str, zones: list[ZoneIn],
                     user: User = Depends(require("referee", "admin"))):
        return service.upsert_zones(event_id, [z.model_dump() for z in zones])

    @app.post("/api/v1/events/{event_id}/calibrations", status_code=201)
    def add_calibration(event_id: str, body: CalibrationIn,
                        user: User = Depends(require("referee", "admin"))):
        return service.add_calibration(event_id, body.model_dump())

    # ---- 证据接收与隔离 ----

    @app.post("/api/v1/evidence/packets", status_code=201)
    def ingest_packet(body: PacketIn, user: User = Depends(require("uploader", "admin"))):
        return service.ingest_packet(user.user_id, body.model_dump())

    @app.get("/api/v1/quarantine")
    def list_quarantine(flight_id: str | None = None):
        return service.list_quarantine(flight_id)

    @app.post("/api/v1/quarantine/{quarantine_id}/resolve")
    def resolve_quarantine(quarantine_id: str, body: ResolveIn,
                           user: User = Depends(require("referee", "admin"))):
        return service.resolve_quarantine(quarantine_id, body.resolution, user.user_id)

    # ---- 航班与成绩 ----

    @app.get("/api/v1/flights")
    def list_flights(event_id: str | None = None):
        return service.list_flights(event_id)

    @app.get("/api/v1/flights/{flight_id}")
    def flight_detail(flight_id: str):
        return service.flight_detail(flight_id)

    @app.get("/api/v1/flights/{flight_id}/points")
    def list_points(flight_id: str):
        return service.list_points(flight_id)

    @app.post("/api/v1/flights/{flight_id}/scores", status_code=201)
    def submit_score(flight_id: str, body: ScoreIn,
                     user: User = Depends(require("referee"))):
        return service.submit_score(user.user_id, flight_id, body.segmentation_id,
                                    [d.model_dump() for d in body.decisions])

    @app.get("/api/v1/scores/{score_id}")
    def get_score(score_id: str):
        return service.get_score(score_id)

    @app.post("/api/v1/scores/{score_id}/confirmations", status_code=201)
    def confirm_score(score_id: str, body: ConfirmIn,
                      user: User = Depends(require("arbitrator"))):
        return service.confirm_score(user.user_id, score_id, body.waterline_seen)

    # ---- 排名 / 发布 / 重开 ----

    @app.get("/api/v1/events/{event_id}")
    def event_detail(event_id: str):
        return service.event_detail(event_id)

    @app.get("/api/v1/events/{event_id}/ranking")
    def event_ranking(event_id: str):
        return service.event_ranking(event_id)

    @app.get("/api/v1/events/{event_id}/publications")
    def event_publications(event_id: str):
        return service.event_publications(event_id)

    @app.get("/api/v1/events/{event_id}/assessments")
    def event_assessments(event_id: str):
        return service.event_assessments(event_id)

    @app.post("/api/v1/events/{event_id}/publish", status_code=201)
    def publish_event(event_id: str, user: User = Depends(require("referee", "admin"))):
        return service.publish_event(event_id, user.user_id)

    @app.post("/api/v1/assessments/{assessment_id}/reopen", status_code=201)
    def approve_reopen(assessment_id: str, body: ReopenIn,
                       user: User = Depends(require("admin"))):
        return service.approve_reopen(assessment_id, user.user_id, body.reason)

    @app.post("/api/v1/assessments/{assessment_id}/reject")
    def reject_assessment(assessment_id: str, user: User = Depends(require("admin"))):
        return service.reject_assessment(assessment_id, user.user_id)

    # ---- 任务队列 ----

    @app.get("/api/v1/jobs")
    def list_jobs():
        return service.list_jobs()

    @app.post("/api/v1/jobs/drain")
    def drain_jobs(user: User = Depends(require("admin"))):
        return {"processed": service.drain()}

    return app


app = create_app()

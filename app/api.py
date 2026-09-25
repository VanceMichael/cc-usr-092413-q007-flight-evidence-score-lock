"""HTTP 接口。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import service
from .models import (
    ArbitrationIn,
    AssessmentDecisionIn,
    CreateEvent,
    EvidencePacketIn,
    SubmitScoreIn,
)
from .service import startup_recovery
from .storage import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(os.environ.get("EVIDENCE_DB"))
    startup_recovery()
    yield


app = FastAPI(title="翼装竞速证据服务", lifespan=lifespan)


def _user(x_user: str | None) -> str:
    if not x_user:
        raise HTTPException(401, "缺少 X-User 身份头")
    return x_user


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/v1/events", status_code=201)
def create_event(body: CreateEvent):
    return {"event_id": service.create_event(body.name, body.config, body.appeal_deadline)}


class CreateFlight(BaseModel):
    pilot_id: str


@app.post("/api/v1/events/{event_id}/flights", status_code=201)
def create_flight(event_id: int, body: CreateFlight):
    try:
        return {"flight_id": service.create_flight(event_id, body.pilot_id)}
    except service.DomainError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/v1/flights/{flight_id}/evidence")
def ingest(flight_id: int, body: EvidencePacketIn, x_user: str | None = Header(default=None)):
    try:
        return service.ingest_packet(flight_id, body, _user(x_user))
    except service.DomainError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/v1/flights/{flight_id}/scores")
def submit_score(flight_id: int, body: SubmitScoreIn):
    try:
        return service.submit_score(flight_id, body)
    except service.DomainError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/v1/flights/{flight_id}/evidence-view")
def view_evidence(flight_id: int, score_id: int, x_user: str | None = Header(default=None)):
    try:
        return service.view_evidence(flight_id, score_id, _user(x_user))
    except service.DomainError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/v1/flights/{flight_id}/scores/{score_id}/arbitration")
def arbitrate(flight_id: int, score_id: int, body: ArbitrationIn):
    try:
        return service.arbitrate(flight_id, score_id, body)
    except service.DomainError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/v1/flights/{flight_id}")
def flight_detail(flight_id: int):
    try:
        return service.flight_detail(flight_id)
    except service.DomainError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/v1/events/{event_id}/lock")
def lock_event(event_id: int):
    try:
        return service.lock_event(event_id)
    except service.DomainError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/v1/events/{event_id}/publish")
def publish_event(event_id: int):
    try:
        return service.publish_event(event_id)
    except service.DomainError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/v1/events/{event_id}/ranking")
def ranking(event_id: int):
    try:
        return service.ranking_detail(event_id)
    except service.DomainError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/v1/assessments/{assessment_id}")
def get_assessment(assessment_id: int):
    from .storage import fetchone, loads
    row = fetchone("SELECT * FROM assessments WHERE id=?", (assessment_id,))
    if row is None:
        raise HTTPException(404, "影响评估不存在")
    return {"assessment_id": assessment_id, "state": row["state"],
            "flight_id": row["flight_id"], "impact": loads(row["impact_json"]),
            "decided_by": row["decided_by"]}


@app.post("/api/v1/assessments/{assessment_id}/decision")
def decide_assessment(assessment_id: int, body: AssessmentDecisionIn):
    try:
        return service.decide_assessment(assessment_id, body)
    except service.DomainError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/v1/flights/{flight_id}/republish")
def republish(flight_id: int):
    from .storage import fetchone
    flight = fetchone("SELECT * FROM flights WHERE id=?", (flight_id,))
    if flight is None:
        raise HTTPException(404, "飞行不存在")
    try:
        return service.confirm_alternate_and_republish(int(flight["event_id"]), flight_id)
    except service.DomainError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/v1/events/{event_id}/notifications")
def notifications(event_id: int):
    from .storage import fetchall, loads
    rows = fetchall(
        "SELECT id,flight_id,kind,payload_json,created_at FROM notifications "
        "WHERE event_id=? ORDER BY id", (event_id,)
    )
    return {"items": [
        {"id": r["id"], "flight_id": r["flight_id"], "kind": r["kind"],
         "payload": loads(r["payload_json"]), "created_at": r["created_at"]}
        for r in rows
    ]}


@app.post("/api/v1/events/{event_id}/pause")
def pause(event_id: int):
    try:
        return service.pause_event(event_id)
    except service.DomainError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/v1/events/{event_id}/resume")
def resume(event_id: int):
    try:
        return service.resume_event(event_id)
    except service.DomainError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/v1/flights")
def flights(event_id: int | None = None):
    from .storage import fetchall
    if event_id is not None:
        rows = fetchall("SELECT id,pilot_id,event_id FROM flights WHERE event_id=? ORDER BY id",
                        (event_id,))
    else:
        rows = fetchall("SELECT id,pilot_id,event_id FROM flights ORDER BY id")
    return {"items": [dict(r) for r in rows]}

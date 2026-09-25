"""测试公共装置:假时钟、客户端、赛道与轨迹构造。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import create_app
from domain.service import EvidenceService
from domain.util import iso, parse_ts


class Clock:
    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 9, 25, 8, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> None:
        self.t += timedelta(**kwargs)


def make_service(**kwargs) -> tuple[EvidenceService, Clock]:
    clock = kwargs.pop("clock", None) or Clock()
    service = EvidenceService(":memory:", now_fn=clock, **kwargs)
    return service, clock


@contextmanager
def client_of(service: EvidenceService):
    with TestClient(create_app(service)) as client:
        yield client


def H(user_id: str, role: str) -> dict:
    return {"X-User-Id": user_id, "X-User-Role": role}


REFEREE = H("ref1", "referee")
REFEREE2 = H("ref2", "referee")
ADMIN = H("admin1", "admin")
ARB1 = H("arb1", "arbitrator")
ARB2 = H("arb2", "arbitrator")
ARB3 = H("arb3", "arbitrator")


def uploader(uid: str = "up1") -> dict:
    return H(uid, "uploader")


START_GATE = {"gate_id": "G-START", "ord": 0, "kind": "start",
              "lat": 45.0, "lon": 7.0, "radius_m": 60.0}
END_GATE = {"gate_id": "G-END", "ord": 1, "kind": "end",
            "lat": 45.0, "lon": 7.02, "radius_m": 60.0}


def setup_course(client, event: str = "E1", zones: list[dict] | None = None):
    r = client.post(f"/api/v1/events/{event}/gates", json=[START_GATE, END_GATE], headers=REFEREE)
    assert r.status_code == 201, r.text
    if zones:
        r = client.post(f"/api/v1/events/{event}/no-fly-zones", json=zones, headers=REFEREE)
        assert r.status_code == 201, r.text


def make_track(n: int = 12, *, start: str = "2026-09-25T10:00:00Z", step_s: int = 30,
               lat0: float = 45.0, lon0: float = 7.0, lon1: float = 7.02,
               alt: float = 1500.0, session: str = "S1", seq0: int = 1,
               point_prefix: str = "p", digest: str = "dg") -> list[dict]:
    """从起点门圆心到终点门圆心的匀速直线轨迹。"""
    t0 = parse_ts(start)
    points = []
    for i in range(n):
        frac = i / (n - 1) if n > 1 else 1.0
        points.append({
            "point_id": f"{point_prefix}{seq0 + i}",
            "device_id": "DEV1",
            "device_session_id": session,
            "device_seq": seq0 + i,
            "device_time": iso(t0 + timedelta(seconds=step_s * i)),
            "lat": lat0,
            "lon": lon0 + (lon1 - lon0) * frac,
            "altitude_m": alt,
            "digest": f"{digest}{seq0 + i}",
        })
    return points


def upload(client, flight: str, player: str, points: list[dict], packet_id: str,
           *, event: str = "E1", uploader_id: str = "up1", expect: int = 201) -> dict:
    r = client.post("/api/v1/evidence/packets", json={
        "packet_id": packet_id, "flight_id": flight, "event_id": event,
        "player_id": player, "points": points,
    }, headers=uploader(uploader_id))
    assert r.status_code == expect, r.text
    return r.json()


def submit_score(client, flight: str, *, referee: dict = REFEREE, expect: int = 201) -> dict:
    """按最新分段提交候选成绩:有效航段采用,其余排除并附理由。"""
    detail = client.get(f"/api/v1/flights/{flight}").json()
    run = detail["segmentation_runs"][-1]
    decisions = []
    for leg in run["legs"]:
        if leg["status"] == "valid":
            decisions.append({"leg_id": leg["leg_id"], "decision": "adopted"})
        else:
            decisions.append({"leg_id": leg["leg_id"], "decision": "excluded",
                              "reason": leg["reason"]})
    r = client.post(f"/api/v1/flights/{flight}/scores",
                    json={"decisions": decisions}, headers=referee)
    assert r.status_code == expect, r.text
    return r.json()


def confirm(client, score: dict, headers: dict, waterline_seen: int | None = None,
            expect: int = 201) -> dict:
    r = client.post(f"/api/v1/scores/{score['score_id']}/confirmations",
                    json={"waterline_seen": score["waterline"] if waterline_seen is None else waterline_seen},
                    headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


def submit_and_lock(client, flight: str, *, referee: dict = REFEREE,
                    arbitrators: tuple = (ARB1, ARB2)) -> dict:
    score = submit_score(client, flight, referee=referee)
    for arb in arbitrators:
        confirm(client, score, arb)
    return client.get(f"/api/v1/scores/{score['score_id']}").json()


# ---------- 服务级(不经 HTTP)辅助 ----------

GATE_DICTS = [
    {"gate_id": "G-START", "ord": 0, "kind": "start", "lat": 45.0, "lon": 7.0, "radius_m": 60.0},
    {"gate_id": "G-END", "ord": 1, "kind": "end", "lat": 45.0, "lon": 7.02, "radius_m": 60.0},
]


def packet(packet_id: str, flight: str, player: str, points: list[dict], event: str = "E1") -> dict:
    return {"packet_id": packet_id, "flight_id": flight, "event_id": event,
            "player_id": player, "points": points}


def service_submit_and_lock(svc, flight: str, referee: str = "ref1",
                            arbs: tuple = ("arb1", "arb2")) -> dict:
    detail = svc.flight_detail(flight)
    run = detail["segmentation_runs"][-1]
    decisions = [
        {"leg_id": l["leg_id"], "decision": "adopted"} if l["status"] == "valid"
        else {"leg_id": l["leg_id"], "decision": "excluded", "reason": l["reason"]}
        for l in run["legs"]
    ]
    score = svc.submit_score(referee, flight, None, decisions)
    for arb in arbs:
        svc.confirm_score(arb, score["score_id"], score["waterline"])
    return svc.get_score(score["score_id"])

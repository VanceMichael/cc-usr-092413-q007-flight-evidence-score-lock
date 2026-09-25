"""分段引擎:跨午夜、设备重启序号回退、禁飞区、校准版本。"""
from datetime import timedelta

from domain.segmentation import Gate, NoFlyZone, TrackPoint, segment_flight
from domain.util import parse_ts
from tests.conftest import (REFEREE, client_of, make_service, make_track,
                            setup_course, upload)

GATES = [Gate("G-START", 0, "start", 45.0, 7.0, 60.0),
         Gate("G-END", 1, "end", 45.0, 7.02, 60.0)]


def straight_points(times, session="S1", seq0=1, lon0=7.0, lon1=7.02, alt=1500.0):
    n = len(times)
    return [TrackPoint(t, session, seq0 + i, 45.0, lon0 + (lon1 - lon0) * (i / (n - 1)), alt)
            for i, t in enumerate(times)]


def test_cross_midnight_flight_uses_device_time_not_sequence():
    t0 = parse_ts("2026-09-25T23:59:00Z")
    times = [t0 + timedelta(seconds=30 * i) for i in range(6)]  # 跨午夜到 00:01:30
    result = segment_flight(straight_points(times), GATES, [])
    assert result["complete"] is True
    assert len(result["legs"]) == 1
    leg = result["legs"][0]
    assert leg["status"] == "valid"
    assert leg["duration_s"] == 150.0  # 若按跨天回退的时钟或全局序号排序,时长会被算错
    assert "跨午夜" in leg["reason"]
    assert any(e["type"] == "midnight_crossing" for e in result["events"])


def test_device_restart_sequence_rollback_does_not_break_ordering():
    t0 = parse_ts("2026-09-25T10:00:00Z")
    times = [t0 + timedelta(seconds=30 * i) for i in range(8)]
    # 设备中途重启:同一会话里序号从 4 回退到 1(与题目场景一致)
    points = []
    n = len(times)
    for i, t in enumerate(times):
        seq = i + 1 if i < 4 else i - 3
        points.append(TrackPoint(t, "S1", seq, 45.0, 7.0 + 0.02 * (i / (n - 1)), 1500.0))
    result = segment_flight(points, GATES, [])
    assert result["complete"] is True
    leg = result["legs"][0]
    # 若按全局序号排序,重启后的点会被插到航迹开头,航段时长与进门判定都会错
    assert leg["duration_s"] == 210.0
    assert "重启" in leg["reason"] or "回退" in leg["reason"]
    assert any(e["type"] == "sequence_rollback" for e in result["events"])


def test_session_change_mid_flight_keeps_course_progress():
    t0 = parse_ts("2026-09-25T10:00:00Z")
    first = straight_points([t0 + timedelta(seconds=30 * i) for i in range(4)],
                            session="S1", lon1=7.01)
    second = straight_points([t0 + timedelta(seconds=120 + 30 * i) for i in range(4)],
                             session="S2", lon0=7.01)
    result = segment_flight(first + second, GATES, [])
    assert result["complete"] is True
    assert result["legs"][0]["duration_s"] == 210.0
    assert any(e["type"] == "session_restart" for e in result["events"])


def test_no_fly_zone_violation_flags_leg():
    t0 = parse_ts("2026-09-25T10:00:00Z")
    times = [t0 + timedelta(seconds=30 * i) for i in range(6)]
    zone = [NoFlyZone("NFZ-1", 45.0, 7.01, 200.0, 2000.0)]
    result = segment_flight(straight_points(times), GATES, zone)
    leg = result["legs"][0]
    assert leg["status"] == "no_fly_violation"
    assert "NFZ-1" in leg["reason"]


def test_calibration_offset_decides_violation():
    t0 = parse_ts("2026-09-25T10:00:00Z")
    times = [t0 + timedelta(seconds=30 * i) for i in range(6)]
    zone = [NoFlyZone("NFZ-1", 45.0, 7.01, 200.0, 1500.0)]
    points = straight_points(times, alt=1490.0)
    # 未校准:1490 <= 1500 触发穿越
    assert segment_flight(points, GATES, zone, 0.0)["legs"][0]["status"] == "no_fly_violation"
    # 校准 +20m:1510 > 1500 不穿越
    assert segment_flight(points, GATES, zone, 20.0)["legs"][0]["status"] == "valid"


def test_truncated_leg_when_track_ends_before_end_gate():
    t0 = parse_ts("2026-09-25T10:00:00Z")
    times = [t0 + timedelta(seconds=30 * i) for i in range(4)]
    result = segment_flight(straight_points(times, lon1=7.005), GATES, [])
    assert result["complete"] is False
    assert result["legs"][-1]["status"] == "truncated"
    assert result["legs"][-1]["duration_s"] is None


def test_service_picks_calibration_version_effective_at_flight_time():
    service, _ = make_service()
    with client_of(service) as client:
        setup_course(client, zones=[{"zone_id": "NFZ-1", "lat": 45.0, "lon": 7.01,
                                     "radius_m": 200.0, "ceiling_m": 1500.0}])
        r = client.post("/api/v1/events/E1/calibrations", json={
            "calibration_id": "CAL-1", "version": "v1-zero",
            "valid_from": "2026-09-01T00:00:00Z", "altitude_offset_m": 0.0}, headers=REFEREE)
        assert r.status_code == 201
        r = client.post("/api/v1/events/E1/calibrations", json={
            "calibration_id": "CAL-2", "version": "v2-plus20",
            "valid_from": "2026-09-20T00:00:00Z", "altitude_offset_m": 20.0}, headers=REFEREE)
        assert r.status_code == 201

        upload(client, "F1", "pilot1", make_track(n=6, alt=1490.0), "PK1")
        detail = client.get("/api/v1/flights/F1").json()
        run = detail["segmentation_runs"][-1]
        # 飞行时间 2026-09-25,应选用当时生效的 v2-plus20,而非 v1-zero
        assert run["calibration_version"] == "v2-plus20"
        assert run["legs"][0]["status"] == "valid"

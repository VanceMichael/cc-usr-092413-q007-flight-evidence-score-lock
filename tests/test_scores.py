"""成绩冻结与仲裁确认:采用/排除航段固化、自批禁止、所见版本确认。"""
from tests.conftest import (ARB1, ARB2, H, REFEREE, client_of, make_service,
                            make_track, setup_course, submit_and_lock, upload)


def _setup_flight(client, flight="F1", player="pilot1", **track_kw):
    setup_course(client)
    upload(client, flight, player, make_track(**track_kw), f"PK-{flight}")
    return client.get(f"/api/v1/flights/{flight}").json()


def test_candidate_score_frozen_with_adopted_and_excluded_legs():
    service, _ = make_service()
    with client_of(service) as client:
        detail = _setup_flight(client)
        run = detail["segmentation_runs"][-1]
        legs = run["legs"]
        decisions = [
            {"leg_id": legs[0]["leg_id"], "decision": "adopted"},
        ]
        r = client.post("/api/v1/flights/F1/scores", json={"decisions": decisions}, headers=REFEREE)
        assert r.status_code == 201, r.text
        score = r.json()
        assert score["status"] == "frozen"
        assert score["version"] == 1
        assert score["total_time_s"] == legs[0]["duration_s"]
        assert score["complete"] is True
        assert score["legs"][0]["decision"] == "adopted"
        assert score["legs"][0]["segment_reason"] == "有效航段"
        assert score["waterline"] == run["waterline"]


def test_excluded_leg_frozen_with_reason():
    service, _ = make_service()
    with client_of(service) as client:
        setup_course(client, zones=[{"zone_id": "NFZ-1", "lat": 45.0, "lon": 7.01,
                                     "radius_m": 200.0, "ceiling_m": 2000.0}])
        upload(client, "F1", "pilot1", make_track(), "PK-F1")
        detail = client.get("/api/v1/flights/F1").json()
        leg = detail["segmentation_runs"][-1]["legs"][0]
        assert leg["status"] == "no_fly_violation"
        r = client.post("/api/v1/flights/F1/scores", json={"decisions": [
            {"leg_id": leg["leg_id"], "decision": "excluded", "reason": "穿越禁飞区,按规则排除"},
        ]}, headers=REFEREE)
        assert r.status_code == 201, r.text
        score = r.json()
        assert score["complete"] is False
        assert score["legs"][0]["decision"] == "excluded"
        assert "禁飞区" in score["legs"][0]["decision_reason"]


def test_excluded_leg_requires_reason():
    service, _ = make_service()
    with client_of(service) as client:
        detail = _setup_flight(client)
        leg = detail["segmentation_runs"][-1]["legs"][0]
        r = client.post("/api/v1/flights/F1/scores", json={"decisions": [
            {"leg_id": leg["leg_id"], "decision": "excluded"},
        ]}, headers=REFEREE)
        assert r.status_code == 422


def test_decisions_must_cover_exactly_all_legs():
    service, _ = make_service()
    with client_of(service) as client:
        detail = _setup_flight(client)
        r = client.post("/api/v1/flights/F1/scores", json={"decisions": [
            {"leg_id": "leg_不存在", "decision": "adopted"},
        ]}, headers=REFEREE)
        assert r.status_code == 422


def test_quorum_locks_score_and_supersedes_old_version():
    service, _ = make_service()
    with client_of(service) as client:
        _setup_flight(client)
        first = submit_and_lock(client, "F1")
        assert first["status"] == "locked"
        # 第二版候选成绩锁定后,旧版被替代
        second = submit_and_lock(client, "F1")
        assert second["version"] == 2
        assert second["status"] == "locked"
        assert second["supersedes"] == first["score_id"]
        assert client.get(f"/api/v1/scores/{first['score_id']}").json()["status"] == "superseded"


def test_confirmation_counts_only_when_waterline_matches():
    service, _ = make_service()
    with client_of(service) as client:
        detail = _setup_flight(client)
        leg = detail["segmentation_runs"][-1]["legs"][0]
        score = client.post("/api/v1/flights/F1/scores", json={"decisions": [
            {"leg_id": leg["leg_id"], "decision": "adopted"},
        ]}, headers=REFEREE).json()
        # 仲裁人看到的证据版本落后于冻结版本:记录在案但不计入法定确认数
        r = client.post(f"/api/v1/scores/{score['score_id']}/confirmations",
                        json={"waterline_seen": score["waterline"] - 1}, headers=ARB1)
        assert r.status_code == 201
        assert r.json()["counts"] is False
        assert client.get(f"/api/v1/scores/{score['score_id']}").json()["status"] == "frozen"
        # 看到正确版本的确认才计入,达到法定人数后锁定
        r = client.post(f"/api/v1/scores/{score['score_id']}/confirmations",
                        json={"waterline_seen": score["waterline"]}, headers=ARB2)
        assert r.json()["counts"] is True
        assert client.get(f"/api/v1/scores/{score['score_id']}").json()["status"] == "frozen"
        r = client.post(f"/api/v1/scores/{score['score_id']}/confirmations",
                        json={"waterline_seen": score["waterline"]}, headers=H("arb3", "arbitrator"))
        assert r.json()["locked"] is True


def test_player_and_uploader_have_no_self_approval():
    service, _ = make_service()
    with client_of(service) as client:
        _setup_flight(client, player="pilot1")
        detail = client.get("/api/v1/flights/F1").json()
        leg = detail["segmentation_runs"][-1]["legs"][0]
        score = client.post("/api/v1/flights/F1/scores", json={"decisions": [
            {"leg_id": leg["leg_id"], "decision": "adopted"},
        ]}, headers=REFEREE).json()
        wl = score["waterline"]
        # 选手本人(即使挂着仲裁角色)不能确认自己的成绩
        r = client.post(f"/api/v1/scores/{score['score_id']}/confirmations",
                        json={"waterline_seen": wl}, headers=H("pilot1", "arbitrator"))
        assert r.status_code == 403
        # 证据上传者不能确认相关成绩
        r = client.post(f"/api/v1/scores/{score['score_id']}/confirmations",
                        json={"waterline_seen": wl}, headers=H("up1", "arbitrator"))
        assert r.status_code == 403
        # 提交裁判不能兼任独立仲裁人
        r = client.post(f"/api/v1/scores/{score['score_id']}/confirmations",
                        json={"waterline_seen": wl}, headers=H("ref1", "arbitrator"))
        assert r.status_code == 403
        # 非仲裁角色直接被接口拒绝
        r = client.post(f"/api/v1/scores/{score['score_id']}/confirmations",
                        json={"waterline_seen": wl}, headers=H("someone", "player"))
        assert r.status_code == 403


def test_score_submission_requires_referee_role():
    service, _ = make_service()
    with client_of(service) as client:
        detail = _setup_flight(client)
        leg = detail["segmentation_runs"][-1]["legs"][0]
        r = client.post("/api/v1/flights/F1/scores", json={"decisions": [
            {"leg_id": leg["leg_id"], "decision": "adopted"},
        ]}, headers=ARB1)
        assert r.status_code == 403

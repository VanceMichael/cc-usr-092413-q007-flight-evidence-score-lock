"""排名发布、补证影响评估、获准重开与替代成绩。"""
from tests.conftest import (ADMIN, ARB1, ARB2, REFEREE, client_of, make_service,
                            make_track, setup_course, submit_and_lock,
                            submit_score, upload)


def _setup_two_flights(client):
    """F2 完整 300s;F1 只有前半程(设备重启前),航段截断。"""
    setup_course(client)
    upload(client, "F2", "pilotB", make_track(n=11, step_s=30), "PK-F2")
    first_half = make_track(n=6, step_s=30, lon1=7.005, session="S1")
    upload(client, "F1", "pilotA", first_half, "PK-F1A")
    submit_and_lock(client, "F2")
    submit_and_lock(client, "F1")


def _supplementary_second_half():
    # 设备重启后续传:序号从 1 回退重排,时间接续,终点门前 5 个点
    return make_track(n=5, start="2026-09-25T10:02:40Z", step_s=10,
                      lon0=7.005, lon1=7.02, session="S1", seq0=1,
                      point_prefix="q", digest="e")


def test_pre_publish_evidence_triggers_recompute():
    service, _ = make_service()
    with client_of(service) as client:
        setup_course(client)
        upload(client, "F2", "pilotB", make_track(n=11), "PK-F2")
        submit_and_lock(client, "F2")
        ranking = client.get("/api/v1/events/E1/ranking").json()["ranking"]
        assert ranking["version"] == 1
        assert [e["flight_id"] for e in ranking["entries"]] == ["F2"]

        # 发布前补交新航班证据:触发重算,分段与草稿排名随之更新
        upload(client, "F1", "pilotA", make_track(n=6, lon1=7.005), "PK-F1A")
        detail = client.get("/api/v1/flights/F1").json()
        assert detail["segmentation_runs"], "补证后应已重算分段"
        submit_and_lock(client, "F1")
        ranking = client.get("/api/v1/events/E1/ranking").json()["ranking"]
        assert ranking["version"] == 2
        assert [e["flight_id"] for e in ranking["entries"]] == ["F2", "F1"]
        assert ranking["entries"][1]["complete"] is False


def test_publish_freezes_waterline_and_creates_notifications():
    service, _ = make_service()
    with client_of(service) as client:
        _setup_two_flights(client)
        r = client.post("/api/v1/events/E1/publish", headers=REFEREE)
        assert r.status_code == 201, r.text
        pub = r.json()
        assert pub["waterline"] is not None
        assert pub["appeal_deadline"] > "2026-09-25"

        pubs = client.get("/api/v1/events/E1/publications").json()["items"]
        assert len(pubs) == 1
        notifications = pubs[0]["notifications"]
        assert {n["player_id"]: n["rank"] for n in notifications} == {"pilotB": 1, "pilotA": 2}

        detail = client.get("/api/v1/events/E1").json()
        assert detail["latest_publication"]["appeal_open"] is True

        # 已发布后不能直接再发布
        r = client.post("/api/v1/events/E1/publish", headers=REFEREE)
        assert r.status_code == 409


def test_post_publish_evidence_generates_impact_assessment_only():
    service, _ = make_service()
    with client_of(service) as client:
        _setup_two_flights(client)
        pub = client.post("/api/v1/events/E1/publish", headers=REFEREE).json()
        ranking_before = client.get("/api/v1/events/E1/ranking").json()["ranking"]

        # 发布后补交重启后半程证据:只生成影响评估,不动已发布排名
        upload(client, "F1", "pilotA", _supplementary_second_half(), "PK-F1B")
        assessments = client.get("/api/v1/events/E1/assessments").json()["items"]
        assert len(assessments) == 1
        asm = assessments[0]
        assert asm["status"] == "pending"
        assert asm["would_change"] == 1
        assert asm["waterline_before"] == pub["waterline"]
        assert asm["waterline_after"] > pub["waterline"]
        by_flight = {d["flight_id"]: d for d in asm["details"]}
        assert by_flight["F1"]["old_rank"] == 2
        assert by_flight["F1"]["new_rank"] == 1
        assert by_flight["F2"]["old_rank"] == 1
        assert by_flight["F2"]["new_rank"] == 2

        ranking_after = client.get("/api/v1/events/E1/ranking").json()["ranking"]
        assert ranking_after["ranking_id"] == ranking_before["ranking_id"]


def test_reopen_creates_replacement_scores_and_preserves_original():
    service, _ = make_service()
    with client_of(service) as client:
        _setup_two_flights(client)
        pub = client.post("/api/v1/events/E1/publish", headers=REFEREE).json()
        upload(client, "F1", "pilotA", _supplementary_second_half(), "PK-F1B")
        asm = client.get("/api/v1/events/E1/assessments").json()["items"][0]

        # 未获准重开前,新证据不会进入排名
        ranking = client.get("/api/v1/events/E1/ranking").json()["ranking"]
        assert [e["player_id"] for e in ranking["entries"]] == ["pilotB", "pilotA"]

        # 获准重开:建立替代成绩,原排名与通知事实保留
        r = client.post(f"/api/v1/assessments/{asm['assessment_id']}/reopen",
                        json={"reason": "补交气压计证据属实,同意重开"}, headers=ADMIN)
        assert r.status_code == 201, r.text

        detail = client.get("/api/v1/flights/F1").json()
        assert len(detail["segmentation_runs"]) >= 2
        latest_run = detail["segmentation_runs"][-1]
        assert latest_run["complete"] is True
        assert any(e["type"] == "sequence_rollback" for e in latest_run["events"])

        replacement = submit_and_lock(client, "F1")
        assert replacement["version"] == 2
        assert replacement["status"] == "locked"
        assert replacement["total_time_s"] == 200.0
        # 冻结的航段理由直接说明跨重启的排序处理
        assert "重启" in replacement["legs"][0]["segment_reason"] \
            or "回退" in replacement["legs"][0]["segment_reason"]

        ranking = client.get("/api/v1/events/E1/ranking").json()["ranking"]
        assert ranking["version"] >= 3
        entries = {e["flight_id"]: e for e in ranking["entries"]}
        assert entries["F1"]["rank"] == 1
        assert entries["F1"]["rank_delta"] == 1
        assert entries["F1"]["score_version"] == 2
        assert entries["F2"]["rank"] == 2
        assert entries["F2"]["rank_delta"] == -1
        # 查询结果直接展示各航段理由与成绩版本
        assert entries["F1"]["legs"][0]["decision"] == "adopted"
        assert entries["F1"]["legs"][0]["segment_reason"]

        # 原发布与通知事实保持原样
        pubs = client.get("/api/v1/events/E1/publications").json()["items"]
        assert len(pubs) == 1
        assert {n["player_id"]: n["rank"] for n in pubs[0]["notifications"]} == \
            {"pilotB": 1, "pilotA": 2}

        # 重开后可再发布,产生新的通知事实,旧的仍在
        r = client.post("/api/v1/events/E1/publish", headers=REFEREE)
        assert r.status_code == 201, r.text
        pubs = client.get("/api/v1/events/E1/publications").json()["items"]
        assert len(pubs) == 2
        assert {n["player_id"]: n["rank"] for n in pubs[0]["notifications"]} == \
            {"pilotB": 1, "pilotA": 2}
        assert {n["player_id"]: n["rank"] for n in pubs[1]["notifications"]} == \
            {"pilotA": 1, "pilotB": 2}


def test_post_publish_irrelevant_evidence_explains_no_change():
    service, _ = make_service()
    with client_of(service) as client:
        _setup_two_flights(client)
        client.post("/api/v1/events/E1/publish", headers=REFEREE)
        # 补交终点门之后的多余轨迹:评估结论为不影响,直接说明为何维持原成绩
        extra = make_track(n=3, start="2026-09-25T10:06:00Z", step_s=30,
                           lon0=7.03, lon1=7.04, point_prefix="x", digest="x")
        upload(client, "F2", "pilotB", extra, "PK-F2B")
        assessments = client.get("/api/v1/events/E1/assessments").json()["items"]
        assert len(assessments) == 1
        assert assessments[0]["would_change"] == 0
        assert assessments[0]["details"] == []
        ranking = client.get("/api/v1/events/E1/ranking").json()["ranking"]
        assert [e["player_id"] for e in ranking["entries"]] == ["pilotB", "pilotA"]


def test_reject_assessment_keeps_ranking_closed():
    service, _ = make_service()
    with client_of(service) as client:
        _setup_two_flights(client)
        client.post("/api/v1/events/E1/publish", headers=REFEREE)
        upload(client, "F1", "pilotA", _supplementary_second_half(), "PK-F1B")
        asm = client.get("/api/v1/events/E1/assessments").json()["items"][0]
        r = client.post(f"/api/v1/assessments/{asm['assessment_id']}/reject", headers=ADMIN)
        assert r.status_code == 200
        r = client.post("/api/v1/events/E1/publish", headers=REFEREE)
        assert r.status_code == 409  # 评估被驳回,赛事仍处于已发布未重开状态

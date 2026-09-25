"""证据接收:完整字段、重复归并、冲突隔离与裁决。"""
from tests.conftest import (ADMIN, ARB1, REFEREE, client_of, make_service,
                            make_track, setup_course, upload, uploader, H)


def test_track_points_stored_with_full_fields():
    service, _ = make_service()
    with client_of(service) as client:
        setup_course(client)
        points = make_track(n=4)
        upload(client, "F1", "pilot1", points, "PK1")

        stored = client.get("/api/v1/flights/F1/points").json()["items"]
        assert len(stored) == 4
        for got, sent in zip(stored, points):
            assert got["device_time"] == sent["device_time"]          # 设备时间
            assert got["received_at"] is not None                     # 接收时间(服务端盖章)
            assert got["device_session_id"] == sent["device_session_id"]
            assert got["device_seq"] == sent["device_seq"]
            assert (got["lat"], got["lon"]) == (sent["lat"], sent["lon"])
            assert got["altitude_m"] == sent["altitude_m"]
            assert got["digest"] == sent["digest"]                    # 摘要
            assert got["status"] == "accepted"


def test_same_packet_replayed_is_idempotent():
    service, _ = make_service()
    with client_of(service) as client:
        setup_course(client)
        first = upload(client, "F1", "pilot1", make_track(n=4), "PK1")
        second = upload(client, "F1", "pilot1", make_track(n=4), "PK1")
        assert second["idempotent"] is True
        assert second["waterline"] == first["waterline"]
        detail = client.get("/api/v1/flights/F1").json()
        assert detail["points"]["accepted"] == 4
        assert detail["points"]["duplicates"] == 0


def test_same_packet_id_with_different_content_rejected():
    service, _ = make_service()
    with client_of(service) as client:
        setup_course(client)
        upload(client, "F1", "pilot1", make_track(n=4), "PK1")
        changed = make_track(n=4, digest="other")
        upload(client, "F1", "pilot1", changed, "PK1", expect=409)


def test_duplicate_points_merged_without_waterline_change():
    service, _ = make_service()
    with client_of(service) as client:
        setup_course(client)
        first = upload(client, "F1", "pilot1", make_track(n=4), "PK1")
        # 同一批点换了个数据包再次到达:归并,不推进证据水位
        second = upload(client, "F1", "pilot1", make_track(n=4), "PK2")
        assert second["accepted"] == 0
        assert second["duplicates"] == 4
        assert second["waterline"] is None
        detail = client.get("/api/v1/flights/F1").json()
        assert detail["points"]["accepted"] == 4
        assert detail["points"]["duplicates"] == 4
        assert detail["evidence_waterline"] == first["waterline"]


def test_conflicting_point_quarantined_and_excluded_then_resolved():
    service, _ = make_service()
    with client_of(service) as client:
        setup_course(client)
        base = make_track(n=4)
        upload(client, "F1", "pilot1", base, "PK1")

        # 标识相同(p1)但内容不同:先隔离;另一个新点正常接收
        tampered = [dict(base[0], lat=46.0, digest="tampered"),
                    dict(base[1], point_id="p9", device_seq=9, digest="dg9")]
        resp = upload(client, "F1", "pilot1", tampered, "PK2")
        assert resp["quarantined"] == 1
        assert resp["accepted"] == 1

        detail = client.get("/api/v1/flights/F1").json()
        assert detail["points"]["quarantined"] == 1
        # 被隔离的点不参与分段:最新分段只统计仍被接受的点
        run = detail["segmentation_runs"][-1]
        assert run["point_count"] == detail["points"]["accepted"] == 4

        quarantine = client.get("/api/v1/quarantine?flight_id=F1").json()["items"]
        assert len(quarantine) == 1
        qid = quarantine[0]["quarantine_id"]
        assert quarantine[0]["incoming_payload"]["lat"] == 46.0

        # 裁决采用新内容后,该点重新进入证据集合
        r = client.post(f"/api/v1/quarantine/{qid}/resolve",
                        json={"resolution": "keep_incoming"}, headers=REFEREE)
        assert r.status_code == 200, r.text
        detail = client.get("/api/v1/flights/F1").json()
        assert detail["points"]["quarantined"] == 0
        assert detail["points"]["accepted"] == 5
        assert detail["segmentation_runs"][-1]["point_count"] == 5
        stored = {p["point_id"]: p for p in client.get("/api/v1/flights/F1/points").json()["items"]}
        assert stored["p1"]["lat"] == 46.0


def test_quarantine_resolve_keep_existing():
    service, _ = make_service()
    with client_of(service) as client:
        setup_course(client)
        base = make_track(n=4)
        upload(client, "F1", "pilot1", base, "PK1")
        upload(client, "F1", "pilot1", [dict(base[0], altitude_m=999.0, digest="x")], "PK2")
        qid = client.get("/api/v1/quarantine?flight_id=F1").json()["items"][0]["quarantine_id"]
        r = client.post(f"/api/v1/quarantine/{qid}/resolve",
                        json={"resolution": "keep_existing"}, headers=ADMIN)
        assert r.status_code == 200, r.text
        stored = {p["point_id"]: p for p in client.get("/api/v1/flights/F1/points").json()["items"]}
        assert stored["p1"]["altitude_m"] == 1500.0
        assert stored["p1"]["status"] == "accepted"


def test_ingest_requires_uploader_role():
    service, _ = make_service()
    with client_of(service) as client:
        setup_course(client)
        r = client.post("/api/v1/evidence/packets", json={
            "packet_id": "PK1", "flight_id": "F1", "event_id": "E1",
            "player_id": "pilot1", "points": make_track(n=2),
        }, headers=H("pilot1", "player"))
        assert r.status_code == 403

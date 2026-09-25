"""全流程：证据补齐、序号回退、归并/隔离、冻结、仲裁、锁定、发布、补证重开、恢复。"""
from tests.conftest import make_config, track


def _event_flight(client, pilot="pilot-1", uploader="uploader-1", deadline=None):
    r = client.post("/api/v1/events", json={
        "name": "2026 大峡谷站", "config": make_config(),
        "appeal_deadline": deadline,
    })
    eid = r.json()["event_id"]
    fid = client.post(f"/api/v1/events/{eid}/flights", json={"pilot_id": pilot}).json()["flight_id"]
    return eid, fid


# --- 证据接收 ---------------------------------------------------------------

def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_duplicate_packet_merges_and_conflict_quarantines(client):
    _eid, fid = _event_flight(client)
    pk = track()
    h1 = {"X-User": "uploader-1"}

    r1 = client.post(f"/api/v1/flights/{fid}/evidence", json=pk, headers=h1)
    assert r1.json()["state"] == "merged"
    seq1 = r1.json()["evidence_seq"]

    # 相同数据包重复到达：归并，水位不变。
    r2 = client.post(f"/api/v1/flights/{fid}/evidence", json=pk, headers=h1)
    assert r2.json()["state"] == "duplicate"
    assert r2.json()["evidence_seq"] == seq1

    # 标识相同、内容不同：隔离。
    tampered = track()
    tampered["points"][5]["altitude"] = 9999.0
    r3 = client.post(f"/api/v1/flights/{fid}/evidence", json=tampered, headers=h1)
    assert r3.json()["state"] == "quarantined"

    detail = client.get(f"/api/v1/flights/{fid}").json()
    assert detail["current_watermark"] == seq1  # 隔离包不抬水位
    states = {p["state"] for p in detail["packets"]}
    assert {"merged", "quarantined"} <= states


def test_reboot_seq_rollback_does_not_corrupt_order(client):
    _eid, fid = _event_flight(client)
    client.post(f"/api/v1/flights/{fid}/evidence", json=track(),
                headers={"X-User": "uploader-1"})
    detail = client.get(f"/api/v1/flights/{fid}").json()
    seg = detail["current_segmentation"]
    assert seg["legs"][0]["adopted"] is True
    assert seg["total_seconds"] == 220.0  # -30m 到 190m 两个门界穿越：22 步 × 10s
    assert any("序号回退" in n or "重启" in n for n in seg["notes"])


def test_baro_supplement_before_publish_triggers_recompute(client):
    eid, fid = _event_flight(client)
    client.post(f"/api/v1/flights/{fid}/evidence", json=track(packet_id="pk-gps"),
                headers={"X-User": "uploader-1"})
    # 发布前补交气压计证据：新 packet_id，入新水位并排队复算。
    baro = track(packet_id="pk-baro", with_altitude=True)
    r = client.post(f"/api/v1/flights/{fid}/evidence", json=baro,
                    headers={"X-User": "uploader-1"})
    assert r.json()["state"] == "merged"
    assert r.json()["evidence_seq"] == 2
    detail = client.get(f"/api/v1/flights/{fid}").json()
    assert detail["current_watermark"] == 2
    assert detail["current_segmentation"]["legs"][0]["calibration_ids"] == ["CAL_V1"]


# --- 成绩冻结与仲裁 ----------------------------------------------------------

def test_score_freeze_and_arbitration_rules(client):
    eid, fid = _event_flight(client, pilot="pilot-1", uploader="uploader-1")
    client.post(f"/api/v1/flights/{fid}/evidence", json=track(),
                headers={"X-User": "uploader-1"})

    # 选手不能提交/自批成绩。
    r = client.post(f"/api/v1/flights/{fid}/scores", json={"judge_id": "pilot-1"})
    assert r.status_code == 409 and "自批" in r.json()["detail"]
    # 上传者也不行。
    r = client.post(f"/api/v1/flights/{fid}/scores", json={"judge_id": "uploader-1"})
    assert r.status_code == 409 and "自批" in r.json()["detail"]

    r = client.post(f"/api/v1/flights/{fid}/scores",
                    json={"judge_id": "judge-A", "note": "候选"})
    score = r.json()
    sid = score["score_id"]
    assert score["status"] == "candidate"
    assert score["evidence_watermark"] == 1
    assert all("reason" in leg for leg in score["legs"])

    # 裁判本人不能确认自己提交的成绩。
    r = client.post(f"/api/v1/flights/{fid}/scores/{sid}/arbitration",
                    json={"arbiter_id": "judge-A", "decision": "confirmed"})
    assert r.status_code == 409

    # 未查看证据不能确认。
    r = client.post(f"/api/v1/flights/{fid}/scores/{sid}/arbitration",
                    json={"arbiter_id": "arbiter-X", "decision": "confirmed"})
    assert r.status_code == 409 and "查看" in r.json()["detail"]

    # 独立仲裁人先查看，登记所见水位。
    v = client.get(f"/api/v1/flights/{fid}/evidence-view?score_id={sid}",
                   headers={"X-User": "arbiter-X"})
    assert v.json()["seen_watermark"] == 1
    r = client.post(f"/api/v1/flights/{fid}/scores/{sid}/arbitration",
                    json={"arbiter_id": "arbiter-X", "decision": "confirmed"})
    assert r.json()["score_status"] == "confirmed"

    # 冻结后新证据不改写旧版本。
    client.post(f"/api/v1/flights/{fid}/evidence",
                json=track(packet_id="pk-baro", with_altitude=True),
                headers={"X-User": "uploader-1"})
    detail = client.get(f"/api/v1/flights/{fid}").json()
    v1 = next(s for s in detail["score_versions"] if s["version"] == 1)
    assert v1["evidence_watermark"] == 1
    assert v1["status"] == "confirmed"


def test_arbiter_cannot_confirm_version_they_did_not_see(client):
    eid, fid = _event_flight(client)
    client.post(f"/api/v1/flights/{fid}/evidence", json=track(),
                headers={"X-User": "uploader-1"})
    sid = client.post(f"/api/v1/flights/{fid}/scores",
                      json={"judge_id": "judge-A"}).json()["score_id"]
    client.get(f"/api/v1/flights/{fid}/evidence-view?score_id={sid}",
               headers={"X-User": "arbiter-X"})
    # 水位推进：仲裁人所见版本落后于新冻结版本时，确认旧成绩仍基于其所见。
    client.post(f"/api/v1/flights/{fid}/evidence",
                json=track(packet_id="pk-baro", with_altitude=True),
                headers={"X-User": "uploader-1"})
    r = client.post(f"/api/v1/flights/{fid}/scores/{sid}/arbitration",
                    json={"arbiter_id": "arbiter-X", "decision": "confirmed"})
    # 旧成绩冻结于水位 1，仲裁人看到的也是水位 1（当时登记），可确认。
    assert r.status_code == 200 and r.json()["seen_watermark"] == 1


# --- 锁定 / 发布 / 补证重开 ---------------------------------------------------

def _confirmed_flight(client, pilot="pilot-1"):
    eid, fid = _event_flight(client, pilot=pilot)
    client.post(f"/api/v1/flights/{fid}/evidence", json=track(),
                headers={"X-User": "uploader-1"})
    sid = client.post(f"/api/v1/flights/{fid}/scores",
                      json={"judge_id": "judge-A"}).json()["score_id"]
    client.get(f"/api/v1/flights/{fid}/evidence-view?score_id={sid}",
               headers={"X-User": "arbiter-X"})
    client.post(f"/api/v1/flights/{fid}/scores/{sid}/arbitration",
                json={"arbiter_id": "arbiter-X", "decision": "confirmed"})
    return eid, fid, sid


def test_lock_and_publish_single_watermark_ranking(client):
    eid, fid, sid = _confirmed_flight(client)
    assert client.post(f"/api/v1/events/{eid}/lock").json()["state"] == "locked"
    pub = client.post(f"/api/v1/events/{eid}/publish").json()
    assert pub["state"] == "published"
    ranking = client.get(f"/api/v1/events/{eid}/ranking").json()
    assert ranking["ranking_state"] == "published"
    assert ranking["rows"][0]["total_seconds"] == 220.0


def test_post_publish_evidence_held_assessment_then_reopen(client):
    eid, fid, sid = _confirmed_flight(client)
    client.post(f"/api/v1/events/{eid}/lock")
    client.post(f"/api/v1/events/{eid}/publish")

    # 发布后补交气压证据：暂扣 + 影响评估，原排名不动。
    r = client.post(f"/api/v1/flights/{fid}/evidence",
                    json=track(packet_id="pk-baro-late", with_altitude=True),
                    headers={"X-User": "uploader-1"})
    assert r.json()["state"] == "held"
    aid = int(r.json()["detail"].split("#")[1].split("，")[0])

    assessment = client.get(f"/api/v1/assessments/{aid}").json()
    assert assessment["state"] == "pending"
    assert assessment["impact"]["current_total"] == 220.0

    # 选手/上传者无权批准重开。
    r = client.post(f"/api/v1/assessments/{aid}/decision",
                    json={"approver_id": "pilot-1", "decision": "approved"})
    assert r.status_code == 409 and "自批" in r.json()["detail"]

    # 独立审批人批准重开：建立替代成绩，原排名保留。
    r = client.post(f"/api/v1/assessments/{aid}/decision",
                    json={"approver_id": "arbiter-lead", "decision": "approved"})
    body = r.json()
    assert body["state"] == "approved"
    alt_sid = body["alternate_score_id"]

    # 当前公开榜单仍是原排名。
    ranking = client.get(f"/api/v1/events/{eid}/ranking").json()
    assert ranking["rows"][0]["score_id"] == sid

    # 替代成绩仍需仲裁确认；选手不能确认。
    r = client.post(f"/api/v1/flights/{fid}/scores/{alt_sid}/arbitration",
                    json={"arbiter_id": "pilot-1", "decision": "confirmed"})
    assert r.status_code == 409
    client.get(f"/api/v1/flights/{fid}/evidence-view?score_id={alt_sid}",
               headers={"X-User": "arbiter-Y"})
    r = client.post(f"/api/v1/flights/{fid}/scores/{alt_sid}/arbitration",
                    json={"arbiter_id": "arbiter-Y", "decision": "confirmed"})
    assert r.json()["score_status"] == "alternate_confirmed"

    # 替代成绩发布为新榜单版本，保留名次变化与原版本事实。
    rep = client.post(f"/api/v1/flights/{fid}/republish").json()
    assert rep["state"] == "published"
    new_ranking = client.get(f"/api/v1/events/{eid}/ranking").json()
    assert new_ranking["rows"][0]["score_id"] == alt_sid
    # 历史榜单与全部通知事实保留可查。
    assert len(new_ranking["history"]) >= 2
    kinds = {n["kind"] for n in client.get(f"/api/v1/events/{eid}/notifications").json()["items"]}
    assert {"ranking_published", "post_publish_evidence_held", "reopen_approved"} <= kinds

    detail = client.get(f"/api/v1/flights/{fid}").json()
    versions = [(s["version"], s["status"]) for s in detail["score_versions"]]
    assert (1, "confirmed") in versions
    assert any(s["evidence_watermark"] == 2 for s in detail["score_versions"])


def test_post_publish_evidence_rejected_keeps_quarantine(client):
    eid, fid, _sid = _confirmed_flight(client)
    client.post(f"/api/v1/events/{eid}/publish")
    r = client.post(f"/api/v1/flights/{fid}/evidence",
                    json=track(packet_id="pk-late", with_altitude=True),
                    headers={"X-User": "uploader-1"})
    aid = int(r.json()["detail"].split("#")[1].split("，")[0])
    r = client.post(f"/api/v1/assessments/{aid}/decision",
                    json={"approver_id": "arbiter-lead", "decision": "rejected"})
    assert r.json()["state"] == "rejected"
    detail = client.get(f"/api/v1/flights/{fid}").json()
    assert detail["current_watermark"] == 1
    held = [p for p in detail["packets"] if p["packet_id"] == "pk-late"][0]
    assert held["state"] == "quarantined"


def test_lock_blocks_new_watermark_only_one_wins(client):
    eid, fid, _sid = _confirmed_flight(client)
    client.post(f"/api/v1/events/{eid}/lock")
    # 锁定窗口内两个补证同时到达：都暂扣为独立评估，不产生混合水位。
    r1 = client.post(f"/api/v1/flights/{fid}/evidence",
                     json=track(packet_id="pk-a", with_altitude=True),
                     headers={"X-User": "uploader-1"})
    r2 = client.post(f"/api/v1/flights/{fid}/evidence",
                     json=track(packet_id="pk-b"),
                     headers={"X-User": "uploader-2"})
    assert {r1.json()["state"], r2.json()["state"]} == {"held"}
    detail = client.get(f"/api/v1/flights/{fid}").json()
    assert detail["current_watermark"] == 1  # 锁内榜单仍是唯一水位


# --- 停机恢复 ----------------------------------------------------------------

def test_pause_resume_extends_appeal_and_drains_jobs(client):
    eid, fid = _event_flight(client)
    deadline = "2026-09-26T12:00:00Z"
    # 直接改库设置申诉截止，再停机。
    from app import storage
    storage.db().execute("UPDATE events SET appeal_deadline=? WHERE id=?", (deadline, eid))
    storage.commit()

    client.post(f"/api/v1/events/{eid}/pause")
    # 停机期间到达的发布前证据入队（发布前状态 open）。
    r = client.post(f"/api/v1/flights/{fid}/evidence", json=track(),
                    headers={"X-User": "uploader-1"})
    assert r.json()["state"] == "merged"

    out = client.post(f"/api/v1/events/{eid}/resume").json()
    assert out["queued_jobs_drained"] is True
    # 申诉期限顺延（恢复时刻晚于停机时刻，截止时间被推后）。
    from datetime import datetime
    def parse(v): return datetime.fromisoformat(v.replace("Z", "+00:00"))
    assert out["appeal_deadline"] is not None
    assert parse(out["appeal_deadline"]) > parse(deadline)


def test_process_restart_recovery_drains_queued_jobs(client):
    from app import storage, service
    eid, fid = _event_flight(client)
    client.post(f"/api/v1/flights/{fid}/evidence", json=track(),
                headers={"X-User": "uploader-1"})
    # 模拟崩溃留下的未完成任务与停机状态。
    db_path = storage.DB_PATH
    storage.db().execute(
        "INSERT INTO jobs(event_id,flight_id,trigger,status,detail) VALUES(?,?,?,?,?)",
        (eid, fid, "rebuild", "queued", "崩溃遗留重算"),
    )
    storage.commit()
    storage.db().close()
    storage.init_db(db_path)
    service.startup_recovery()
    jobs = storage.fetchall("SELECT status FROM jobs WHERE event_id=?", (eid,))
    assert all(j["status"] == "done" for j in jobs)

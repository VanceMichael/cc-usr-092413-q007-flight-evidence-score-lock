"""领域服务编排：证据水位、成绩冻结、仲裁、榜单锁定/发布/重开。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Optional

from . import storage as st
from .engine import TrajPoint, parse_ts, segment
from .models import ArbitrationIn, AssessmentDecisionIn, EventConfig, EvidencePacketIn, SubmitScoreIn


class DomainError(Exception):
    """可向调用方展示的领域规则错误（4xx）。"""


# --- 基础查询 ---------------------------------------------------------------

def _event(event_id: int):
    row = st.fetchone("SELECT * FROM events WHERE id=?", (event_id,))
    if row is None:
        raise DomainError("赛事不存在")
    return row


def _flight(flight_id: int):
    row = st.fetchone("SELECT * FROM flights WHERE id=?", (flight_id,))
    if row is None:
        raise DomainError("飞行不存在")
    return row


def _config(event_row) -> EventConfig:
    return EventConfig.model_validate(st.loads(event_row["config_json"]))


def _flight_uploaders(flight_id: int) -> set[str]:
    rows = st.fetchall(
        "SELECT DISTINCT uploader_id FROM packets WHERE flight_id=?", (flight_id,)
    )
    return {r["uploader_id"] for r in rows}


def _merged_packets(flight_id: int) -> list:
    return st.fetchall(
        "SELECT * FROM packets WHERE flight_id=? AND state='merged' ORDER BY evidence_seq",
        (flight_id,),
    )


def _current_watermark(flight_id: int) -> int:
    row = st.fetchone(
        "SELECT COALESCE(MAX(evidence_seq),0) AS w FROM packets "
        "WHERE flight_id=? AND state='merged'",
        (flight_id,),
    )
    return int(row["w"])


def _evidence_fingerprint(flight_id: int) -> tuple[int, str]:
    packets = _merged_packets(flight_id)
    h = hashlib.sha256("|".join(p["content_hash"] for p in packets).encode()).hexdigest()
    return (packets[-1]["evidence_seq"] if packets else 0, h)


def _notify(event_id: int, kind: str, payload: dict, flight_id: Optional[int] = None) -> None:
    st.insert(
        "notifications", event_id=event_id, flight_id=flight_id, kind=kind,
        payload_json=st.dumps(payload),
    )


# --- 事件/飞行 --------------------------------------------------------------

def create_event(name: str, config: EventConfig, appeal_deadline: Optional[str]) -> int:
    with st.lock():
        eid = st.insert(
            "events", name=name, config_json=config.model_dump_json(),
            appeal_deadline=appeal_deadline,
        )
        st.commit()
        return eid


def create_flight(event_id: int, pilot_id: str) -> int:
    _event(event_id)
    with st.lock():
        fid = st.insert("flights", event_id=event_id, pilot_id=pilot_id)
        st.commit()
        return fid


# --- 证据接收：归并 / 重复 / 隔离 / 锁定水位暂扣 -----------------------------

def ingest_packet(flight_id: int, packet: EvidencePacketIn, uploader_id: str) -> dict:
    flight = _flight(flight_id)
    event = _event(flight["event_id"])
    canonical = packet.model_dump_json()
    content_hash = hashlib.sha256(canonical.encode()).hexdigest()
    received_at = st.now()

    with st.lock():
        existing = st.fetchall(
            "SELECT * FROM packets WHERE flight_id=? AND packet_id=?",
            (flight_id, packet.packet_id),
        )
        same_hash = next((p for p in existing if p["content_hash"] == content_hash), None)
        if same_hash is not None:
            # 相同数据包重复到达：归并（幂等），不新增点、不动水位。
            result = {
                "packet_id": packet.packet_id, "state": "duplicate",
                "evidence_seq": same_hash["evidence_seq"],
                "detail": "相同数据包重复到达，已按首包归并，水位不变",
            }
            st.commit()
            return result

        if existing:
            # 标识相同但内容不同：隔离，绝不混入既有证据。
            st.insert(
                "packets", packet_id=packet.packet_id, flight_id=flight_id,
                uploader_id=uploader_id, content_hash=content_hash,
                summary=packet.summary, state="quarantined",
                duplicate_of=existing[0]["id"], received_at=received_at,
                payload_json=canonical,
            )
            _notify(flight["event_id"], "evidence_quarantined", {
                "flight_id": flight_id, "packet_id": packet.packet_id,
                "reason": "相同标识但内容哈希不同，已隔离待查",
            }, flight_id)
            st.commit()
            return {"packet_id": packet.packet_id, "state": "quarantined",
                    "detail": "标识相同但内容与既有包不同，已隔离，不进入分段与水位"}

        # 新标识：若榜单已锁定/发布，先暂扣，只允许一个水位胜出。
        if event["status"] in ("locked", "published"):
            pid = st.insert(
                "packets", packet_id=packet.packet_id, flight_id=flight_id,
                uploader_id=uploader_id, content_hash=content_hash,
                summary=packet.summary, state="held",
                duplicate_of=None, received_at=received_at, payload_json=canonical,
            )
            assessment = {
                "flight_id": flight_id, "packet_id": packet.packet_id,
                "current_total": _latest_total(flight_id),
                "note": "发布/锁定后补证：尚未进入证据水位，等待重开审批",
            }
            aid = st.insert(
                "assessments", event_id=flight["event_id"], flight_id=flight_id,
                packet_id=pid, state="pending", impact_json=st.dumps(assessment),
            )
            _notify(flight["event_id"], "post_publish_evidence_held", {
                "flight_id": flight_id, "packet_id": packet.packet_id,
                "assessment_id": aid,
            }, flight_id)
            st.commit()
            return {"packet_id": packet.packet_id, "state": "held",
                    "detail": f"榜单已{event['status']}，补证暂扣并生成影响评估 #{aid}，获准重开后才入水位"}

        seq = _merge_packet(flight_id, uploader_id, packet, content_hash, canonical, received_at)
        st.insert(
            "jobs", event_id=flight["event_id"], flight_id=flight_id,
            trigger="pre_publish_evidence", status="queued",
            detail=f"新证据水位 {seq}，待复算",
        )
        _notify(flight["event_id"], "evidence_merged", {
            "flight_id": flight_id, "packet_id": packet.packet_id, "evidence_seq": seq,
        }, flight_id)
        st.commit()
        _drain_jobs(flight["event_id"])
        return {"packet_id": packet.packet_id, "state": "merged", "evidence_seq": seq,
                "detail": f"已并入证据水位 {seq}，复算任务已排队"}


def _merge_packet(flight_id, uploader_id, packet, content_hash, canonical, received_at) -> int:
    row = st.fetchone(
        "SELECT COALESCE(MAX(evidence_seq),0)+1 AS s FROM packets WHERE flight_id=?",
        (flight_id,),
    )
    seq = int(row["s"])
    pid = st.insert(
        "packets", evidence_seq=seq, packet_id=packet.packet_id, flight_id=flight_id,
        uploader_id=uploader_id, content_hash=content_hash, summary=packet.summary,
        state="merged", duplicate_of=None, received_at=received_at,
        payload_json=canonical,
    )
    for pt in packet.points:
        st.insert(
            "points", flight_id=flight_id, packet_id=pid,
            device_session=pt.device_session, device_seq=pt.device_seq,
            device_time=pt.device_time, received_at=received_at,
            lat=pt.lat, lon=pt.lon, altitude=pt.altitude,
            point_summary=pt.point_summary,
        )
    return seq


def _promote_held_packet(packet_row, packet) -> int:
    """批准重开：把暂扣包就地提升进证据水位（同一行，保留原接收时间）。"""
    flight_id = packet_row["flight_id"]
    row = st.fetchone(
        "SELECT COALESCE(MAX(evidence_seq),0)+1 AS s FROM packets WHERE flight_id=?",
        (flight_id,),
    )
    seq = int(row["s"])
    st.db().execute(
        "UPDATE packets SET state='merged', evidence_seq=? WHERE id=?",
        (seq, packet_row["id"]),
    )
    for pt in packet.points:
        st.insert(
            "points", flight_id=flight_id, packet_id=packet_row["id"],
            device_session=pt.device_session, device_seq=pt.device_seq,
            device_time=pt.device_time, received_at=packet_row["received_at"],
            lat=pt.lat, lon=pt.lon, altitude=pt.altitude,
            point_summary=pt.point_summary,
        )
    return seq

def _traj_points(flight_id: int) -> list[TrajPoint]:
    rows = st.fetchall(
        "SELECT p.* FROM points p JOIN packets k ON p.packet_id=k.id "
        "WHERE p.flight_id=? AND k.state='merged' ORDER BY k.evidence_seq, p.id",
        (flight_id,),
    )
    return [
        TrajPoint(
            device_session=r["device_session"], device_seq=r["device_seq"],
            device_time=parse_ts(r["device_time"]), received_at=r["received_at"],
            lat=r["lat"], lon=r["lon"], altitude=r["altitude"],
            point_summary=r["point_summary"],
        )
        for r in rows
    ]


def recompute(flight_id: int) -> dict:
    flight = _flight(flight_id)
    event = _event(flight["event_id"])
    report = segment(_traj_points(flight_id), _config(event))
    return report.to_dict()


def _latest_total(flight_id: int) -> Optional[float]:
    row = st.fetchone(
        "SELECT total_seconds FROM scores WHERE flight_id=? "
        "ORDER BY version DESC LIMIT 1", (flight_id,),
    )
    return float(row["total_seconds"]) if row and row["total_seconds"] is not None else None


def _drain_jobs(event_id: int) -> None:
    """执行已排队的复算任务（服务恢复后同样调用）。"""
    jobs = st.fetchall(
        "SELECT * FROM jobs WHERE event_id=? AND status='queued' ORDER BY id", (event_id,)
    )
    for job in jobs:
        st.db().execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
        st.commit()
        try:
            if job["flight_id"] is not None:
                recompute(int(job["flight_id"]))
            st.db().execute(
                "UPDATE jobs SET status='done', finished_at=? WHERE id=?",
                (st.now(), job["id"]),
            )
            st.commit()
        except Exception as exc:  # 失败保留队列可重试，不丢任务
            st.db().execute(
                "UPDATE jobs SET status='queued', detail=? WHERE id=?",
                (f"{job['detail'] or ''}; 重试原因: {exc}", job["id"]),
            )
            st.commit()


# --- 候选成绩冻结 ------------------------------------------------------------

def _assert_not_interested_party(user_id: str, flight, judge_id: Optional[str] = None) -> None:
    if user_id == flight["pilot_id"]:
        raise DomainError("选手没有自批权限")
    if user_id in _flight_uploaders(flight["id"]):
        raise DomainError("证据上传者没有自批权限")
    if judge_id is not None and user_id == judge_id:
        raise DomainError("提交成绩的裁判不能独自仲裁确认")


def submit_score(flight_id: int, body: SubmitScoreIn) -> dict:
    flight = _flight(flight_id)
    event = _event(flight["event_id"])
    _assert_not_interested_party(body.judge_id, flight)
    if not _merged_packets(flight_id):
        raise DomainError("没有已归并证据，无法冻结成绩")

    with st.lock():
        watermark, fingerprint = _evidence_fingerprint(flight_id)
        report = recompute(flight_id)
        row = st.fetchone(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM scores WHERE flight_id=?",
            (flight_id,),
        )
        version = int(row["v"])
        sid = st.insert(
            "scores", flight_id=flight_id, version=version, status="candidate",
            judge_id=body.judge_id, evidence_watermark=watermark,
            total_seconds=report["total_seconds"], leg_json=st.dumps(report),
            note=body.note,
        )
        # 冻结快照同时固化证据指纹，事后证据变化不改写本版本。
        st.db().execute(
            "UPDATE scores SET note=COALESCE(note,'')||? WHERE id=?",
            (f" [evidence_fingerprint={fingerprint[:12]}]", sid),
        )
        _notify(event["id"], "score_frozen", {
            "flight_id": flight_id, "score_id": sid, "version": version,
            "watermark": watermark,
        }, flight_id)
        st.commit()
        return {"score_id": sid, "version": version, "status": "candidate",
                "evidence_watermark": watermark, **report}


# --- 仲裁 --------------------------------------------------------------------

def view_evidence(flight_id: int, score_id: int, arbiter_id: str) -> dict:
    flight = _flight(flight_id)
    score = _score(score_id, flight_id)
    _assert_not_interested_party(arbiter_id, flight, score["judge_id"])
    with st.lock():
        watermark, fingerprint = _evidence_fingerprint(flight_id)
        st.db().execute(
            "INSERT INTO arbiter_views(score_id, arbiter_id, seen_watermark, seen_fingerprint) "
            "VALUES(?,?,?,?) ON CONFLICT(score_id, arbiter_id) DO UPDATE SET "
            "seen_watermark=excluded.seen_watermark, seen_fingerprint=excluded.seen_fingerprint, "
            "viewed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (score_id, arbiter_id, watermark, fingerprint),
        )
        st.commit()
        return {"score_id": score_id, "arbiter_id": arbiter_id,
                "seen_watermark": watermark, "seen_fingerprint": fingerprint,
                "score_watermark": score["evidence_watermark"],
                "report": st.loads(score["leg_json"])}


def _score(score_id: int, flight_id: int):
    row = st.fetchone("SELECT * FROM scores WHERE id=? AND flight_id=?",
                      (score_id, flight_id))
    if row is None:
        raise DomainError("成绩版本不存在")
    return row


def arbitrate(flight_id: int, score_id: int, body: ArbitrationIn) -> dict:
    flight = _flight(flight_id)
    score = _score(score_id, flight_id)
    _assert_not_interested_party(body.arbiter_id, flight, score["judge_id"])
    view = st.fetchone(
        "SELECT * FROM arbiter_views WHERE score_id=? AND arbiter_id=?",
        (score_id, body.arbiter_id),
    )
    if view is None:
        raise DomainError("仲裁人须先查看并登记证据版本，才能确认")
    declared = body.seen_watermark if body.seen_watermark is not None else view["seen_watermark"]
    if declared != view["seen_watermark"]:
        raise DomainError("声明水位与查看登记水位不一致")
    if view["seen_watermark"] != score["evidence_watermark"]:
        raise DomainError(
            f"只能确认自己看到的证据版本：所见水位 {view['seen_watermark']}，"
            f"成绩冻结水位 {score['evidence_watermark']}"
        )

    with st.lock():
        dup = st.fetchone("SELECT id FROM arbitrations WHERE score_id=? AND arbiter_id=?",
                          (score_id, body.arbiter_id))
        if dup:
            raise DomainError("同一仲裁人对该成绩已作出裁决")
        aid = st.insert(
            "arbitrations", score_id=score_id, arbiter_id=body.arbiter_id,
            seen_watermark=view["seen_watermark"], decision=body.decision,
            comment=body.comment,
        )
        confirmations = st.fetchall(
            "SELECT decision FROM arbitrations WHERE score_id=?", (score_id,)
        )
        new_status = score["status"]
        if body.decision == "confirmed":
            new_status = "confirmed" if score["status"] == "candidate" else (
                "alternate_confirmed" if score["status"] == "alternate" else score["status"]
            )
        elif any(r["decision"] == "rejected" for r in confirmations):
            new_status = "rejected"
        st.db().execute("UPDATE scores SET status=? WHERE id=?", (new_status, score_id))
        event = _event(flight["event_id"])
        _notify(event["id"], "arbitration_decided", {
            "flight_id": flight_id, "score_id": score_id,
            "arbiter": body.arbiter_id, "decision": body.decision,
            "seen_watermark": view["seen_watermark"], "new_status": new_status,
        }, flight_id)
        st.commit()
        return {"arbitration_id": aid, "score_status": new_status,
                "seen_watermark": view["seen_watermark"]}


# --- 榜单：锁定/发布（单一水位事务） ------------------------------------------

def _eligible_scores(event_id: int) -> list:
    rows = st.fetchall(
        "SELECT s.* FROM scores s JOIN flights f ON s.flight_id=f.id "
        "WHERE f.event_id=? AND s.status IN ('confirmed','alternate_confirmed') "
        "ORDER BY s.flight_id, s.version DESC", (event_id,)
    )
    best: dict[int, dict] = {}
    for r in rows:  # 每个飞行取最新生效版本
        best.setdefault(r["flight_id"], r)
    return list(best.values())


def _build_ranking(event_id: int, state: str) -> int:
    scores = _eligible_scores(event_id)
    scored = [s for s in scores if s["total_seconds"] is not None]
    scored.sort(key=lambda s: float(s["total_seconds"]))
    watermark = max((int(s["evidence_watermark"]) for s in scores), default=0)

    prev_pub = st.fetchone(
        "SELECT id FROM rankings WHERE event_id=? AND state='published' "
        "ORDER BY published_at DESC, id DESC LIMIT 1", (event_id,)
    )
    prev_rank: dict[int, int] = {}
    if prev_pub:
        for r in st.fetchall("SELECT * FROM ranking_rows WHERE ranking_id=?", (prev_pub["id"],)):
            prev_rank[r["flight_id"]] = r["rank"]

    rid = st.insert(
        "rankings", event_id=event_id, watermark=watermark, state=state,
        published_at=st.now() if state == "published" else None,
    )
    for rank, s in enumerate(scored, start=1):
        old = prev_rank.get(s["flight_id"])
        st.insert(
            "ranking_rows", ranking_id=rid, flight_id=s["flight_id"], score_id=s["id"],
            rank=rank, total_seconds=s["total_seconds"], prev_rank=old,
            rank_change=(old - rank) if old is not None else None,
        )
    return rid


def lock_event(event_id: int) -> dict:
    _event(event_id)
    with st.lock():
        event = _event(event_id)
        if event["status"] == "published":
            raise DomainError("已发布的榜单不能回退为锁定")
        rid = _build_ranking(event_id, "locked")
        st.db().execute("UPDATE events SET status='locked' WHERE id=?", (event_id,))
        st.db().execute("UPDATE rankings SET state='superseded' WHERE event_id=? AND state='locked' AND id<>?",
                        (event_id, rid))
        _notify(event_id, "ranking_locked", {"ranking_id": rid}, None)
        st.commit()
        return {"ranking_id": rid, "state": "locked"}


def publish_event(event_id: int) -> dict:
    with st.lock():
        rid = _build_ranking(event_id, "published")
        st.db().execute("UPDATE events SET status='published' WHERE id=?", (event_id,))
        # 旧的锁定/发布快照保留为历史事实，但不再是当前榜单。
        st.db().execute(
            "UPDATE rankings SET state='superseded' WHERE event_id=? AND id<>?",
            (event_id, rid),
        )
        _notify(event_id, "ranking_published", {"ranking_id": rid}, None)
        st.commit()
        return {"ranking_id": rid, "state": "published"}


# --- 发布后补证：影响评估与重开 ----------------------------------------------

def impact_preview(event_id: int, flight_id: int, held_packet_row) -> dict:
    """把暂扣包临时并入内存复算，不落盘，得到影响预览。"""
    from .models import EvidencePacketIn
    event = _event(event_id)
    flight = _flight(flight_id)
    packet = EvidencePacketIn.model_validate(st.loads(held_packet_row["payload_json"]))
    pts = _traj_points(flight_id)
    for pt in packet.points:
        pts.append(TrajPoint(
            device_session=pt.device_session, device_seq=pt.device_seq,
            device_time=parse_ts(pt.device_time), received_at=held_packet_row["received_at"],
            lat=pt.lat, lon=pt.lon, altitude=pt.altitude, point_summary=pt.point_summary,
        ))
    trial = segment(pts, _config(event)).to_dict()
    return {
        "current_total": _latest_total(flight_id),
        "projected_total": trial["total_seconds"],
        "legs": trial["legs"],
        "notes": trial["notes"],
    }


def decide_assessment(assessment_id: int, body: AssessmentDecisionIn) -> dict:
    row = st.fetchone("SELECT * FROM assessments WHERE id=?", (assessment_id,))
    if row is None:
        raise DomainError("影响评估不存在")
    if row["state"] != "pending":
        raise DomainError("该评估已决")
    flight = _flight(row["flight_id"])
    # 选手与上传者无权批准重开。
    _assert_not_interested_party(body.approver_id, flight)

    with st.lock():
        if body.decision == "rejected":
            st.db().execute(
                "UPDATE assessments SET state='rejected', decided_by=?, decided_at=? WHERE id=?",
                (body.approver_id, st.now(), assessment_id),
            )
            st.db().execute("UPDATE packets SET state='quarantined' WHERE id=?", (row["packet_id"],))
            _notify(row["event_id"], "reopen_rejected", {
                "flight_id": row["flight_id"], "assessment_id": assessment_id,
                "by": body.approver_id,
            }, row["flight_id"])
            st.commit()
            return {"state": "rejected"}

        impact = impact_preview(row["event_id"], row["flight_id"],
                                st.fetchone("SELECT * FROM packets WHERE id=?", (row["packet_id"],)))
        packet_row = st.fetchone("SELECT * FROM packets WHERE id=?", (row["packet_id"],))
        packet = EvidencePacketIn.model_validate(st.loads(packet_row["payload_json"]))
        seq = _promote_held_packet(packet_row, packet)

        report = recompute(row["flight_id"])
        versions = st.fetchone(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM scores WHERE flight_id=?",
            (row["flight_id"],),
        )
        version = int(versions["v"])
        sid = st.insert(
            "scores", flight_id=row["flight_id"], version=version, status="alternate",
            judge_id=body.approver_id, evidence_watermark=seq,
            total_seconds=report["total_seconds"], leg_json=st.dumps(report),
            note=f"重开替代成绩（评估 #{assessment_id}）",
        )
        st.db().execute(
            "UPDATE assessments SET state='approved', decided_by=?, decided_at=?, "
            "impact_json=? WHERE id=?",
            (body.approver_id, st.now(), st.dumps(impact), assessment_id),
        )
        # 原排名与既有通知原样保留，仅新增事实。
        _notify(row["event_id"], "reopen_approved", {
            "flight_id": row["flight_id"], "assessment_id": assessment_id,
            "alternate_score_id": sid, "watermark": seq,
            "original_ranking_preserved": True,
        }, row["flight_id"])
        st.commit()
        return {"state": "approved", "evidence_seq": seq,
                "alternate_score_id": sid, "impact": impact}


def confirm_alternate_and_republish(event_id: int, flight_id: int) -> dict:
    """替代成绩经仲裁确认后，发布新榜单版本（原榜单与通知保留）。"""
    with st.lock():
        alt = st.fetchone(
            "SELECT * FROM scores WHERE flight_id=? AND status='alternate_confirmed' "
            "ORDER BY version DESC LIMIT 1", (flight_id,)
        )
        if alt is None:
            raise DomainError("没有已确认的替代成绩")
        result = publish_event(event_id)
        result["flight_id"] = flight_id
        result["score_id"] = alt["id"]
        return result


# --- 查询：航段理由 / 成绩版本 / 排名变化 ------------------------------------

def flight_detail(flight_id: int) -> dict:
    flight = _flight(flight_id)
    packets = st.fetchall("SELECT id,packet_id,evidence_seq,state,summary,received_at,content_hash "
                          "FROM packets WHERE flight_id=? ORDER BY COALESCE(evidence_seq,999999), id",
                          (flight_id,))
    scores = st.fetchall("SELECT * FROM scores WHERE flight_id=? ORDER BY version", (flight_id,))
    report = recompute(flight_id)
    return {
        "flight_id": flight_id, "pilot_id": flight["pilot_id"],
        "current_watermark": _current_watermark(flight_id),
        "packets": [dict(p) for p in packets],
        "current_segmentation": report,
        "score_versions": [
            {
                "score_id": s["id"], "version": s["version"], "status": s["status"],
                "judge_id": s["judge_id"], "evidence_watermark": s["evidence_watermark"],
                "total_seconds": s["total_seconds"], "note": s["note"],
                "legs": st.loads(s["leg_json"])["legs"],
                "arbitrations": [
                    dict(a) for a in st.fetchall(
                        "SELECT arbiter_id,decision,seen_watermark,comment FROM arbitrations "
                        "WHERE score_id=?", (s["id"],))
                ],
            }
            for s in scores
        ],
    }


def ranking_detail(event_id: int) -> dict:
    event = _event(event_id)
    row = st.fetchone(
        "SELECT * FROM rankings WHERE event_id=? AND state='published' "
        "ORDER BY published_at DESC, id DESC LIMIT 1", (event_id,)
    )
    locked = st.fetchone(
        "SELECT * FROM rankings WHERE event_id=? AND state='locked' ORDER BY id DESC LIMIT 1",
        (event_id,),
    )
    chosen = row or locked
    history = [
        {"ranking_id": r["id"], "state": r["state"], "watermark": r["watermark"],
         "published_at": r["published_at"], "created_at": r["created_at"]}
        for r in st.fetchall(
            "SELECT id,state,watermark,published_at,created_at FROM rankings "
            "WHERE event_id=? ORDER BY id", (event_id,))
    ]
    if chosen is None:
        return {"event_id": event_id, "status": event["status"], "ranking": None,
                "history": history}
    rows = st.fetchall("SELECT * FROM ranking_rows WHERE ranking_id=? ORDER BY rank", (chosen["id"],))
    return {
        "event_id": event_id, "event_status": event["status"],
        "ranking_id": chosen["id"], "ranking_state": chosen["state"],
        "evidence_watermark": chosen["watermark"],
        "history": history,
        "rows": [
            {
                "rank": r["rank"], "flight_id": r["flight_id"], "score_id": r["score_id"],
                "total_seconds": r["total_seconds"], "previous_rank": r["prev_rank"],
                "rank_change": r["rank_change"],
            }
            for r in rows
        ],
    }


# --- 停机/恢复：申诉期限顺延与队列续跑 ---------------------------------------

def pause_event(event_id: int) -> dict:
    _event(event_id)
    with st.lock():
        st.db().execute("UPDATE events SET downtime_start=? WHERE id=? AND downtime_start IS NULL",
                        (st.now(), event_id))
        st.commit()
        return {"state": "paused", "downtime_start": _event(event_id)["downtime_start"]}


def resume_event(event_id: int) -> dict:
    """恢复：申诉期限按停机时长顺延，已排队重算继续执行。"""
    with st.lock():
        event = _event(event_id)
        extended = None
        if event["downtime_start"]:
            duration = parse_ts(st.now()) - parse_ts(event["downtime_start"])
            if event["appeal_deadline"]:
                new_deadline = (parse_ts(event["appeal_deadline"]) + duration).isoformat()
                st.db().execute("UPDATE events SET appeal_deadline=? WHERE id=?",
                                (new_deadline, event_id))
                extended = new_deadline
            st.db().execute("UPDATE events SET downtime_start=NULL WHERE id=?", (event_id,))
        st.commit()
        _drain_jobs(event_id)
        return {"state": "resumed", "appeal_deadline": extended,
                "queued_jobs_drained": True}


def startup_recovery() -> None:
    """进程启动：所有处于停机态的赛事顺延申诉期并续跑队列。"""
    for e in st.fetchall("SELECT id FROM events WHERE downtime_start IS NOT NULL"):
        try:
            resume_event(int(e["id"]))
        except DomainError:
            pass
    for e in st.fetchall("SELECT DISTINCT event_id FROM jobs WHERE status='queued'"):
        _drain_jobs(int(e["event_id"]))

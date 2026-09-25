"""竞速飞行证据补齐与成绩锁定的核心业务服务。

关键语义:
- 证据水位(waterline)是全局单调序号,任何改变计算输入的写事务都会推进它;
  分段、成绩、排名、发布都记录自己使用的水位,可解释、可复算。
- 相同内容重复到达归并;标识相同内容不同先隔离,裁决前不参与计算。
- 候选成绩提交即冻结(含采用/排除航段及理由);独立仲裁人只对与自己
  所见一致的证据版本确认;选手与上传者没有自批权限。
- 发布前新证据触发重算;发布后补证先生成影响评估,获准重开才建立替代
  成绩,原排名与通知事实保留。
- 重算任务持久化在 jobs 表,服务恢复后续跑;申诉期限是绝对时间,天然续存。
"""
from __future__ import annotations

import functools
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .segmentation import Gate, NoFlyZone, TrackPoint, segment_flight
from .storage import Store
from .util import canonical_hash, iso, parse_ts


def _locked(fn):
    """查询方法与写事务共用同一把可重入锁,保证单连接下的读写串行。"""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.store.lock:
            return fn(self, *args, **kwargs)
    return wrapper


class DomainError(Exception):
    status = 400


class NotFound(DomainError):
    status = 404


class Conflict(DomainError):
    status = 409


class Forbidden(DomainError):
    status = 403


class Unprocessable(DomainError):
    status = 422


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _point_hash(point: dict) -> str:
    return canonical_hash({
        "point_id": point["point_id"],
        "device_id": point["device_id"],
        "device_session_id": point["device_session_id"],
        "device_seq": point["device_seq"],
        "device_time": point["device_time"],
        "lat": point["lat"],
        "lon": point["lon"],
        "altitude_m": point["altitude_m"],
        "digest": point["digest"],
    })


def _rank_key(candidate: dict) -> tuple:
    # 完整赛程优先,其余按总用时;再按提交先后与 id 保证确定性。
    return (0 if candidate["complete"] else 1, candidate["total_time_s"],
            candidate["created_at"], candidate["score_id"])


class EvidenceService:
    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        quorum: int = 2,
        appeal_window_s: float = 72 * 3600,
        auto_drain: bool = True,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.store = Store(db_path)
        self.quorum = quorum
        self.appeal_window_s = appeal_window_s
        self.auto_drain = auto_drain
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    # ---------- 基础 ----------

    def now(self) -> datetime:
        return self.now_fn()

    def _waterline(self, conn) -> int:
        return int(conn.execute("SELECT value FROM meta WHERE key='waterline'").fetchone()["value"])

    def _bump_waterline(self, conn) -> int:
        wl = self._waterline(conn) + 1
        conn.execute("UPDATE meta SET value=? WHERE key='waterline'", (str(wl),))
        return wl

    def _enqueue(self, conn, job_type: str, payload: dict) -> str:
        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        dup = conn.execute(
            "SELECT job_id FROM jobs WHERE type=? AND payload_json=? AND status='queued'",
            (job_type, payload_json),
        ).fetchone()
        if dup:
            return dup["job_id"]
        job_id = _id("job")
        conn.execute(
            "INSERT INTO jobs(job_id, type, payload_json, status, created_at) VALUES (?,?,?,?,?)",
            (job_id, job_type, payload_json, "queued", iso(self.now())),
        )
        return job_id

    def _enqueue_for_event(self, conn, event_id: str, trigger_packet_id: str | None = None) -> None:
        published = conn.execute(
            "SELECT 1 FROM publications WHERE event_id=? LIMIT 1", (event_id,)
        ).fetchone()
        open_reopen = conn.execute(
            "SELECT 1 FROM reopens WHERE event_id=? AND status='open' LIMIT 1", (event_id,)
        ).fetchone()
        if (not published) or open_reopen:
            self._enqueue(conn, "recompute_event", {"event_id": event_id})
        else:
            self._enqueue(conn, "impact_assessment",
                          {"event_id": event_id, "trigger_packet_id": trigger_packet_id})

    # ---------- 赛道与校准配置 ----------

    def upsert_gates(self, event_id: str, gates: list[dict]) -> dict:
        with self.store.txn() as conn:
            for g in gates:
                conn.execute(
                    """INSERT INTO gates(gate_id, event_id, ord, kind, lat, lon, radius_m)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(gate_id) DO UPDATE SET
                         event_id=excluded.event_id, ord=excluded.ord, kind=excluded.kind,
                         lat=excluded.lat, lon=excluded.lon, radius_m=excluded.radius_m""",
                    (g["gate_id"], event_id, g["ord"], g["kind"], g["lat"], g["lon"], g["radius_m"]),
                )
            self._bump_waterline(conn)
            self._enqueue_for_event(conn, event_id)
        self._maybe_drain()
        return {"event_id": event_id, "gates": len(gates)}

    def upsert_zones(self, event_id: str, zones: list[dict]) -> dict:
        with self.store.txn() as conn:
            for z in zones:
                conn.execute(
                    """INSERT INTO no_fly_zones(zone_id, event_id, lat, lon, radius_m, ceiling_m)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(zone_id) DO UPDATE SET
                         event_id=excluded.event_id, lat=excluded.lat, lon=excluded.lon,
                         radius_m=excluded.radius_m, ceiling_m=excluded.ceiling_m""",
                    (z["zone_id"], event_id, z["lat"], z["lon"], z["radius_m"], z["ceiling_m"]),
                )
            self._bump_waterline(conn)
            self._enqueue_for_event(conn, event_id)
        self._maybe_drain()
        return {"event_id": event_id, "zones": len(zones)}

    def add_calibration(self, event_id: str, data: dict) -> dict:
        valid_from = iso(parse_ts(data["valid_from"]))
        with self.store.txn() as conn:
            conn.execute(
                """INSERT INTO calibrations(calibration_id, event_id, version, valid_from, altitude_offset_m, note)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(calibration_id) DO UPDATE SET
                     event_id=excluded.event_id, version=excluded.version, valid_from=excluded.valid_from,
                     altitude_offset_m=excluded.altitude_offset_m, note=excluded.note""",
                (data["calibration_id"], event_id, data["version"], valid_from,
                 data["altitude_offset_m"], data.get("note")),
            )
            self._bump_waterline(conn)
            self._enqueue_for_event(conn, event_id)
        self._maybe_drain()
        return {"calibration_id": data["calibration_id"], "version": data["version"]}

    # ---------- 证据接收:归并与隔离 ----------

    def ingest_packet(self, uploader_id: str, data: dict) -> dict:
        now_iso = iso(self.now())
        points = data["points"]
        if not points:
            raise Unprocessable("数据包必须至少包含一个轨迹点")
        normalized = []
        for p in points:
            try:
                device_time = iso(parse_ts(p["device_time"]))
            except ValueError as exc:
                raise Unprocessable(f"轨迹点 {p.get('point_id')!r} 设备时间无效: {exc}") from exc
            normalized.append({**p, "device_time": device_time})
        payload_hash = canonical_hash({
            "packet_id": data["packet_id"],
            "flight_id": data["flight_id"],
            "event_id": data["event_id"],
            "player_id": data["player_id"],
            "points": sorted(_point_hash(p) for p in normalized),
        })

        with self.store.txn() as conn:
            existing_packet = conn.execute(
                "SELECT * FROM packets WHERE packet_id=?", (data["packet_id"],)
            ).fetchone()
            if existing_packet:
                if existing_packet["payload_hash"] == payload_hash:
                    return {
                        "packet_id": data["packet_id"],
                        "idempotent": True,
                        "accepted": existing_packet["accepted"],
                        "duplicates": existing_packet["duplicates"],
                        "quarantined": existing_packet["quarantined"],
                        "waterline": existing_packet["waterline"],
                    }
                raise Conflict(f"数据包 {data['packet_id']} 已存在但内容不同,拒绝覆盖")

            flight = conn.execute(
                "SELECT * FROM flights WHERE flight_id=?", (data["flight_id"],)
            ).fetchone()
            if flight is None:
                conn.execute(
                    "INSERT INTO flights(flight_id, event_id, player_id, created_at) VALUES (?,?,?,?)",
                    (data["flight_id"], data["event_id"], data["player_id"], now_iso),
                )
            elif flight["event_id"] != data["event_id"] or flight["player_id"] != data["player_id"]:
                raise Conflict(f"航班 {data['flight_id']} 已登记在其他赛事/选手名下")

            accepted = duplicates = quarantined = 0
            changed = False
            for p in normalized:
                ch = _point_hash(p)
                row = conn.execute(
                    "SELECT * FROM points WHERE flight_id=? AND point_id=?",
                    (data["flight_id"], p["point_id"]),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """INSERT INTO points(flight_id, point_id, packet_id, device_id, device_session_id,
                             device_seq, device_time, received_at, lat, lon, altitude_m, digest,
                             content_hash, status)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'accepted')""",
                        (data["flight_id"], p["point_id"], data["packet_id"], p["device_id"],
                         p["device_session_id"], p["device_seq"], p["device_time"], now_iso,
                         p["lat"], p["lon"], p["altitude_m"], p["digest"], ch),
                    )
                    accepted += 1
                    changed = True
                elif row["content_hash"] == ch:
                    # 相同数据重复到达:归并,不改变证据集合。
                    conn.execute(
                        "UPDATE points SET duplicate_count=duplicate_count+1 WHERE flight_id=? AND point_id=?",
                        (data["flight_id"], p["point_id"]),
                    )
                    duplicates += 1
                else:
                    # 标识相同但内容不同:先隔离,裁决前不参与计算。
                    already = conn.execute(
                        """SELECT 1 FROM quarantine WHERE flight_id=? AND point_id=?
                             AND incoming_hash=? AND status='pending' LIMIT 1""",
                        (data["flight_id"], p["point_id"], ch),
                    ).fetchone()
                    if already:
                        duplicates += 1
                        continue
                    if row["status"] == "accepted":
                        conn.execute(
                            "UPDATE points SET status='quarantined' WHERE flight_id=? AND point_id=?",
                            (data["flight_id"], p["point_id"]),
                        )
                        changed = True
                    existing_payload = {
                        "point_id": row["point_id"], "device_id": row["device_id"],
                        "device_session_id": row["device_session_id"], "device_seq": row["device_seq"],
                        "device_time": row["device_time"], "lat": row["lat"], "lon": row["lon"],
                        "altitude_m": row["altitude_m"], "digest": row["digest"],
                    }
                    conn.execute(
                        """INSERT INTO quarantine(quarantine_id, flight_id, point_id, existing_payload,
                             incoming_payload, incoming_hash, incoming_packet_id, status, created_at)
                           VALUES (?,?,?,?,?,?,?, 'pending', ?)""",
                        (_id("q"), data["flight_id"], p["point_id"], json.dumps(existing_payload),
                         json.dumps(p), ch, data["packet_id"], now_iso),
                    )
                    quarantined += 1

            waterline = None
            if changed:
                waterline = self._bump_waterline(conn)
                conn.execute(
                    "UPDATE flights SET evidence_waterline=? WHERE flight_id=?",
                    (waterline, data["flight_id"]),
                )
                self._enqueue_for_event(conn, data["event_id"], trigger_packet_id=data["packet_id"])
            conn.execute(
                """INSERT INTO packets(packet_id, flight_id, uploader_id, payload_hash, received_at,
                     accepted, duplicates, quarantined, waterline)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (data["packet_id"], data["flight_id"], uploader_id, payload_hash, now_iso,
                 accepted, duplicates, quarantined, waterline),
            )

        self._maybe_drain()
        return {
            "packet_id": data["packet_id"],
            "idempotent": False,
            "accepted": accepted,
            "duplicates": duplicates,
            "quarantined": quarantined,
            "waterline": waterline,
        }

    def resolve_quarantine(self, quarantine_id: str, resolution: str, resolved_by: str) -> dict:
        with self.store.txn() as conn:
            q = conn.execute(
                "SELECT * FROM quarantine WHERE quarantine_id=?", (quarantine_id,)
            ).fetchone()
            if q is None:
                raise NotFound(f"隔离记录 {quarantine_id} 不存在")
            if q["status"] != "pending":
                raise Conflict(f"隔离记录 {quarantine_id} 已裁决")
            if resolution == "keep_incoming":
                p = json.loads(q["incoming_payload"])
                conn.execute(
                    """UPDATE points SET packet_id=?, device_id=?, device_session_id=?, device_seq=?,
                         device_time=?, lat=?, lon=?, altitude_m=?, digest=?, content_hash=?, status='accepted'
                       WHERE flight_id=? AND point_id=?""",
                    (q["incoming_packet_id"], p["device_id"], p["device_session_id"], p["device_seq"],
                     p["device_time"], p["lat"], p["lon"], p["altitude_m"], p["digest"],
                     q["incoming_hash"], q["flight_id"], q["point_id"]),
                )
            else:
                conn.execute(
                    "UPDATE points SET status='accepted' WHERE flight_id=? AND point_id=?",
                    (q["flight_id"], q["point_id"]),
                )
            conn.execute(
                "UPDATE quarantine SET status='resolved', resolution=?, resolved_at=?, resolved_by=? WHERE quarantine_id=?",
                (resolution, iso(self.now()), resolved_by, quarantine_id),
            )
            waterline = self._bump_waterline(conn)
            flight = conn.execute(
                "SELECT * FROM flights WHERE flight_id=?", (q["flight_id"],)
            ).fetchone()
            conn.execute(
                "UPDATE flights SET evidence_waterline=? WHERE flight_id=?",
                (waterline, q["flight_id"]),
            )
            self._enqueue_for_event(conn, flight["event_id"])
        self._maybe_drain()
        return {"quarantine_id": quarantine_id, "resolution": resolution, "waterline": waterline}

    @_locked
    def list_quarantine(self, flight_id: str | None = None) -> dict:
        sql = "SELECT * FROM quarantine"
        args: tuple = ()
        if flight_id:
            sql += " WHERE flight_id=?"
            args = (flight_id,)
        sql += " ORDER BY created_at"
        rows = self.store.conn.execute(sql, args).fetchall()
        return {"items": [dict(r) | {"existing_payload": json.loads(r["existing_payload"]),
                                     "incoming_payload": json.loads(r["incoming_payload"])}
                          for r in rows]}

    # ---------- 分段 ----------

    def _segment_stale_flights(self, conn, event_id: str, waterline: int) -> None:
        rows = conn.execute(
            """SELECT f.flight_id FROM flights f
                 WHERE f.event_id=? AND f.evidence_waterline > COALESCE(
                   (SELECT MAX(waterline) FROM segmentation_runs r WHERE r.flight_id=f.flight_id), -1)""",
            (event_id,),
        ).fetchall()
        for r in rows:
            self._segment_flight(conn, r["flight_id"], waterline)

    def _segment_flight(self, conn, flight_id: str, waterline: int) -> str:
        flight = conn.execute("SELECT * FROM flights WHERE flight_id=?", (flight_id,)).fetchone()
        rows = conn.execute(
            "SELECT * FROM points WHERE flight_id=? AND status='accepted'", (flight_id,)
        ).fetchall()
        points = [
            TrackPoint(parse_ts(r["device_time"]), r["device_session_id"], r["device_seq"],
                   r["lat"], r["lon"], r["altitude_m"])
            for r in rows
        ]
        gates = [
            Gate(g["gate_id"], g["ord"], g["kind"], g["lat"], g["lon"], g["radius_m"])
            for g in conn.execute("SELECT * FROM gates WHERE event_id=?", (flight["event_id"],))
        ]
        zones = [
            NoFlyZone(z["zone_id"], z["lat"], z["lon"], z["radius_m"], z["ceiling_m"])
            for z in conn.execute("SELECT * FROM no_fly_zones WHERE event_id=?", (flight["event_id"],))
        ]
        # 当时的校准版本:按飞行首个轨迹点的设备时间选取生效版本。
        start_time = min((p.device_time for p in points), default=None)
        calibration_version, offset = "uncalibrated", 0.0
        if start_time is not None:
            cal = conn.execute(
                """SELECT * FROM calibrations WHERE event_id=? AND valid_from<=?
                     ORDER BY valid_from DESC LIMIT 1""",
                (flight["event_id"], iso(start_time)),
            ).fetchone()
            if cal:
                calibration_version, offset = cal["version"], cal["altitude_offset_m"]

        result = segment_flight(points, gates, zones, offset)
        run_id = _id("seg")
        conn.execute(
            """INSERT INTO segmentation_runs(segmentation_id, flight_id, waterline, calibration_version,
                 altitude_offset_m, point_count, complete, events_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, flight_id, waterline, calibration_version, offset, result["point_count"],
             1 if result["complete"] else 0, json.dumps(result["events"], ensure_ascii=False),
             iso(self.now())),
        )
        for leg in result["legs"]:
            conn.execute(
                """INSERT INTO legs(leg_id, segmentation_id, flight_id, ord, from_gate_id, to_gate_id,
                     entered_at, exited_at, duration_s, status, reason, point_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_id("leg"), run_id, flight_id, leg["ord"], leg["from_gate_id"], leg["to_gate_id"],
                 leg["entered_at"], leg["exited_at"], leg["duration_s"], leg["status"],
                 leg["reason"], leg["point_count"]),
            )
        return run_id

    # ---------- 成绩:冻结与确认 ----------

    def submit_score(self, referee_id: str, flight_id: str, segmentation_id: str | None,
                     decisions: list[dict]) -> dict:
        with self.store.txn() as conn:
            flight = conn.execute("SELECT * FROM flights WHERE flight_id=?", (flight_id,)).fetchone()
            if flight is None:
                raise NotFound(f"航班 {flight_id} 不存在")
            if segmentation_id:
                run = conn.execute(
                    "SELECT * FROM segmentation_runs WHERE segmentation_id=? AND flight_id=?",
                    (segmentation_id, flight_id),
                ).fetchone()
            else:
                run = conn.execute(
                    """SELECT * FROM segmentation_runs WHERE flight_id=?
                         ORDER BY waterline DESC, created_at DESC LIMIT 1""",
                    (flight_id,),
                ).fetchone()
            if run is None:
                raise Conflict(f"航班 {flight_id} 尚无可用分段,无法提交成绩")
            legs = conn.execute(
                "SELECT * FROM legs WHERE segmentation_id=? ORDER BY ord", (run["segmentation_id"],)
            ).fetchall()
            if not legs:
                raise Unprocessable("该分段没有任何航段,无法提交成绩")

            leg_by_id = {l["leg_id"]: l for l in legs}
            decision_by_leg = {d["leg_id"]: d for d in decisions}
            missing = [lid for lid in leg_by_id if lid not in decision_by_leg]
            extra = [lid for lid in decision_by_leg if lid not in leg_by_id]
            if missing or extra:
                raise Unprocessable(f"航段裁决必须恰好覆盖分段全部航段;缺少 {missing},多余 {extra}")

            total = 0.0
            adopted = 0
            for d in decisions:
                leg = leg_by_id[d["leg_id"]]
                if d["decision"] == "adopted":
                    if leg["duration_s"] is None:
                        raise Unprocessable(f"航段 {d['leg_id']} 未完成,不能采用")
                    total += leg["duration_s"]
                    adopted += 1
                elif d["decision"] == "excluded" and not (d.get("reason") or "").strip():
                    raise Unprocessable(f"排除航段 {d['leg_id']} 必须填写理由")

            version_row = conn.execute(
                "SELECT MAX(version) AS v FROM scores WHERE flight_id=?", (flight_id,)
            ).fetchone()
            version = (version_row["v"] or 0) + 1
            prev = conn.execute(
                "SELECT score_id FROM scores WHERE flight_id=? ORDER BY version DESC LIMIT 1",
                (flight_id,),
            ).fetchone()
            score_id = _id("score")
            complete = bool(run["complete"]) and adopted == len(legs)
            # 提交即冻结:采用与排除的航段及理由随成绩一并固化。
            conn.execute(
                """INSERT INTO scores(score_id, flight_id, segmentation_id, version, waterline,
                     submitted_by, created_at, total_time_s, complete, status, supersedes)
                   VALUES (?,?,?,?,?,?,?,?,?, 'frozen', ?)""",
                (score_id, flight_id, run["segmentation_id"], version, run["waterline"],
                 referee_id, iso(self.now()), total, 1 if complete else 0,
                 prev["score_id"] if prev else None),
            )
            for d in decisions:
                leg = leg_by_id[d["leg_id"]]
                reason = (d.get("reason") or "").strip() or leg["reason"]
                conn.execute(
                    "INSERT INTO score_legs(score_id, leg_id, decision, reason) VALUES (?,?,?,?)",
                    (score_id, d["leg_id"], d["decision"], reason),
                )
        return self.get_score(score_id)

    def confirm_score(self, arbitrator_id: str, score_id: str, waterline_seen: int) -> dict:
        with self.store.txn() as conn:
            score = conn.execute("SELECT * FROM scores WHERE score_id=?", (score_id,)).fetchone()
            if score is None:
                raise NotFound(f"成绩 {score_id} 不存在")
            if score["status"] not in ("frozen", "locked"):
                raise Conflict(f"成绩 {score_id} 已处于 {score['status']} 状态,无法确认")
            flight = conn.execute(
                "SELECT * FROM flights WHERE flight_id=?", (score["flight_id"],)
            ).fetchone()
            if arbitrator_id == flight["player_id"]:
                raise Forbidden("选手不能确认自己的成绩(无自批权限)")
            if arbitrator_id == score["submitted_by"]:
                raise Forbidden("提交裁判不能兼任该成绩的独立仲裁人")
            uploader = conn.execute(
                "SELECT 1 FROM packets WHERE flight_id=? AND uploader_id=? LIMIT 1",
                (score["flight_id"], arbitrator_id),
            ).fetchone()
            if uploader:
                raise Forbidden("证据上传者不能确认相关成绩(无自批权限)")

            counts = 1 if waterline_seen == score["waterline"] else 0
            confirmation_id = _id("cfm")
            try:
                conn.execute(
                    """INSERT INTO confirmations(confirmation_id, score_id, arbitrator_id,
                         waterline_seen, counts, created_at) VALUES (?,?,?,?,?,?)""",
                    (confirmation_id, score_id, arbitrator_id, waterline_seen, counts, iso(self.now())),
                )
            except Exception as exc:
                raise Conflict(f"仲裁人 {arbitrator_id} 已确认过成绩 {score_id}") from exc

            locked = False
            if counts and score["status"] == "frozen":
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM confirmations WHERE score_id=? AND counts=1",
                    (score_id,),
                ).fetchone()["c"]
                if n >= self.quorum:
                    conn.execute("UPDATE scores SET status='locked' WHERE score_id=?", (score_id,))
                    conn.execute(
                        """UPDATE scores SET status='superseded'
                             WHERE flight_id=? AND score_id!=? AND status='locked'""",
                        (score["flight_id"], score_id),
                    )
                    locked = True
                    published = conn.execute(
                        "SELECT 1 FROM publications WHERE event_id=? LIMIT 1", (flight["event_id"],)
                    ).fetchone()
                    open_reopen = conn.execute(
                        "SELECT 1 FROM reopens WHERE event_id=? AND status='open' LIMIT 1",
                        (flight["event_id"],),
                    ).fetchone()
                    if (not published) or open_reopen:
                        self._enqueue(conn, "recompute_event", {"event_id": flight["event_id"]})
        self._maybe_drain()
        return {"confirmation_id": confirmation_id, "counts": bool(counts), "locked": locked}

    # ---------- 排名 ----------

    def _locked_candidates(self, conn, event_id: str, waterline: int) -> dict[str, dict]:
        rows = conn.execute(
            """SELECT s.score_id, s.flight_id, f.player_id, s.total_time_s, s.complete, s.created_at
                 FROM scores s JOIN flights f ON f.flight_id=s.flight_id
                 WHERE f.event_id=? AND s.status='locked' AND s.waterline<=?""",
            (event_id, waterline),
        ).fetchall()
        best: dict[str, dict] = {}
        for r in rows:
            cand = {"score_id": r["score_id"], "flight_id": r["flight_id"],
                    "player_id": r["player_id"], "total_time_s": r["total_time_s"],
                    "complete": bool(r["complete"]), "created_at": r["created_at"]}
            cur = best.get(r["flight_id"])
            if cur is None or _rank_key(cand) < _rank_key(cur):
                best[r["flight_id"]] = cand
        return best

    @staticmethod
    def _rank_candidates(candidates: list[dict]) -> list[dict]:
        ordered = sorted(candidates, key=_rank_key)
        return [dict(c, rank=i + 1) for i, c in enumerate(ordered)]

    def _current_ranking(self, conn, event_id: str):
        return conn.execute(
            "SELECT * FROM rankings WHERE event_id=? AND status='current'", (event_id,)
        ).fetchone()

    def _ranking_entries(self, conn, ranking_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM ranking_entries WHERE ranking_id=? ORDER BY rank", (ranking_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def _entries_equal(self, conn, ranking_id: str, entries: list[dict]) -> bool:
        old = self._ranking_entries(conn, ranking_id)
        sig_old = sorted((e["flight_id"], e["score_id"], e["rank"]) for e in old)
        sig_new = sorted((e["flight_id"], e["score_id"], e["rank"]) for e in entries)
        return sig_old == sig_new

    def _store_ranking(self, conn, event_id: str, waterline: int, entries: list[dict]) -> str:
        prev = self._current_ranking(conn, event_id)
        prev_ranks: dict[str, int] = {}
        if prev:
            for e in self._ranking_entries(conn, prev["ranking_id"]):
                prev_ranks[e["flight_id"]] = e["rank"]
            conn.execute("UPDATE rankings SET status='superseded' WHERE ranking_id=?",
                         (prev["ranking_id"],))
        ranking_id = _id("rank")
        version = (prev["version"] + 1) if prev else 1
        conn.execute(
            "INSERT INTO rankings(ranking_id, event_id, version, waterline, status, created_at)"
            " VALUES (?,?,?,?, 'current', ?)",
            (ranking_id, event_id, version, waterline, iso(self.now())),
        )
        for e in entries:
            prev_rank = prev_ranks.get(e["flight_id"])
            delta = (prev_rank - e["rank"]) if prev_rank is not None else None
            conn.execute(
                """INSERT INTO ranking_entries(ranking_id, flight_id, player_id, score_id,
                     total_time_s, complete, rank, rank_delta) VALUES (?,?,?,?,?,?,?,?)""",
                (ranking_id, e["flight_id"], e["player_id"], e["score_id"], e["total_time_s"],
                 1 if e["complete"] else 0, e["rank"], delta),
            )
        return ranking_id

    def _recompute_event(self, conn, event_id: str) -> None:
        waterline = self._waterline(conn)
        self._segment_stale_flights(conn, event_id, waterline)
        published = conn.execute(
            "SELECT 1 FROM publications WHERE event_id=? LIMIT 1", (event_id,)
        ).fetchone()
        open_reopen = conn.execute(
            "SELECT 1 FROM reopens WHERE event_id=? AND status='open' LIMIT 1", (event_id,)
        ).fetchone()
        if published and not open_reopen:
            return  # 已发布且未获准重开:只更新分段,不动排名
        entries = self._rank_candidates(list(self._locked_candidates(conn, event_id, waterline).values()))
        current = self._current_ranking(conn, event_id)
        if current and self._entries_equal(conn, current["ranking_id"], entries):
            return
        if not entries and not current:
            return
        self._store_ranking(conn, event_id, waterline, entries)

    def _provisional_candidate(self, conn, flight: dict, run) -> dict | None:
        legs = conn.execute(
            "SELECT * FROM legs WHERE segmentation_id=? ORDER BY ord", (run["segmentation_id"],)
        ).fetchall()
        adopted = [l for l in legs if l["status"] == "valid"]
        if not adopted:
            return None
        total = sum(l["duration_s"] for l in adopted)
        complete = bool(run["complete"]) and len(adopted) == len(legs)
        return {"score_id": f"provisional:{run['segmentation_id']}", "flight_id": flight["flight_id"],
                "player_id": flight["player_id"], "total_time_s": total, "complete": complete,
                "created_at": run["created_at"]}

    def _impact_assessment(self, conn, event_id: str, trigger_packet_id: str | None) -> None:
        waterline = self._waterline(conn)
        self._segment_stale_flights(conn, event_id, waterline)
        pub = conn.execute(
            "SELECT * FROM publications WHERE event_id=? ORDER BY published_at DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        if pub is None:
            return
        old_entries = {e["flight_id"]: e for e in self._ranking_entries(conn, pub["ranking_id"])}
        candidates = self._locked_candidates(conn, event_id, waterline)
        flights = conn.execute("SELECT * FROM flights WHERE event_id=?", (event_id,)).fetchall()
        for flight in flights:
            if flight["evidence_waterline"] <= pub["waterline"]:
                continue
            run = conn.execute(
                """SELECT * FROM segmentation_runs WHERE flight_id=?
                     ORDER BY waterline DESC, created_at DESC LIMIT 1""",
                (flight["flight_id"],),
            ).fetchone()
            if run is None:
                continue
            prov = self._provisional_candidate(conn, dict(flight), run)
            if prov is None:
                continue
            cur = candidates.get(flight["flight_id"])
            # 临时成绩只有严格更优(先完整度后总用时)才顶替已锁定成绩;
            # 等价时保留已锁定成绩,避免评估虚报"依据变化"。
            if cur is None or (0 if prov["complete"] else 1, prov["total_time_s"]) < \
                    (0 if cur["complete"] else 1, cur["total_time_s"]):
                candidates[flight["flight_id"]] = prov
        hypo = self._rank_candidates(list(candidates.values()))
        hypo_by_flight = {e["flight_id"]: e for e in hypo}

        details: list[dict] = []
        for flight_id, e in hypo_by_flight.items():
            old = old_entries.get(flight_id)
            if old is None:
                details.append({"flight_id": flight_id, "player_id": e["player_id"],
                                "old_rank": None, "new_rank": e["rank"],
                                "old_total_s": None, "new_total_s": e["total_time_s"],
                                "basis_score_id": e["score_id"], "note": "新成绩进入榜单"})
            elif old["rank"] != e["rank"] or old["score_id"] != e["score_id"]:
                details.append({"flight_id": flight_id, "player_id": e["player_id"],
                                "old_rank": old["rank"], "new_rank": e["rank"],
                                "old_total_s": old["total_time_s"], "new_total_s": e["total_time_s"],
                                "basis_score_id": e["score_id"], "note": "排名或成绩依据变化"})
        for flight_id, old in old_entries.items():
            if flight_id not in hypo_by_flight:
                details.append({"flight_id": flight_id, "player_id": old["player_id"],
                                "old_rank": old["rank"], "new_rank": None,
                                "old_total_s": old["total_time_s"], "new_total_s": None,
                                "basis_score_id": None, "note": "跌出榜单"})
        conn.execute(
            """INSERT INTO assessments(assessment_id, event_id, trigger_packet_id, waterline_before,
                 waterline_after, would_change, details_json, status, created_at)
               VALUES (?,?,?,?,?,?,?, 'pending', ?)""",
            (_id("asm"), event_id, trigger_packet_id, pub["waterline"], waterline,
             1 if details else 0, json.dumps(details, ensure_ascii=False), iso(self.now())),
        )

    # ---------- 发布 / 重开 ----------

    def publish_event(self, event_id: str, published_by: str) -> dict:
        self.drain()  # 先结清已排队的重算,再在单事务里固定水位
        with self.store.txn() as conn:
            latest_pub = conn.execute(
                "SELECT * FROM publications WHERE event_id=? ORDER BY published_at DESC LIMIT 1",
                (event_id,),
            ).fetchone()
            open_reopen = conn.execute(
                "SELECT * FROM reopens WHERE event_id=? AND status='open' LIMIT 1", (event_id,)
            ).fetchone()
            if latest_pub and not open_reopen:
                raise Conflict("赛事已发布;补证须先完成影响评估并获准重开")
            waterline = self._waterline(conn)
            entries = self._rank_candidates(
                list(self._locked_candidates(conn, event_id, waterline).values()))
            if not entries:
                raise Conflict("没有已锁定成绩,无法发布排名")
            current = self._current_ranking(conn, event_id)
            if current and self._entries_equal(conn, current["ranking_id"], entries):
                ranking_id = current["ranking_id"]
            else:
                ranking_id = self._store_ranking(conn, event_id, waterline, entries)
            publication_id = _id("pub")
            now = self.now()
            appeal_deadline = now + timedelta(seconds=self.appeal_window_s)
            conn.execute(
                """INSERT INTO publications(publication_id, event_id, ranking_id, waterline,
                     published_at, appeal_deadline) VALUES (?,?,?,?,?,?)""",
                (publication_id, event_id, ranking_id, waterline, iso(now), iso(appeal_deadline)),
            )
            for e in entries:
                conn.execute(
                    """INSERT INTO notifications(notification_id, publication_id, player_id, flight_id,
                         rank, total_time_s, created_at) VALUES (?,?,?,?,?,?,?)""",
                    (_id("ntf"), publication_id, e["player_id"], e["flight_id"], e["rank"],
                     e["total_time_s"], iso(now)),
                )
            if open_reopen:
                conn.execute(
                    "UPDATE reopens SET status='closed', closed_at=? WHERE event_id=? AND status='open'",
                    (iso(now), event_id),
                )
        return {"publication_id": publication_id, "event_id": event_id, "ranking_id": ranking_id,
                "waterline": waterline, "appeal_deadline": iso(appeal_deadline),
                "entries": entries}

    def approve_reopen(self, assessment_id: str, approved_by: str, reason: str) -> dict:
        with self.store.txn() as conn:
            a = conn.execute(
                "SELECT * FROM assessments WHERE assessment_id=?", (assessment_id,)
            ).fetchone()
            if a is None:
                raise NotFound(f"影响评估 {assessment_id} 不存在")
            if a["status"] != "pending":
                raise Conflict(f"影响评估 {assessment_id} 已处理({a['status']})")
            conn.execute(
                "UPDATE assessments SET status='reopen_approved', decided_at=?, decided_by=?"
                " WHERE assessment_id=?",
                (iso(self.now()), approved_by, assessment_id),
            )
            reopen_id = _id("reopen")
            conn.execute(
                """INSERT INTO reopens(reopen_id, event_id, assessment_id, approved_by, reason,
                     status, created_at) VALUES (?,?,?,?,?, 'open', ?)""",
                (reopen_id, a["event_id"], assessment_id, approved_by, reason, iso(self.now())),
            )
            self._enqueue(conn, "recompute_event", {"event_id": a["event_id"]})
        self._maybe_drain()
        return {"reopen_id": reopen_id, "assessment_id": assessment_id, "status": "open"}

    def reject_assessment(self, assessment_id: str, decided_by: str) -> dict:
        with self.store.txn() as conn:
            a = conn.execute(
                "SELECT * FROM assessments WHERE assessment_id=?", (assessment_id,)
            ).fetchone()
            if a is None:
                raise NotFound(f"影响评估 {assessment_id} 不存在")
            if a["status"] != "pending":
                raise Conflict(f"影响评估 {assessment_id} 已处理({a['status']})")
            conn.execute(
                "UPDATE assessments SET status='rejected', decided_at=?, decided_by=?"
                " WHERE assessment_id=?",
                (iso(self.now()), decided_by, assessment_id),
            )
        return {"assessment_id": assessment_id, "status": "rejected"}

    # ---------- 任务队列与恢复 ----------

    def drain(self) -> list[dict]:
        processed: list[dict] = []
        while True:
            with self.store.lock:
                row = self.store.conn.execute(
                    "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at, job_id LIMIT 1"
                ).fetchone()
            if row is None:
                break
            job_id = row["job_id"]
            try:
                with self.store.txn() as conn:
                    conn.execute(
                        "UPDATE jobs SET status='running', started_at=? WHERE job_id=?",
                        (iso(self.now()), job_id),
                    )
                    payload = json.loads(row["payload_json"])
                    if row["type"] == "recompute_event":
                        self._recompute_event(conn, payload["event_id"])
                    elif row["type"] == "impact_assessment":
                        self._impact_assessment(conn, payload["event_id"],
                                                payload.get("trigger_packet_id"))
                    else:
                        raise ValueError(f"未知任务类型 {row['type']}")
                    conn.execute(
                        "UPDATE jobs SET status='done', finished_at=? WHERE job_id=?",
                        (iso(self.now()), job_id),
                    )
                processed.append({"job_id": job_id, "type": row["type"], "status": "done"})
            except Exception as exc:  # 任务失败留在队列里可查,不拖垮其他任务
                with self.store.txn() as conn:
                    conn.execute(
                        "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE job_id=?",
                        (str(exc), iso(self.now()), job_id),
                    )
                processed.append({"job_id": job_id, "type": row["type"], "status": "failed",
                                  "error": str(exc)})
        return processed

    def _maybe_drain(self) -> None:
        if self.auto_drain:
            self.drain()

    def recover(self) -> dict:
        """服务恢复:中断的 running 任务重新入队并续跑;申诉期限为绝对时间,天然续存。"""
        with self.store.txn() as conn:
            cur = conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            resumed = cur.rowcount
            queued = conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE status='queued'").fetchone()["c"]
        processed = self.drain() if self.auto_drain else []
        return {"resumed_running_jobs": resumed, "queued_before_drain": queued,
                "drained": len(processed)}

    # ---------- 查询 ----------

    @_locked
    def list_flights(self, event_id: str | None = None) -> dict:
        sql = "SELECT * FROM flights"
        args: tuple = ()
        if event_id:
            sql += " WHERE event_id=?"
            args = (event_id,)
        sql += " ORDER BY created_at, flight_id"
        items = []
        for f in self.store.conn.execute(sql, args).fetchall():
            stats = self.store.conn.execute(
                """SELECT COALESCE(SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END),0) AS accepted,
                          COALESCE(SUM(CASE WHEN status='quarantined' THEN 1 ELSE 0 END),0) AS quarantined,
                          COALESCE(SUM(duplicate_count),0) AS duplicates
                     FROM points WHERE flight_id=?""",
                (f["flight_id"],),
            ).fetchone()
            score = self.store.conn.execute(
                "SELECT score_id, version, status, total_time_s FROM scores WHERE flight_id=?"
                " ORDER BY version DESC LIMIT 1",
                (f["flight_id"],),
            ).fetchone()
            items.append({
                "flight_id": f["flight_id"], "event_id": f["event_id"], "player_id": f["player_id"],
                "evidence_waterline": f["evidence_waterline"],
                "points": dict(stats),
                "latest_score": dict(score) if score else None,
            })
        return {"items": items}

    @_locked
    def _score_legs(self, score_id: str) -> list[dict]:
        rows = self.store.conn.execute(
            """SELECT sl.decision, sl.reason AS decision_reason, l.leg_id, l.ord,
                      l.from_gate_id, l.to_gate_id, l.entered_at, l.exited_at, l.duration_s,
                      l.status, l.reason AS segment_reason
                 FROM score_legs sl JOIN legs l ON l.leg_id = sl.leg_id
                 WHERE sl.score_id=? ORDER BY l.ord""",
            (score_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def get_score(self, score_id: str) -> dict:
        s = self.store.conn.execute("SELECT * FROM scores WHERE score_id=?", (score_id,)).fetchone()
        if s is None:
            raise NotFound(f"成绩 {score_id} 不存在")
        confirmations = self.store.conn.execute(
            "SELECT * FROM confirmations WHERE score_id=? ORDER BY created_at", (score_id,)
        ).fetchall()
        return {
            "score_id": s["score_id"], "flight_id": s["flight_id"], "version": s["version"],
            "status": s["status"], "waterline": s["waterline"],
            "segmentation_id": s["segmentation_id"], "submitted_by": s["submitted_by"],
            "created_at": s["created_at"], "total_time_s": s["total_time_s"],
            "complete": bool(s["complete"]), "supersedes": s["supersedes"],
            "legs": self._score_legs(score_id),
            "confirmations": [dict(c) for c in confirmations],
        }

    @_locked
    def flight_detail(self, flight_id: str) -> dict:
        f = self.store.conn.execute(
            "SELECT * FROM flights WHERE flight_id=?", (flight_id,)).fetchone()
        if f is None:
            raise NotFound(f"航班 {flight_id} 不存在")
        packets = self.store.conn.execute(
            "SELECT * FROM packets WHERE flight_id=? ORDER BY received_at", (flight_id,)
        ).fetchall()
        stats = self.store.conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END),0) AS accepted,
                      COALESCE(SUM(CASE WHEN status='quarantined' THEN 1 ELSE 0 END),0) AS quarantined,
                      COALESCE(SUM(duplicate_count),0) AS duplicates
                 FROM points WHERE flight_id=?""",
            (flight_id,),
        ).fetchone()
        runs = []
        for r in self.store.conn.execute(
            "SELECT * FROM segmentation_runs WHERE flight_id=? ORDER BY waterline", (flight_id,)
        ).fetchall():
            legs = self.store.conn.execute(
                "SELECT * FROM legs WHERE segmentation_id=? ORDER BY ord", (r["segmentation_id"],)
            ).fetchall()
            runs.append({
                "segmentation_id": r["segmentation_id"], "waterline": r["waterline"],
                "calibration_version": r["calibration_version"],
                "altitude_offset_m": r["altitude_offset_m"],
                "point_count": r["point_count"], "complete": bool(r["complete"]),
                "events": json.loads(r["events_json"]),
                "legs": [dict(l) for l in legs],
            })
        scores = [self.get_score(r["score_id"]) for r in self.store.conn.execute(
            "SELECT score_id FROM scores WHERE flight_id=? ORDER BY version", (flight_id,)
        ).fetchall()]
        quarantine = self.list_quarantine(flight_id)["items"]
        return {
            "flight_id": f["flight_id"], "event_id": f["event_id"], "player_id": f["player_id"],
            "evidence_waterline": f["evidence_waterline"],
            "points": dict(stats),
            "packets": [dict(p) for p in packets],
            "segmentation_runs": runs,
            "scores": scores,
            "quarantine": quarantine,
        }

    @_locked
    def event_detail(self, event_id: str) -> dict:
        flights = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM flights WHERE event_id=?", (event_id,)).fetchone()["c"]
        current = self._current_ranking(self.store.conn, event_id)
        pub = self.store.conn.execute(
            "SELECT * FROM publications WHERE event_id=? ORDER BY published_at DESC LIMIT 1",
            (event_id,)).fetchone()
        open_reopen = self.store.conn.execute(
            "SELECT * FROM reopens WHERE event_id=? AND status='open' LIMIT 1", (event_id,)
        ).fetchone()
        pending_assessments = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM assessments WHERE event_id=? AND status='pending'",
            (event_id,)).fetchone()["c"]
        queued_jobs = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE status='queued'").fetchone()["c"]
        appeal_open = None
        if pub:
            appeal_open = self.now() < parse_ts(pub["appeal_deadline"])
        return {
            "event_id": event_id,
            "flights": flights,
            "waterline": self._waterline(self.store.conn),
            "current_ranking": ({"ranking_id": current["ranking_id"], "version": current["version"],
                                 "waterline": current["waterline"]} if current else None),
            "latest_publication": ({"publication_id": pub["publication_id"],
                                    "ranking_id": pub["ranking_id"],
                                    "waterline": pub["waterline"],
                                    "published_at": pub["published_at"],
                                    "appeal_deadline": pub["appeal_deadline"],
                                    "appeal_open": appeal_open} if pub else None),
            "open_reopen": dict(open_reopen) if open_reopen else None,
            "pending_assessments": pending_assessments,
            "queued_jobs": queued_jobs,
        }

    @_locked
    def event_ranking(self, event_id: str) -> dict:
        current = self._current_ranking(self.store.conn, event_id)
        if current is None:
            return {"event_id": event_id, "ranking": None}
        entries = []
        for e in self._ranking_entries(self.store.conn, current["ranking_id"]):
            score = self.store.conn.execute(
                "SELECT * FROM scores WHERE score_id=?", (e["score_id"],)).fetchone()
            entries.append({
                "rank": e["rank"], "rank_delta": e["rank_delta"],
                "flight_id": e["flight_id"], "player_id": e["player_id"],
                "score_id": e["score_id"],
                "score_version": score["version"] if score else None,
                "score_waterline": score["waterline"] if score else None,
                "total_time_s": e["total_time_s"], "complete": bool(e["complete"]),
                "legs": self._score_legs(e["score_id"]) if score else [],
            })
        return {
            "event_id": event_id,
            "ranking": {
                "ranking_id": current["ranking_id"], "version": current["version"],
                "waterline": current["waterline"], "created_at": current["created_at"],
                "entries": entries,
            },
        }

    @_locked
    def event_publications(self, event_id: str) -> dict:
        pubs = []
        for p in self.store.conn.execute(
            "SELECT * FROM publications WHERE event_id=? ORDER BY published_at", (event_id,)
        ).fetchall():
            notifications = self.store.conn.execute(
                "SELECT * FROM notifications WHERE publication_id=? ORDER BY rank",
                (p["publication_id"],),
            ).fetchall()
            pubs.append({
                "publication_id": p["publication_id"], "ranking_id": p["ranking_id"],
                "waterline": p["waterline"], "published_at": p["published_at"],
                "appeal_deadline": p["appeal_deadline"],
                "appeal_open": self.now() < parse_ts(p["appeal_deadline"]),
                "notifications": [dict(n) for n in notifications],
            })
        return {"items": pubs}

    @_locked
    def event_assessments(self, event_id: str) -> dict:
        rows = self.store.conn.execute(
            "SELECT * FROM assessments WHERE event_id=? ORDER BY created_at", (event_id,)
        ).fetchall()
        return {"items": [dict(r) | {"details": json.loads(r["details_json"])} for r in rows]}

    @_locked
    def list_points(self, flight_id: str) -> dict:
        f = self.store.conn.execute(
            "SELECT flight_id FROM flights WHERE flight_id=?", (flight_id,)).fetchone()
        if f is None:
            raise NotFound(f"航班 {flight_id} 不存在")
        rows = self.store.conn.execute(
            """SELECT point_id, packet_id, device_id, device_session_id, device_seq, device_time,
                      received_at, lat, lon, altitude_m, digest, status, duplicate_count
                 FROM points WHERE flight_id=? ORDER BY device_time, device_session_id, device_seq""",
            (flight_id,),
        ).fetchall()
        return {"items": [dict(r) for r in rows]}

    @_locked
    def list_jobs(self) -> dict:
        rows = self.store.conn.execute(
            "SELECT * FROM jobs ORDER BY created_at, job_id").fetchall()
        return {"items": [dict(r) for r in rows]}

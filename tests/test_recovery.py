"""服务恢复:中断任务重新入队续跑,申诉期限按绝对时间继续。"""
from datetime import datetime, timezone

from domain.service import EvidenceService
from tests.conftest import (GATE_DICTS, Clock, make_track, packet,
                            service_submit_and_lock)


def test_recovery_resumes_queued_recompute(tmp_path):
    db = str(tmp_path / "evidence.db")
    clock = Clock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))

    # 实例 A:延迟执行模式下证据入队即"崩溃"(任务甚至来不及离开 running)
    svc_a = EvidenceService(db, auto_drain=False, now_fn=clock)
    svc_a.upsert_gates("E1", GATE_DICTS)
    svc_a.ingest_packet("up1", packet("PK-F1", "F1", "pilotA", make_track(n=11)))
    queued = [j for j in svc_a.list_jobs()["items"] if j["status"] == "queued"]
    assert queued, "补证应已生成待执行的重算任务"
    svc_a.store.conn.execute("UPDATE jobs SET status='running' WHERE status='queued'")

    # 实例 B 接管同一数据库:恢复时把中断的 running 任务重新入队并续跑
    svc_b = EvidenceService(db, auto_drain=True, now_fn=clock)
    report = svc_b.recover()
    assert report["resumed_running_jobs"] == len(queued)
    detail = svc_b.flight_detail("F1")
    assert detail["segmentation_runs"], "恢复后应续跑排队的重算,生成分段"
    assert all(j["status"] == "done" for j in svc_b.list_jobs()["items"])


def test_recovery_preserves_appeal_deadline(tmp_path):
    db = str(tmp_path / "evidence.db")
    clock = Clock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))

    svc_a = EvidenceService(db, auto_drain=True, now_fn=clock, appeal_window_s=3600)
    svc_a.upsert_gates("E1", GATE_DICTS)
    svc_a.ingest_packet("up1", packet("PK-F1", "F1", "pilotA", make_track(n=11)))
    service_submit_and_lock(svc_a, "F1")
    svc_a.publish_event("E1", "ref1")

    clock.advance(seconds=1800)
    # 重启后申诉期限继续有效(绝对时间,不随进程生命周期重置)
    svc_b = EvidenceService(db, auto_drain=True, now_fn=clock, appeal_window_s=3600)
    svc_b.recover()
    pub = svc_b.event_detail("E1")["latest_publication"]
    assert pub["appeal_open"] is True
    assert pub["appeal_deadline"] == "2026-09-25T09:00:00Z"

    clock.advance(seconds=1801)
    svc_c = EvidenceService(db, auto_drain=True, now_fn=clock, appeal_window_s=3600)
    svc_c.recover()
    assert svc_c.event_detail("E1")["latest_publication"]["appeal_open"] is False

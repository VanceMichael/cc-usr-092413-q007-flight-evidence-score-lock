"""锁定/发布与补证并发:事务只能让一个证据水位胜出。"""
import threading

from tests.conftest import (GATE_DICTS, make_service, make_track, packet,
                            service_submit_and_lock)


def test_publish_and_concurrent_evidence_single_waterline_wins():
    svc, _ = make_service()
    svc.upsert_gates("E1", GATE_DICTS)
    svc.ingest_packet("up0", packet("PK-F1", "F1", "pilotA", make_track(n=11)))
    service_submit_and_lock(svc, "F1")

    barrier = threading.Barrier(4)
    outcome = {"pub": None, "uploads": [], "errors": []}

    def do_publish():
        try:
            barrier.wait()
            outcome["pub"] = svc.publish_event("E1", "ref1")
        except Exception as exc:  # pragma: no cover
            outcome["errors"].append(exc)

    def do_upload(i):
        try:
            barrier.wait()
            resp = svc.ingest_packet(f"up{i}", packet(
                f"PK-LATE{i}", f"FL{i}", f"pilotL{i}",
                make_track(n=6, point_prefix=f"L{i}p", digest=f"L{i}d")))
            outcome["uploads"].append((f"PK-LATE{i}", resp))
        except Exception as exc:  # pragma: no cover
            outcome["errors"].append(exc)

    threads = [threading.Thread(target=do_publish)]
    threads += [threading.Thread(target=do_upload, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not outcome["errors"]
    pub = outcome["pub"]
    pub_wl = pub["waterline"]

    assessments = svc.event_assessments("E1")["items"]
    for packet_id, resp in outcome["uploads"]:
        wl = resp["waterline"]
        assert wl is not None
        if wl > pub_wl:
            # 晚于发布水位胜出的补证:进入影响评估,绝不混进已发布榜单
            assert any(a["trigger_packet_id"] == packet_id for a in assessments), packet_id
        else:
            # 先于发布水位被接收的证据:已在发布事务里被复算覆盖
            assert not any(a["trigger_packet_id"] == packet_id for a in assessments), packet_id

    # 已发布榜单中每个成绩版本的证据水位都不超过发布水位:无多版本混合
    ranking = svc.event_ranking("E1")["ranking"]
    assert ranking is not None
    for entry in ranking["entries"]:
        assert entry["score_waterline"] <= pub_wl

    # 只存在一个发布事实
    assert len(svc.event_publications("E1")["items"]) == 1
    # 没有任何任务失败
    assert all(j["status"] == "done" for j in svc.list_jobs()["items"])

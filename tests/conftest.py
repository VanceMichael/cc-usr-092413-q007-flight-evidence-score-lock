import os
import tempfile

import pytest

from app import storage


@pytest.fixture()
def client():
    tmp = tempfile.mkdtemp()
    os.environ["EVIDENCE_DB"] = os.path.join(tmp, "test.db")
    storage.init_db(os.environ["EVIDENCE_DB"])
    from fastapi.testclient import TestClient
    from app.api import app
    with TestClient(app) as c:
        yield c
    storage.db().close()


def make_config():
    return {
        "start_gate": "S",
        "finish_gate": "F",
        "gates": [
            {"id": "S", "lat": 0.0, "lon": 0.0, "radius_m": 30.0},
            {"id": "F", "lat": 220.0 / 111195.0, "lon": 0.0, "radius_m": 30.0},
        ],
        "corridors": [
            {"id": "C1", "from_gate": "S", "to_gate": "F", "half_width_m": 80.0}
        ],
        "no_fly_zones": [
            {"id": "NFZ_FAR", "lat": 0.5, "lon": 0.5, "radius_m": 50.0}
        ],
        "calibrations": [
            {"id": "CAL_V1", "valid_from": "2026-09-25T00:00:00Z",
             "altitude_offset_m": 2.0, "altitude_scale": 1.01}
        ],
    }


def track(packet_id="pk-gps", base="2026-09-25T10:00:00Z", with_altitude=False,
          session="s1", seq_start=1, reboot_after_finish=True, alt_value=1500.0):
    """沿经线生成 -40m..240m 的轨迹，门点 S=0m F=220m，点距 10m / 10s。"""
    from datetime import datetime, timedelta, timezone
    t0 = datetime.fromisoformat(base.replace("Z", "+00:00"))
    points = []
    i = 0
    m = -40
    while m <= 240:
        points.append({
            "device_session": session,
            "device_seq": seq_start + i,
            "device_time": (t0 + timedelta(seconds=10 * i)).isoformat().replace("+00:00", "Z"),
            "lat": m / 111195.0,
            "lon": 0.0,
            "altitude": alt_value if with_altitude else None,
            "point_summary": "baro+gnss" if with_altitude else "gnss",
        })
        m += 10
        i += 1
    if reboot_after_finish:
        # 完赛之后设备重启：新会话 + 序号回退归零。
        points.append({
            "device_session": "s2",
            "device_seq": 1,
            "device_time": (t0 + timedelta(seconds=10 * i)).isoformat().replace("+00:00", "Z"),
            "lat": 250 / 111195.0, "lon": 0.0,
            "altitude": alt_value if with_altitude else None,
            "point_summary": "post-reboot",
        })
    return {"packet_id": packet_id, "points": points, "summary": "正赛轨迹"}

"""Regression: /sync/live_metrics returned 500 when total_bytes arrived as a string."""
from sp_sync import sync_metrics as m


def setup_function(_):
    m.reset_metrics()


def teardown_function(_):
    m.reset_metrics()


def test_string_total_bytes_from_start_file_progress():
    m.set_sync_active(True, "sharepoint", 3)
    m.start_file_progress("SEM03", "lecture.mp4", total_bytes="10485760")
    out = m.get_live_metrics()
    assert out["total_bytes"] == 10485760
    assert out["total_mb"] == 10.0


def test_string_bytes_from_update_file_progress():
    m.set_sync_active(True, "sharepoint", 1)
    m.start_file_progress("SEM03", "lecture.mp4")
    m.update_file_progress("5242880", "10485760")
    out = m.get_live_metrics()
    assert out["downloaded_mb"] == 5.0
    assert out["total_mb"] == 10.0
    assert out["file_progress_pct"] == 50


def test_garbage_values_do_not_raise():
    with m._lock:
        m._state["total_bytes"] = "n/a"
        m._state["downloaded_bytes"] = None
        m._state["speed_bps"] = ""
    out = m.get_live_metrics()
    assert out["total_mb"] == 0.0
    assert out["downloaded_mb"] == 0.0
    assert out["speed_mbps"] == 0.0


def test_live_metrics_route_ok_with_string_total(monkeypatch):
    import app as app_module
    flask_app = getattr(app_module, "app", None)
    if flask_app is None:
        import pytest
        pytest.skip("no module-level Flask app")
    with m._lock:
        m._state["total_bytes"] = "1048576"
    resp = flask_app.test_client().get("/sync/live_metrics")
    assert resp.status_code == 200

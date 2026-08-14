from marry.monitor import MonitorState


def test_monitor_state_defaults_to_serializable_values() -> None:
    state = MonitorState(status="unavailable", checked_at="2026-08-14T00:00:00+09:00")
    assert state.status == "unavailable"
    assert state.detail == ""

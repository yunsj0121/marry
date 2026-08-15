from marry.monitor import MonitorState, Target


def test_monitor_state_defaults_to_serializable_values() -> None:
    state = MonitorState(run_status="ok", checked_at="2026-08-14T00:00:00+09:00")
    assert state.run_status == "ok"
    assert state.targets == {}
    assert state.error == ""


def test_target_key_formats_as_iso_date_and_time() -> None:
    target = Target(2027, 9, 4, "11:00")
    assert target.key == "2027-09-04 11:00"

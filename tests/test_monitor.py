import pytest

import marry.monitor as monitor
from marry.monitor import MonitorState, Target, actions_run_url, build_error_message, parse_extra_targets


def test_monitor_state_defaults_to_serializable_values() -> None:
    state = MonitorState(run_status="ok", checked_at="2026-08-14T00:00:00+09:00")
    assert state.run_status == "ok"
    assert state.targets == {}
    assert state.error == ""


def test_target_key_formats_as_iso_date_and_time() -> None:
    target = Target(2027, 9, 4, "11:00")
    assert target.key == "2027-09-04 11:00"


def test_actions_run_url_builds_from_env(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    assert actions_run_url() == "https://github.com/owner/repo/actions/runs/123"


def test_actions_run_url_is_empty_outside_actions(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    assert actions_run_url() == ""


def test_build_error_message_includes_diagnostics_and_run_url(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monitor.DIAGNOSTIC_LINES.clear()
    monitor.DIAGNOSTIC_LINES.append("로그인 제출 후 URL: https://example.test")
    message = build_error_message("TimeoutError: boom")
    assert "TimeoutError: boom" in message
    assert "로그인 제출 후 URL: https://example.test" in message
    assert "https://github.com/owner/repo/actions/runs/123" in message


def test_build_error_message_stays_within_telegram_limit(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monitor.DIAGNOSTIC_LINES.clear()
    for index in range(monitor.DIAGNOSTIC_LINES.maxlen or 25):
        monitor.DIAGNOSTIC_LINES.append(f"{index}:" + "가" * 400)
    message = build_error_message("RuntimeError: " + "나" * 2_000)
    assert len(message) <= monitor.TELEGRAM_LIMIT


def test_parse_extra_targets_returns_empty_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("EXTRA_TARGETS", raising=False)
    assert parse_extra_targets() == []


def test_parse_extra_targets_parses_comma_separated_list(monkeypatch) -> None:
    monkeypatch.setenv(
        "EXTRA_TARGETS", "2027-08-29 11:00, 2027-08-29 13:00,2027-08-29 17:00"
    )
    targets = parse_extra_targets()
    assert [t.key for t in targets] == [
        "2027-08-29 11:00",
        "2027-08-29 13:00",
        "2027-08-29 17:00",
    ]


def test_parse_extra_targets_rejects_bad_format(monkeypatch) -> None:
    monkeypatch.setenv("EXTRA_TARGETS", "not-a-date")
    with pytest.raises(RuntimeError):
        parse_extra_targets()


def test_diagnostic_tee_captures_lines_and_passes_through() -> None:
    import io

    monitor.DIAGNOSTIC_LINES.clear()
    stream = io.StringIO()
    tee = monitor.DiagnosticTee(stream)
    tee.write("first line\nsecond line\n")
    assert stream.getvalue() == "first line\nsecond line\n"
    assert list(monitor.DIAGNOSTIC_LINES) == ["first line", "second line"]

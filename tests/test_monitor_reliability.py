import json
import sys
from unittest.mock import MagicMock, Mock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import marry.monitor as monitor


@pytest.fixture
def monitor_run(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor, 'ARTIFACT_DIR', tmp_path / 'artifacts')
    monkeypatch.setattr(monitor, 'STATE_PATH', tmp_path / 'state.json')
    monkeypatch.setattr(monitor, 'sync_playwright', MagicMock())
    monkeypatch.setattr(monitor, 'required_env', Mock(return_value='unused'))
    monkeypatch.setattr(monitor, 'login', Mock())
    monkeypatch.setattr(monitor, 'select_hall', Mock())
    monkeypatch.setattr(monitor, 'parse_extra_targets', Mock(return_value=[]))
    monkeypatch.setattr(monitor, 'TARGETS', [monitor.Target(2027, 9, 4, '11:00')])
    monkeypatch.setattr(monitor, 'HALL_DAY_SCANS', [])
    monkeypatch.setattr(monitor, 'HALL_MONTH_SCANS', [])
    monkeypatch.setattr(monitor, 'read_target_status', Mock(return_value=('available', '예약가능')))
    monkeypatch.setattr(monitor, 'send_telegram', Mock())
    original_stdout = sys.stdout
    def run():
        try:
            return monitor.run()
        finally:
            sys.stdout = original_stdout
    yield run
    sys.stdout = original_stdout


def test_failed_alert_is_retried_then_acknowledged(monitor_run):
    monitor.send_telegram.side_effect = [OSError('offline'), None]
    assert monitor_run() == 1
    failed_state = monitor.load_previous_state()
    assert failed_state.targets['2027-09-04 11:00']['status'] == 'available'
    assert failed_state.pending_available == ['2027-09-04 11:00']
    assert monitor_run() == 0
    assert monitor.send_telegram.call_count == 2
    assert monitor.load_previous_state().pending_available == []
    assert monitor_run() == 0
    assert monitor.send_telegram.call_count == 2


def test_closed_slot_is_not_retried_as_available(monitor_run):
    monitor.send_telegram.side_effect = OSError('offline')
    assert monitor_run() == 1
    monitor.send_telegram.reset_mock(side_effect=True)
    monitor.read_target_status.return_value = ('unavailable', '예약마감')
    assert monitor_run() == 0
    assert monitor.load_previous_state().pending_available == []
    monitor.send_telegram.assert_not_called()


def test_pending_alert_survives_unknown_status(monitor_run):
    monitor.send_telegram.side_effect = OSError('offline')
    assert monitor_run() == 1
    monitor.send_telegram.reset_mock(side_effect=True)
    monitor.read_target_status.return_value = ('unknown', '확인불가')
    assert monitor_run() == 0
    assert monitor.load_previous_state().pending_available == ['2027-09-04 11:00']
    monitor.read_target_status.return_value = ('available', '예약가능')
    assert monitor_run() == 0
    assert monitor.load_previous_state().pending_available == []
    assert '🎉 신규!' in monitor.send_telegram.call_args.args[0]


def test_pending_alert_survives_login_failure(monitor_run):
    monitor.send_telegram.side_effect = OSError('offline')
    assert monitor_run() == 1
    monitor.send_telegram.reset_mock(side_effect=True)
    monitor.login.side_effect = RuntimeError('login failed')
    assert monitor_run() == 1
    assert monitor.load_previous_state().pending_available == ['2027-09-04 11:00']
    monitor.login.side_effect = None
    assert monitor_run() == 0
    assert monitor.load_previous_state().pending_available == []


def test_long_alert_is_not_truncated_or_acknowledged_after_partial_failure(monitor_run, monkeypatch):
    monkeypatch.setattr(monitor, 'TARGETS', [
        monitor.Target(2027, month, day, '11:00')
        for month in range(1, 13) for day in range(1, 29)
    ])
    monitor.send_telegram.side_effect = [None, OSError('second message failed')]
    assert monitor_run() == 1
    assert len(monitor.load_previous_state().pending_available) == 336
    monitor.send_telegram.reset_mock(side_effect=True)
    assert monitor_run() == 0
    sent = [call.args[0] for call in monitor.send_telegram.call_args_list]
    assert len(sent) > 1
    assert all(len(part) <= monitor.TELEGRAM_LIMIT for part in sent)
    assert '2027-12-28 11:00' in '\n'.join(sent)
    assert monitor.load_previous_state().pending_available == []


def test_old_state_without_pending_field_is_supported(monkeypatch, tmp_path):
    path = tmp_path / 'availability.json'
    path.write_text(json.dumps({'run_status': 'ok', 'targets': {}}), encoding='utf-8')
    monkeypatch.setattr(monitor, 'STATE_PATH', path)
    assert monitor.load_previous_state().pending_available == []


def test_atomic_state_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor, 'STATE_PATH', tmp_path / 'state' / 'availability.json')
    state = monitor.MonitorState('ok', 'now', pending_available=['slot'])
    monitor.save_state(state)
    assert monitor.load_previous_state() == state
    assert not monitor.STATE_PATH.with_suffix('.tmp').exists()


@pytest.mark.parametrize('message', ['a' * 9000, '\n'.join(str(i) * 90 for i in range(100))])
def test_message_split_preserves_content(message):
    parts = monitor.split_telegram_message(message)
    assert all(len(part) <= monitor.TELEGRAM_LIMIT for part in parts)
    assert ''.join(parts).replace('\n', '') == message.replace('\n', '')


def calendar_page():
    page = MagicMock()
    buttons = page.locator.return_value.locator.return_value
    buttons.count.return_value = 1
    button = buttons.nth.return_value
    button.inner_text.return_value = '28'
    button.get_attribute.return_value = ''
    button.locator.return_value.inner_text.return_value = '28'
    return page, button


def test_calendar_click_timeout_is_unknown_not_closed():
    page, button = calendar_page()
    button.click.side_effect = PlaywrightTimeoutError('loading overlay')
    with pytest.raises(RuntimeError, match='확인불가'):
        monitor.click_table_day(page, 28)


def test_explicit_closed_calendar_day_is_still_closed():
    page, button = calendar_page()
    button.locator.return_value.inner_text.return_value = '28 마감'
    assert monitor.click_table_day(page, 28) == 'closed'
    button.click.assert_not_called()


def test_per_hall_timeout_is_reported_unknown(monitor_run, monkeypatch):
    monkeypatch.setattr(monitor, 'HALL_DAY_SCANS', [monitor.HallDayScan('other', 2027, 9, 28)])
    monkeypatch.setattr(monitor, 'read_day_times', Mock(side_effect=RuntimeError('클릭 시간 초과')))
    assert monitor_run() == 0
    assert monitor.load_previous_state().targets['other 2027-09-28']['status'] == 'unknown'


def test_fixed_target_timeout_fails_run_instead_of_reporting_closed(monitor_run):
    monitor.read_target_status.side_effect = RuntimeError('클릭 시간 초과')
    assert monitor_run() == 1
    assert monitor.load_previous_state().run_status == 'error'


def test_message_split_rejects_zero_limit():
    with pytest.raises(ValueError):
        monitor.split_telegram_message('message', limit=0)

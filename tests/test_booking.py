from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import marry.book as book


@pytest.fixture
def submission_page(monkeypatch):
    monkeypatch.delenv('BOOKING_SUCCESS_SELECTOR', raising=False)
    monkeypatch.setattr(book, 'capture_screen_only', Mock())
    page = MagicMock()
    buttons = MagicMock()
    buttons.count.return_value = 1
    button = buttons.nth.return_value
    button.is_visible.return_value = True
    button.is_enabled.return_value = True
    confirmation = MagicMock()
    confirmation.count.return_value = 0
    page.get_by_text.side_effect = [buttons, confirmation]
    return SimpleNamespace(page=page, buttons=buttons, button=button, confirmation=confirmation)


def test_submission_requires_visible_completion(submission_page):
    case = submission_page
    case.confirmation.first.wait_for.side_effect = PlaywrightTimeoutError('still on form')
    assert book.submit_application(case.page, 'test') is book.SubmissionStatus.UNKNOWN
    case.button.click.assert_called_once()


def test_confirmed_submission(submission_page):
    case = submission_page
    assert book.submit_application(case.page, 'test') is book.SubmissionStatus.CONFIRMED
    case.confirmation.first.wait_for.assert_called_once_with(state='visible', timeout=15_000)


def test_click_timeout_does_not_retry(submission_page):
    case = submission_page
    case.button.click.side_effect = PlaywrightTimeoutError('request may have been sent')
    assert book.submit_application(case.page, 'test') is book.SubmissionStatus.UNKNOWN
    case.button.click.assert_called_once()
    case.confirmation.first.wait_for.assert_not_called()


@pytest.mark.parametrize('count', [0, 2])
def test_ambiguous_or_missing_submit_button_is_not_clicked(submission_page, count):
    case = submission_page
    case.buttons.count.return_value = count
    assert book.submit_application(case.page, 'test') is book.SubmissionStatus.NOT_SUBMITTED
    case.button.click.assert_not_called()


def test_old_confirmation_is_not_accepted(submission_page):
    case = submission_page
    case.confirmation.count.return_value = 1
    case.confirmation.first.is_visible.return_value = True
    assert book.submit_application(case.page, 'test') is book.SubmissionStatus.UNKNOWN
    case.button.click.assert_not_called()


def test_screenshot_failure_does_not_change_confirmed_result(submission_page, monkeypatch):
    monkeypatch.setattr(book, 'capture_screen_only', Mock(side_effect=OSError('disk full')))
    assert book.submit_application(submission_page.page, 'test') is book.SubmissionStatus.CONFIRMED


def test_custom_confirmation_selector(submission_page, monkeypatch):
    monkeypatch.setenv('BOOKING_SUCCESS_SELECTOR', '#booking-confirmation')
    case = submission_page
    case.page.locator.return_value = case.confirmation
    assert book.submit_application(case.page, 'test') is book.SubmissionStatus.CONFIRMED
    case.page.locator.assert_called_once_with('#booking-confirmation')


@pytest.mark.parametrize('text', ['신청완료', '신청', '예약이 완료되지 않았습니다'])
def test_generic_labels_and_error_text_are_not_success(text):
    assert book.SUCCESS_MESSAGE.fullmatch(text) is None


@pytest.mark.parametrize('text', ['신청이 완료되었습니다.', '예약이 정상적으로 완료되었습니다.'])
def test_explicit_success_sentence(text):
    assert book.SUCCESS_MESSAGE.fullmatch(text)


@pytest.mark.parametrize('bad_field', list(book.REQUIRED_APPLICANT_FIELDS) + ['missing'])
def test_form_failure_blocks_submission(monkeypatch, bad_field):
    monkeypatch.setattr(book, 'click_text', Mock(return_value=True))
    monkeypatch.setattr(book, 'check_all_agreements', Mock(return_value=True))
    values = {field: '성공' for field in book.REQUIRED_APPLICANT_FIELDS}
    if bad_field == 'missing':
        values.pop(book.REQUIRED_APPLICANT_FIELDS[0])
    else:
        values[bad_field] = '채우기 실패'
    monkeypatch.setattr(book, 'fill_applicant_info', Mock(return_value=values))
    assert book.proceed_to_info_form(MagicMock(), 'test') is False


def test_valid_form_can_proceed(monkeypatch):
    monkeypatch.setattr(book, 'click_text', Mock(return_value=True))
    monkeypatch.setattr(book, 'check_all_agreements', Mock(return_value=True))
    monkeypatch.setattr(book, 'fill_applicant_info', Mock(return_value={
        field: '성공' for field in book.REQUIRED_APPLICANT_FIELDS
    }))
    monkeypatch.setattr(book, 'capture_screen_only', Mock())
    assert book.proceed_to_info_form(MagicMock(), 'test') is True


@pytest.fixture
def booking_run(monkeypatch):
    playwright = MagicMock()
    monkeypatch.setattr(book, 'sync_playwright', playwright)
    monkeypatch.setattr(book, 'required_env', Mock(return_value='unused'))
    monkeypatch.setattr(book, 'login', Mock())
    monkeypatch.setattr(book, 'prepare_target', Mock())
    monkeypatch.setattr(book, 'wait_until', Mock())
    monkeypatch.setattr(book, 'attempt_target', Mock(return_value=True))
    monkeypatch.setattr(book, 'proceed_to_info_form', Mock(return_value=True))
    monkeypatch.setattr(book, 'send_telegram', Mock())
    monkeypatch.setattr(book, 'submit_application', Mock(return_value=book.SubmissionStatus.CONFIRMED))
    targets = [book.BookingTarget('hall', 2027, 9, day, '11:00') for day in (4, 5)]
    return lambda auto_submit=True: book.run(
        targets, datetime.now(book.KST), headless=True, auto_submit=auto_submit,
    )


def test_notification_failure_does_not_attempt_next_target(booking_run):
    book.send_telegram.side_effect = OSError('offline')
    assert booking_run() is True
    book.submit_application.assert_called_once()
    book.attempt_target.assert_called_once()


@pytest.mark.parametrize('status', [book.SubmissionStatus.UNKNOWN, book.SubmissionStatus.NOT_SUBMITTED])
def test_unconfirmed_submission_stops_with_failure(booking_run, status):
    book.submit_application.return_value = status
    assert booking_run() is False
    book.attempt_target.assert_called_once()
    assert all('신청 완료 확인!' not in call.args[0] for call in book.send_telegram.call_args_list)


def test_unexpected_post_submission_error_never_tries_next_target(booking_run):
    book.submit_application.side_effect = RuntimeError('unexpected')
    with pytest.raises(RuntimeError):
        booking_run()
    book.attempt_target.assert_called_once()


def test_closed_first_target_can_fall_back(booking_run):
    book.attempt_target.side_effect = [False, True]
    assert booking_run() is True
    assert book.attempt_target.call_count == 2
    book.submit_application.assert_called_once()


def test_all_targets_failed(booking_run):
    book.attempt_target.return_value = False
    assert booking_run() is False
    book.submit_application.assert_not_called()


def test_dry_run_never_submits(booking_run):
    assert booking_run(auto_submit=False) is True
    book.submit_application.assert_not_called()


def test_prepare_target_called_once_with_first_target(booking_run):
    booking_run()
    book.prepare_target.assert_called_once()
    assert book.prepare_target.call_args.args[1].day == 4


def test_reselect_hall_only_when_hall_changes(monkeypatch):
    """오픈 전에 미리 이동해둔 첫 지망은 다시 홀을 선택하지 않고,
    지망마다 홀이 바뀔 때만 다시 선택하도록 한다."""
    playwright = MagicMock()
    monkeypatch.setattr(book, 'sync_playwright', playwright)
    monkeypatch.setattr(book, 'required_env', Mock(return_value='unused'))
    monkeypatch.setattr(book, 'login', Mock())
    monkeypatch.setattr(book, 'prepare_target', Mock())
    monkeypatch.setattr(book, 'wait_until', Mock())
    monkeypatch.setattr(book, 'attempt_target', Mock(side_effect=[False, False]))
    monkeypatch.setattr(book, 'proceed_to_info_form', Mock(return_value=True))
    monkeypatch.setattr(book, 'send_telegram', Mock())

    targets = [
        book.BookingTarget('서초사옥', 2027, 11, 21, '11:00'),
        book.BookingTarget('삼성금융연수원', 2027, 11, 20, '13:00'),
    ]
    book.run(targets, datetime.now(book.KST), headless=True, auto_submit=True)

    calls = book.attempt_target.call_args_list
    assert calls[0].kwargs['reselect_hall'] is False
    assert calls[1].kwargs['reselect_hall'] is True


def test_previous_month_handles_year_rollover():
    assert book.previous_month(2027, 11) == (2027, 10)
    assert book.previous_month(2027, 1) == (2026, 12)


@pytest.mark.parametrize(('success', 'exit_code'), [(True, 0), (False, 1)])
def test_main_propagates_result(monkeypatch, success, exit_code):
    monkeypatch.setattr(book, 'parse_targets', Mock(return_value=[]))
    monkeypatch.setattr(book, 'required_env', lambda name: {
        'BOOKING_OPEN_AT': '2026-09-15T14:00:00',
        'BOOKING_HEADLESS': 'true', 'BOOKING_AUTO_SUBMIT': 'true',
    }[name])
    monkeypatch.setattr(book, 'run', Mock(return_value=success))
    with pytest.raises(SystemExit) as exc:
        book.main()
    assert exc.value.code == exit_code

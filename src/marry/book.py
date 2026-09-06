"""오픈 순간(예: 9/15 14:00) 우선순위 지망 목록을 순서대로 시도하는 스크립트.

동시접속이 몰리는 오픈 순간에는 1지망이 이미 마감돼 있을 가능성이 높으므로,
지망 목록을 우선순위대로 두고 마감이면 즉시 다음 지망으로 넘어간다.

BOOKING_AUTO_SUBMIT=true면 정보입력 이후 '신청' 버튼까지 자동으로 눌러 사람 개입 없이
진행하고 완료 문구를 확인한다. 결과가 불확실하면 추가 신청 없이 멈춘다. false면
정보입력 화면 도달까지만 자동화하고 '신청'은 누르지 않은 채 멈춘다 - headless=False로
로컬에서 직접 띄워서 실행하면, 정보입력까지 자동으로 도달한 그 화면을 사람이 그대로
이어받아 '신청'을 누를 수도 있다.

트리거(예: GitHub Actions)는 오픈 시각보다 몇 분 앞서 실행을 시작해야 한다 - 이 스크립트
안에서 wait_until()로 실제 목표 시각까지 대기하는 식으로 정밀도를 맞춘다. schedule/외부
크론 자체의 실행 시각은 초 단위로 믿을 수 없다.

로그인 세션 유지시간이 10분이라, 트리거 시각에 바로 로그인해두면 오픈 순간에는 이미
세션이 끊겨 있을 수 있다(사용자 제보). 그래서 로그인 자체도 오픈 시각 직전(기본 5분 전)
까지 미뤘다가 한다. 또한 오픈 시각이 되어도 실제로는 몇 초 뒤에나 다음 달로 넘어갈 수
있다는 후기가 있어(2027-11-21 케이스), 목표 달 진입은 정각 이후 짧게 재시도한다.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from marry.monitor import (
    click_available_time,
    click_day_button,
    click_text,
    go_to_target_month,
    login,
    required_env,
    select_hall,
    send_telegram,
)
from marry.rehearse import (
    REQUIRED_APPLICANT_FIELDS,
    capture_screen_only,
    check_all_agreements,
    fill_applicant_info,
)

KST = ZoneInfo("Asia/Seoul")

# 로그인 세션 유지시간(10분)보다 여유 있게, 오픈 시각 이 시간 전에 로그인한다.
LOGIN_LEAD_SECONDS = 5 * 60

# 오픈 정각이 되어도 실제로 다음 달이 열리기까지 몇 초 걸릴 수 있어(후기 기반),
# 목표 달 진입을 이 시간 동안 짧은 간격으로 재시도한다.
MONTH_OPEN_RETRY_SECONDS = 20.0


@dataclass(frozen=True)
class BookingTarget:
    hall: str
    year: int
    month: int
    day: int
    time_text: str

    @property
    def label(self) -> str:
        return f"{self.hall} {self.year}-{self.month:02d}-{self.day:02d} {self.time_text}"


def parse_targets() -> list[BookingTarget]:
    """BOOKING_TARGETS_JSON 환경변수를 우선순위 지망 목록으로 파싱한다.

    형식(우선순위 순서대로 배열): 예)
    [{"hall": "서초사옥", "date": "2027-11-13", "time": "13:00"},
     {"hall": "삼성금융연수원", "date": "2027-11-20", "time": "11:00"}]
    """
    raw = required_env("BOOKING_TARGETS_JSON")
    entries = json.loads(raw)
    if not isinstance(entries, list) or not entries:
        raise ValueError("BOOKING_TARGETS_JSON은 최소 1개 이상의 지망을 담은 배열이어야 합니다.")

    targets: list[BookingTarget] = []
    for entry in entries:
        year_str, month_str, day_str = str(entry["date"]).split("-")
        targets.append(
            BookingTarget(
                hall=str(entry["hall"]),
                year=int(year_str),
                month=int(month_str),
                day=int(day_str),
                time_text=str(entry["time"]),
            )
        )
    return targets


def wait_until(target: datetime) -> None:
    """목표 시각까지 대기한다. 멀 때는 길게 자고, 임박하면 촘촘히 깨어나 오차를 줄인다."""
    while True:
        remaining = (target - datetime.now(KST)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30) if remaining > 5 else 0.05)


def previous_month(year: int, month: int) -> tuple[int, int]:
    return (year, month - 1) if month > 1 else (year - 1, 12)


def prepare_target(page, target: BookingTarget) -> None:
    """오픈 전에 미리 홀을 선택하고 목표 달 바로 전 달까지 이동해 대기한다.
    실제 신청 화면에서도 오픈 시각까지는 다음 달로 못 넘어가고, 정각이 되면
    '다음달' 화살표 한 번 누른 뒤 날짜/시간을 클릭하는 방식이라는 걸 확인했다
    (2027-11-21 오픈 케이스). 오픈 순간에 할 일을 최소화하기 위해, 대기 중에
    미리 목표 달 전달까지 이동해두고 정각에는 '다음달' 한 번 + 날짜/시간
    클릭만 하면 되도록 한다."""
    prep_year, prep_month = previous_month(target.year, target.month)
    select_hall(page, target.hall)
    go_to_target_month(page, prep_year, prep_month)
    print(f"사전 준비 완료: {target.hall} {prep_year}-{prep_month:02d}월까지 미리 이동해 대기합니다.")


def wait_for_month_open(page, target: BookingTarget) -> None:
    """목표 달로 이동될 때까지 짧은 간격으로 재시도한다. 오픈 정각이 지나도
    실제로는 몇 초 뒤에나 다음 달이 열릴 수 있어(후기 기반), go_to_target_month가
    실패해도 바로 포기하지 않는다."""
    deadline = time.monotonic() + MONTH_OPEN_RETRY_SECONDS
    while True:
        try:
            go_to_target_month(page, target.year, target.month)
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def attempt_target(page, target: BookingTarget, *, reselect_hall: bool) -> bool:
    """지망 하나를 시도한다. 시간대 클릭까지 성공하면 True.
    reselect_hall이 False면 이미 해당 홀 페이지에 있다고 보고 다시 이동하지 않는다
    (오픈 직전에 prepare_target으로 미리 가 있는 지망을 그대로 이어서 쓰기 위함)."""
    print(f"[시도] {target.label}")
    if reselect_hall:
        select_hall(page, target.hall)
    wait_for_month_open(page, target)

    if click_day_button(page, target.year, target.month, target.day) == "closed":
        print(f"[실패] {target.label} - 날짜 마감")
        return False

    if not click_available_time(page, target.time_text):
        print(f"[실패] {target.label} - 시간대 마감")
        return False

    print(f"[성공] {target.label} - 시간대 선택 완료")
    return True


def proceed_to_info_form(page, label: str) -> bool:
    """시간대 선택 이후 다음/동의/다음을 거쳐 정보입력 화면까지 진행한다."""
    page.wait_for_timeout(1_000)
    if not click_text(page, ["다음"], timeout=5_000):
        print(f"[{label}] '다음' 버튼을 찾지 못함")
        return False

    page.wait_for_timeout(1_000)
    if not check_all_agreements(page):
        print(f"[{label}] 동의 체크 실패")
        return False

    page.wait_for_timeout(500)
    if not click_text(page, ["다음"], timeout=5_000):
        print(f"[{label}] 동의 단계 '다음' 버튼을 찾지 못함")
        return False

    page.wait_for_timeout(1_000)
    results = fill_applicant_info(page)
    failed = [name for name in REQUIRED_APPLICANT_FIELDS if results.get(name) != "성공"]
    if failed:
        print(f"[{label}] 필수 입력 확인 실패: {', '.join(failed)}")
        return False
    capture_booking_screen(page, f"book-{label}")
    return True


class SubmissionStatus(Enum):
    CONFIRMED = "confirmed"
    NOT_SUBMITTED = "not_submitted"
    UNKNOWN = "unknown"


# 메뉴의 '신청완료' 라벨과 구분되는 명시적인 완료 문장만 인정한다.
SUCCESS_MESSAGE = re.compile(
    r"^\s*(?:신청|예약)(?:이|가)?\s*(?:정상적으로\s*)?완료되었습니다[.!]?\s*$"
)


def notify_booking(message: str) -> bool:
    """알림 실패는 예약 흐름으로 전파하지 않는다."""
    try:
        send_telegram(message)
        return True
    except Exception as exc:  # noqa: BLE001 - notification must never retry a booking
        print(f"예약 알림 전송 실패: {type(exc).__name__}", file=sys.stderr)
        return False


def capture_booking_screen(page, label: str) -> None:
    try:
        capture_screen_only(page, label)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not change booking outcome
        print(f"[{label}] 화면 저장 실패: {type(exc).__name__}", file=sys.stderr)


def submit_application(page, label: str) -> SubmissionStatus:
    """한 번만 제출하고 완료를 확인한다. 클릭 이후의 오류는 결과 불명으로 처리한다."""
    try:
        candidates = page.get_by_text(re.compile(r"^\s*신청\s*$"))
        visible = [candidates.nth(i) for i in range(candidates.count())
                   if candidates.nth(i).is_visible()]
        if len(visible) != 1:
            print(f"[{label}] '신청' 버튼을 하나로 특정하지 못함")
            return SubmissionStatus.NOT_SUBMITTED
        button = visible[0]
        if not button.is_enabled():
            return SubmissionStatus.NOT_SUBMITTED
        selector = os.getenv("BOOKING_SUCCESS_SELECTOR", "").strip()
        confirmation = page.locator(selector) if selector else page.get_by_text(SUCCESS_MESSAGE)
        # 기존 화면의 완료 라벨을 이번 제출 결과로 잘못 인정하지 않는다.
        if confirmation.count() and confirmation.first.is_visible():
            print(f"[{label}] 제출 전부터 완료 표시가 있어 결과를 확인할 수 없음")
            return SubmissionStatus.UNKNOWN
    except Exception as exc:  # noqa: BLE001 - no click has happened yet
        print(f"[{label}] 제출 준비 실패: {type(exc).__name__}")
        return SubmissionStatus.NOT_SUBMITTED

    try:
        # click()의 타임아웃도 서버에 요청이 도착한 뒤 발생할 수 있으므로 재시도하지 않는다.
        button.click(timeout=5_000)
        confirmation.first.wait_for(state="visible", timeout=15_000)
    except Exception as exc:  # noqa: BLE001 - submission may already have succeeded
        print(f"[{label}] 제출 결과 확인불가: {type(exc).__name__}")
        capture_booking_screen(page, f"book-{label}-unconfirmed")
        return SubmissionStatus.UNKNOWN
    capture_booking_screen(page, f"book-{label}-submitted")
    print(f"[{label}] 신청 완료 표시 확인")
    return SubmissionStatus.CONFIRMED


def run(
    targets: list[BookingTarget], open_at: datetime, *, headless: bool, auto_submit: bool
) -> bool:
    employee_id = required_env("SAMSUNG_WEDDING_EMPLOYEE_ID")
    password = required_env("SAMSUNG_WEDDING_EMPLOYEE_PASSWORD")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul")
        page = context.new_page()
        try:
            login_at = open_at - timedelta(seconds=LOGIN_LEAD_SECONDS)
            print(f"로그인 세션 유지시간을 감안해 {login_at.isoformat()}까지 대기합니다.")
            wait_until(login_at)
            login(page, employee_id, password)

            current_hall: str | None = None
            if targets:
                prepare_target(page, targets[0])
                current_hall = targets[0].hall

            print(f"{open_at.isoformat()}까지 대기합니다.")
            wait_until(open_at)
            print("오픈 시각 도달 - 지망 순서대로 시도합니다.")

            for index, target in enumerate(targets, start=1):
                label = f"target{index}"
                try:
                    reselect_hall = target.hall != current_hall
                    current_hall = target.hall
                    if not attempt_target(page, target, reselect_hall=reselect_hall):
                        continue
                    if not proceed_to_info_form(page, label):
                        continue

                    if not auto_submit:
                        notify_booking(
                            f"[웨딩홀 예약] {index}지망 {target.label} 정보입력 화면까지 진입 성공.\n"
                            "지금 바로 확인해서 최종 신청을 완료하세요."
                        )
                        return True

                except Exception as exc:  # noqa: BLE001 - only pre-submission failures retry
                    print(f"[{target.label}] 제출 전 처리 오류: {type(exc).__name__}")
                    continue

                # 제출부터는 다음 지망으로 넘어가는 예외 처리 범위 밖에서 실행한다.
                result = submit_application(page, label)
                if result is SubmissionStatus.CONFIRMED:
                    notify_booking(f"[웨딩홀 예약] {index}지망 {target.label} 신청 완료 확인!")
                    return True
                if result is SubmissionStatus.UNKNOWN:
                    notify_booking(
                        f"[웨딩홀 예약] {index}지망 {target.label} 제출 결과를 확인하지 못했습니다.\n"
                        "중복 신청 방지를 위해 중단했습니다. 예약 내역을 직접 확인하세요."
                    )
                    return False
                notify_booking(
                    f"[웨딩홀 예약] {index}지망 {target.label} '신청' 버튼을 누르지 못했습니다."
                )
                return False
            notify_booking("[웨딩홀 예약] 모든 지망에서 신청 화면 진입 또는 입력 확인에 실패했습니다.")
            return False
        finally:
            if not headless and not auto_submit:
                print("headless=False - 신청 화면을 이어서 진행하려면 브라우저 창을 직접 확인하세요.")
                page.wait_for_timeout(600_000)
            context.close()
            browser.close()


def main() -> None:
    targets = parse_targets()
    open_at = datetime.fromisoformat(required_env("BOOKING_OPEN_AT")).replace(tzinfo=KST)
    headless = required_env("BOOKING_HEADLESS").strip().lower() not in {"0", "false", "no"}
    auto_submit = required_env("BOOKING_AUTO_SUBMIT").strip().lower() not in {"0", "false", "no"}
    success = run(targets, open_at, headless=headless, auto_submit=auto_submit)
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()


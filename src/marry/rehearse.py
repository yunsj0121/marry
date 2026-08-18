"""신청 화면 구조를 파악하기 위한 1회성 리허설 스크립트.

실제로 열려 있는 날짜/시간을 클릭해 신청 흐름에 진입하되, 그 다음 화면을
스크린샷과 텍스트로 남기고 즉시 멈춘다. 동의/정보입력/최종제출 단계는 아직
구조를 모르므로 이 스크립트는 그 단계를 자동으로 진행하지 않는다.
"""

from __future__ import annotations

import os
import re
import sys

from playwright.sync_api import sync_playwright

from marry.monitor import (
    ARTIFACT_DIR,
    click_available_time,
    click_day_button,
    go_to_target_month,
    login,
    required_env,
    select_hall,
)


def dump_screen(page, label: str) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = ARTIFACT_DIR / f"rehearsal-{label}.png"
    page.screenshot(path=screenshot_path, full_page=True)
    print(f"[{label}] URL={page.url}")
    try:
        body_text = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=2_000)).strip()
        print(f"[{label}] 본문 텍스트(최대 2000자): {body_text[:2000]}")
    except Exception as exc:  # noqa: BLE001 - 진단 실패는 무시하고 계속 진행
        print(f"[{label}] 본문 텍스트 추출 실패: {exc}", file=sys.stderr)
    try:
        html = page.evaluate("() => document.body.innerHTML")
        print(f"[{label}] body HTML(최대 4000자): {html[:4000]}")
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] HTML 추출 실패: {exc}", file=sys.stderr)


def main() -> None:
    year = int(required_env("REHEARSE_YEAR"))
    month = int(required_env("REHEARSE_MONTH"))
    day = int(required_env("REHEARSE_DAY"))
    time_text = required_env("REHEARSE_TIME")
    hall = os.getenv("REHEARSE_HALL", "").strip()

    employee_id = required_env("SAMSUNG_WEDDING_EMPLOYEE_ID")
    password = required_env("SAMSUNG_WEDDING_EMPLOYEE_PASSWORD")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul")
        page = context.new_page()
        try:
            login(page, employee_id, password)
            if hall:
                select_hall(page, hall)
            else:
                select_hall(page)

            go_to_target_month(page, year, month)
            day_result = click_day_button(page, year, month, day)
            if day_result == "closed":
                raise RuntimeError(f"{year}-{month:02d}-{day:02d}가 이미 마감 상태라 클릭할 수 없습니다.")
            dump_screen(page, "01-day-panel")

            if not click_available_time(page, time_text):
                raise RuntimeError(f"{time_text} 시간대를 클릭하지 못했습니다 - 이미 마감됐을 수 있습니다.")

            page.wait_for_timeout(1_500)
            dump_screen(page, "02-after-time-click")

            print("리허설 완료: 여기서 멈춥니다. 동의/정보입력/제출은 진행하지 않았습니다.")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()

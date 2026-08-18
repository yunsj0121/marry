"""신청 화면 구조를 파악하기 위한 1회성 리허설 스크립트.

실제로 열려 있는 날짜/시간을 클릭해 신청 흐름에 진입하되, 그 다음 화면을
스크린샷과 텍스트로 남기고 즉시 멈춘다. 동의/정보입력/최종제출 단계는 아직
구조를 모르므로 이 스크립트는 그 단계를 자동으로 진행하지 않는다.
"""

from __future__ import annotations

import os
import re
import sys

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from marry.monitor import (
    ARTIFACT_DIR,
    click_available_time,
    click_day_button,
    click_text,
    go_to_target_month,
    login,
    required_env,
    select_hall,
)


def dump_selected_day(page, label: str) -> None:
    """button[data-date] 중 실제로 "선택됨/활성" class가 붙은 날짜를 정확히 찾는다.
    body 전체 텍스트 덤프는 접근성 라벨과 숫자가 뒤섞여 어느 날짜가 진짜
    선택됐는지 구분이 안 되므로, data-date 속성과 class를 직접 읽는다."""
    buttons = page.locator("button[data-date]")
    total = buttons.count()
    entries = []
    for index in range(total):
        button = buttons.nth(index)
        date_attr = button.get_attribute("data-date") or ""
        class_attr = button.get_attribute("class") or ""
        if "sel" in class_attr or "active" in class_attr or "on" in class_attr:
            entries.append(f"{date_attr}(class={class_attr!r})")
    print(f"[{label}] 선택된 것으로 보이는 날짜 버튼(총 {total}개 중): {entries}")


def dump_clickable_elements(page, label: str) -> None:
    """다음 단계로 넘어가는 버튼을 찾기 위해, 보이는 클릭 가능 요소의 텍스트를 나열한다.
    달력 날짜 셀(button[data-date])은 최대 31개나 차지해 진짜 관심있는 버튼을
    밀어낼 수 있어 제외한다."""
    clickable = page.locator("button, a, [role=button], input[type=submit], input[type=button]")
    total = clickable.count()
    labels = []
    for index in range(total):
        element = clickable.nth(index)
        try:
            if element.get_attribute("data-date") is not None:
                continue
            if not element.is_visible():
                continue
            text = re.sub(r"\s+", " ", element.inner_text(timeout=300)).strip()
            if not text:
                text = element.get_attribute("value") or element.get_attribute("aria-label") or ""
            if text:
                labels.append(text)
        except Exception:  # noqa: BLE001
            continue
    print(f"[{label}] 날짜 셀 제외 클릭가능요소(전체 {total}개 중 {len(labels)}개): {labels}")

    for keyword in ["다음", "신청", "확인", "선택완료", "예약", "동의"]:
        matches = page.get_by_text(re.compile(re.escape(keyword)))
        count = matches.count()
        if not count:
            continue
        visible_texts = []
        for index in range(count):
            candidate = matches.nth(index)
            try:
                if candidate.is_visible():
                    visible_texts.append(re.sub(r"\s+", " ", candidate.inner_text(timeout=300)).strip())
            except Exception:  # noqa: BLE001
                continue
        if visible_texts:
            print(f"[{label}] '{keyword}' 포함 보이는 요소({len(visible_texts)}개): {visible_texts}")


def dump_time_area_html(page, label: str, time_text: str) -> None:
    """클릭해도 화면이 안 바뀌어서, "17:00" 텍스트 주변 실제 마크업을 단계별로
    덤프한다 - 숨겨진 라디오나 별도 확인 버튼이 있는지 확인하기 위함."""
    loc = page.get_by_text(re.compile(rf"^\s*{re.escape(time_text)}\s*$")).first
    if not loc.count():
        print(f"[{label}] '{time_text}' 요소를 찾지 못해 HTML 덤프 불가")
        return
    for levels_up in [2, 3, 4]:
        ancestor = loc.locator("xpath=" + "/.." * levels_up)
        try:
            html = ancestor.evaluate("el => el.outerHTML")
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] {levels_up}단계 상위 HTML 추출 실패: {exc}", file=sys.stderr)
            continue
        print(f"[{label}] '{time_text}' {levels_up}단계 상위 HTML(최대 2500자): {html[:2500]}")


def dump_checkbox_markup(page, label: str) -> None:
    """동의 체크박스 실제 마크업을 확인한다. 시간대 라디오와 같은 위젯 패턴(input+label)인지
    먼저 진단하기 위해, 페이지의 모든 checkbox input과 "전체 동의" 주변 HTML을 덤프한다."""
    checkboxes = page.locator("input[type=checkbox]")
    total = checkboxes.count()
    entries = []
    for index in range(total):
        cb = checkboxes.nth(index)
        entries.append(
            {
                "id": cb.get_attribute("id"),
                "name": cb.get_attribute("name"),
                "checked": cb.get_attribute("checked"),
            }
        )
    print(f"[{label}] 체크박스 input 목록(총 {total}개): {entries}")

    loc = page.get_by_text(re.compile(r"^\s*전체\s*동의\s*$")).first
    if not loc.count():
        print(f"[{label}] '전체 동의' 요소를 찾지 못해 HTML 덤프 불가")
        return
    for levels_up in [2, 3, 4]:
        ancestor = loc.locator("xpath=" + "/.." * levels_up)
        try:
            html = ancestor.evaluate("el => el.outerHTML")
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] 전체동의 {levels_up}단계 상위 HTML 추출 실패: {exc}", file=sys.stderr)
            continue
        print(f"[{label}] 전체동의 {levels_up}단계 상위 HTML(최대 2000자): {html[:2000]}")


def check_all_agreements(page) -> bool:
    """"전체 동의" 체크박스를 클릭해 모든 동의 항목을 체크한다.
    실제 신청/정보입력은 별도 단계이며 이 함수는 동의 체크만 담당한다."""
    checkboxes = page.locator("input[type=checkbox]")
    total = checkboxes.count()
    for index in range(total):
        cb = checkboxes.nth(index)
        cb_id = (cb.get_attribute("id") or "").lower()
        cb_name = (cb.get_attribute("name") or "").lower()
        if "all" in cb_id or "all" in cb_name or "tot" in cb_id or "tot" in cb_name:
            raw_id = cb.get_attribute("id") or ""
            label = page.locator(f'label[for="{raw_id}"]') if raw_id else None
            for candidate in ([label, cb] if label is not None else [cb]):
                try:
                    candidate.first.click(timeout=2_000)
                    print(f"전체 동의 체크박스 클릭 성공 (id={raw_id!r})")
                    return True
                except PlaywrightTimeoutError:
                    continue

    if click_text(page, ["전체 동의"], timeout=2_000):
        print("전체 동의 텍스트 클릭 성공(폴백)")
        return True
    print("전체 동의 체크박스를 클릭하지 못함")
    return False


def dump_screen(page, label: str, time_text: str | None = None) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = ARTIFACT_DIR / f"rehearsal-{label}.png"
    page.screenshot(path=screenshot_path, full_page=True)
    print(f"[{label}] URL={page.url}")
    dump_selected_day(page, label)
    dump_clickable_elements(page, label)
    if time_text:
        dump_time_area_html(page, label, time_text)
    try:
        body_text = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=2_000)).strip()
        print(f"[{label}] 본문 텍스트(최대 1500자): {body_text[:1500]}")
    except Exception as exc:  # noqa: BLE001 - 진단 실패는 무시하고 계속 진행
        print(f"[{label}] 본문 텍스트 추출 실패: {exc}", file=sys.stderr)


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
            dump_screen(page, "01-day-panel", time_text=time_text)

            if not click_available_time(page, time_text):
                raise RuntimeError(f"{time_text} 시간대를 클릭하지 못했습니다 - 이미 마감됐을 수 있습니다.")

            page.wait_for_timeout(1_500)
            dump_screen(page, "02-after-time-click", time_text=time_text)

            if click_text(page, ["다음"], timeout=3_000):
                page.wait_for_timeout(1_500)
                dump_screen(page, "03-after-next-click")
                print("'다음' 버튼 클릭 완료 - 02 동의 단계로 보이는 화면을 남겼습니다.")

                dump_checkbox_markup(page, "03-agreement-before-check")
                if check_all_agreements(page):
                    page.wait_for_timeout(500)
                    dump_checkbox_markup(page, "04-agreement-after-check")
                    if click_text(page, ["다음"], timeout=3_000):
                        page.wait_for_timeout(1_500)
                        dump_screen(page, "05-after-agreement-next-click")
                        print(
                            "동의 단계 '다음' 클릭 완료 - 03 정보 입력으로 보이는 화면을 남겼습니다. "
                            "정보입력/최종제출은 진행하지 않았습니다."
                        )
                    else:
                        print("동의 단계 '다음' 버튼을 찾지 못해 클릭하지 않았습니다.")
                else:
                    print("동의 체크박스를 클릭하지 못해 다음 단계로 진행하지 않았습니다.")
            else:
                print("'다음' 버튼을 찾지 못해 클릭하지 않았습니다.")

            print("리허설 완료: 여기서 멈춥니다. 정보입력/최종제출은 진행하지 않았습니다.")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()

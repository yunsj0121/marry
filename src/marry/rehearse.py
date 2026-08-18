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


def dump_info_form_fields(page, label: str) -> None:
    """03 정보 입력 화면의 실제 input/select 마크업을 파악한다.
    아직 값을 채우지 않은 상태에서 구조만 읽는다 - 잘못된 값을 잘못된 필드에
    입력하지 않도록, 채우기 전에 항상 먼저 구조를 확인한다."""
    inputs = page.locator("input")
    total = inputs.count()
    entries = []
    for index in range(total):
        el = inputs.nth(index)
        entries.append(
            {
                "type": el.get_attribute("type"),
                "id": el.get_attribute("id"),
                "name": el.get_attribute("name"),
                "placeholder": el.get_attribute("placeholder"),
                "checked": el.get_attribute("checked"),
            }
        )
    print(f"[{label}] input 요소 목록(총 {total}개, value는 개인정보라 생략): {entries}")

    selects = page.locator("select")
    stotal = selects.count()
    sentries = []
    for index in range(stotal):
        el = selects.nth(index)
        sentries.append({"id": el.get_attribute("id"), "name": el.get_attribute("name")})
    print(f"[{label}] select 요소 목록(총 {stotal}개): {sentries}")

    for keyword in ["생년월일", "부서명", "휴대전화번호", "구분", "신랑 성명", "신부 성명", "이메일"]:
        loc = page.get_by_text(re.compile(re.escape(keyword))).first
        if not loc.count():
            print(f"[{label}] '{keyword}' 요소를 찾지 못함")
            continue
        for levels_up in [2, 3]:
            ancestor = loc.locator("xpath=" + "/.." * levels_up)
            try:
                html = ancestor.evaluate("el => el.outerHTML")
            except Exception as exc:  # noqa: BLE001
                print(f"[{label}] '{keyword}' {levels_up}단계 상위 HTML 추출 실패: {exc}", file=sys.stderr)
                continue
            print(f"[{label}] '{keyword}' {levels_up}단계 상위 HTML(최대 1200자): {html[:1200]}")


def fill_applicant_info(page) -> dict[str, str]:
    """개인정보 필드를 환경변수 값으로 채운다. 실제 값(생년월일/이름/전화번호 등)은
    절대 로그에 출력하지 않고, 필드별 성공/실패 여부만 반환한다."""
    results: dict[str, str] = {}

    def fill_text(selector: str, value: str, field_name: str) -> None:
        if not value:
            results[field_name] = "값 없음(건너뜀)"
            return
        loc = page.locator(selector)
        if not loc.count():
            results[field_name] = "요소를 찾지 못함"
            return
        try:
            loc.first.fill(value, timeout=2_000)
            actual = loc.first.input_value(timeout=1_000)
            results[field_name] = "성공" if actual == value else "값이 기대와 다름(포맷터가 값을 바꿨을 수 있음)"
        except PlaywrightTimeoutError:
            results[field_name] = "채우기 실패(타임아웃)"

    fill_text("#wedgAplcnsBird", os.getenv("APPLICANT_BIRTHDATE_YYYYMMDD", "").strip(), "생년월일")
    fill_text("#wedgAplcnsDeptNm", os.getenv("APPLICANT_DEPARTMENT", "").strip(), "부서명")
    fill_text("#wedgAplcnsEmadre", os.getenv("APPLICANT_EMAIL_LOCAL", "").strip(), "이메일")
    fill_text("#wedgAplcnsMpnoeB", os.getenv("APPLICANT_PHONE_SUFFIX", "").strip(), "휴대전화번호")
    fill_text("#wedgAplcRlpplFnm1", os.getenv("GROOM_NAME", "").strip(), "신랑 성명")
    fill_text("#wedgAplcRlpplFnm2", os.getenv("BRIDE_NAME", "").strip(), "신부 성명")

    role = os.getenv("APPLICANT_ROLE", "").strip()
    role_id = {"부모": "fi_rd_parent", "신랑": "fi_rd_groom", "신부": "fi_rd_bride"}.get(role)
    if role_id:
        try:
            page.locator(f'label[for="{role_id}"]').first.click(timeout=2_000)
            results["구분"] = "성공"
        except PlaywrightTimeoutError:
            results["구분"] = "클릭 실패(타임아웃)"
    else:
        results["구분"] = "값 없음(건너뜀)"

    print(f"[07-info-form-filled] 필드 채우기 결과(값 자체는 개인정보라 표시 안 함): {results}")
    return results


def capture_screen_only(page, label: str) -> None:
    """텍스트 덤프 없이 스크린샷만 남긴다 - 이 시점 화면에는 실제 개인정보가
    입력되어 있어 로그에 텍스트로 남기지 않기 위함."""
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = ARTIFACT_DIR / f"rehearsal-{label}.png"
    page.screenshot(path=screenshot_path, full_page=True)
    print(f"[{label}] URL={page.url} (개인정보 노출 방지를 위해 텍스트 덤프는 생략, 스크린샷만 저장)")


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
                        dump_info_form_fields(page, "06-info-form-fields")
                        print(
                            "동의 단계 '다음' 클릭 완료 - 03 정보 입력으로 보이는 화면을 남겼습니다. "
                            "정보입력/최종제출은 진행하지 않았습니다."
                        )

                        personal_env_keys = [
                            "APPLICANT_BIRTHDATE_YYYYMMDD",
                            "APPLICANT_DEPARTMENT",
                            "APPLICANT_EMAIL_LOCAL",
                            "APPLICANT_PHONE_SUFFIX",
                            "GROOM_NAME",
                            "BRIDE_NAME",
                            "APPLICANT_ROLE",
                        ]
                        if any(os.getenv(key) for key in personal_env_keys):
                            fill_applicant_info(page)
                            page.wait_for_timeout(500)
                            capture_screen_only(page, "07-after-info-fill")
                            print(
                                "정보 입력 필드까지 채웠습니다. '신청' 버튼은 누르지 않았습니다 - 여기서 멈춥니다."
                            )
                        else:
                            print("개인정보 환경변수가 없어 정보 입력 필드는 채우지 않았습니다.")
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

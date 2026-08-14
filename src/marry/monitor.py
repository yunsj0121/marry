from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

LOGIN_URL = "https://s-wedding.samsungcard.com/login/UWDDWSCO02M1.jsp"
APPLICATION_URL = "https://s-wedding.samsungcard.com/internal/add-apply/UWDDWSWH04M1.jsp"
TARGET_HALL = "서초사옥"
TARGET_YEAR = 2027
TARGET_MONTH = 8
TARGET_DAY = 28
TARGET_TIME = "17:00"
STATE_PATH = Path("state/availability.json")
ARTIFACT_DIR = Path("artifacts")


@dataclass
class MonitorState:
    status: str
    checked_at: str
    detail: str = ""


def required_env(name: str) -> str:
    value = os.getenv(name)
    value = value.strip() if value else ""
    if not value:
        raise RuntimeError(f"필수 GitHub Secret이 없습니다: {name}")
    return value


def click_text(page: Page, candidates: list[str], timeout: int = 4_000) -> bool:
    for frame in page.frames:
        for text in candidates:
            matches = frame.get_by_text(re.compile(rf"^\s*{re.escape(text)}\s*$"))
            for index in range(matches.count()):
                target = matches.nth(index)
                try:
                    if target.is_visible():
                        target.click(timeout=timeout)
                        return True
                except PlaywrightTimeoutError:
                    pass
    return False


def has_visible_text(page: Page, pattern: re.Pattern[str]) -> bool:
    for frame in page.frames:
        matches = frame.get_by_text(pattern)
        for index in range(matches.count()):
            if matches.nth(index).is_visible():
                return True
    return False


def fill_first(page: Page, selectors: list[str], value: str) -> None:
    for frame in page.frames:
        for selector in selectors:
            field = frame.locator(selector).first
            try:
                field.fill(value, timeout=2_000)
                if field.input_value() == value:
                    return
            except (PlaywrightTimeoutError, AssertionError):
                pass
    raise RuntimeError("로그인 입력란을 찾거나 입력하지 못했습니다.")


def handle_security_page(page: Page) -> bool:
    if not has_visible_text(page, re.compile(r"보안프로그램\s*설치여부")):
        return False
    (ARTIFACT_DIR / "security-page.html").write_text(
        page.content(), encoding="utf-8"
    )
    radios = page.locator("input[type=radio]")
    selected = False
    if radios.count() >= 2:
        try:
            radios.last.evaluate(
                """element => {
                    element.checked = true;
                    element.dispatchEvent(new Event('input', { bubbles: true }));
                    element.dispatchEvent(new Event('change', { bubbles: true }));
                }"""
            )
            selected = True
        except Exception:  # noqa: BLE001 - fall back to clicking the card
            pass
    if not selected:
        headings = page.get_by_text(re.compile(r"^\s*설치하지\s*않음\s*$"))
        for index in range(headings.count()):
            no_install = headings.nth(index)
            if not no_install.is_visible():
                continue
            for levels_up in [3, 2, 1, 0]:
                target = no_install if levels_up == 0 else no_install.locator(
                    "xpath=" + "/.." * levels_up
                )
                try:
                    target.click(force=True, timeout=2_000)
                    page.wait_for_timeout(250)
                    selected = True
                    break
                except PlaywrightTimeoutError:
                    continue
            if selected:
                break
    if not selected:
        raise RuntimeError("보안프로그램 '설치하지 않음'을 선택하지 못했습니다.")
    if not click_text(page, ["확인"], timeout=5_000):
        raise RuntimeError("보안프로그램 선택 화면의 확인 버튼을 누르지 못했습니다.")
    page.wait_for_timeout(3_000)
    return True


def login(page: Page, employee_id: str, password: str) -> None:
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)

    company_inputs = [
        "input[type=search]",
        "input[placeholder*=회사]",
        "input[placeholder*=소속]",
        "input[type=text]",
    ]
    for selector in company_inputs:
        try:
            page.locator(selector).first.fill("삼성화재", timeout=1_500)
            click_text(page, ["검색", "조회"])
            company_name = page.get_by_text(re.compile(r"^\s*삼성화재\s*$"))
            company_name.first.wait_for(state="visible", timeout=8_000)
            company_radios = page.locator("input[type=radio]")
            if company_radios.count():
                company_radios.first.evaluate(
                    """element => {
                        element.checked = true;
                        element.dispatchEvent(new Event('input', { bubbles: true }));
                        element.dispatchEvent(new Event('change', { bubbles: true }));
                    }"""
                )
            elif not click_text(page, ["삼성화재"], timeout=3_000):
                continue
            if not click_text(page, ["선택 완료", "선택완료"], timeout=4_000):
                continue
            for _ in range(40):
                if (
                    has_visible_text(page, re.compile(r"보안프로그램\s*설치여부"))
                    or has_visible_text(page, re.compile(r"사원번호.*아이디.*로그인"))
                ):
                    break
                page.wait_for_timeout(500)
            (ARTIFACT_DIR / "post-company.html").write_text(
                page.content(), encoding="utf-8"
            )
            break
        except PlaywrightTimeoutError:
            continue

    if has_visible_text(page, re.compile(r"보안프로그램\s*설치여부")):
        (ARTIFACT_DIR / "security-page.html").write_text(
            page.content(), encoding="utf-8"
        )
        handle_security_page(page)

    login_heading = page.get_by_text(re.compile(r"사원번호.*아이디.*로그인")).first
    try:
        login_heading.wait_for(state="visible", timeout=8_000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("회사 선택 후 사원번호 로그인 화면으로 이동하지 못했습니다.") from exc

    id_selectors = [
        "#acoEmpno",
        "input[name*=id i]",
        "input[id*=id i]",
        "input[name*=emp i]",
        "input[id*=emp i]",
        "input[placeholder*=사원]",
        "input[placeholder*=아이디]",
        "input[type=text]",
    ]
    password_selectors = [
        "input[type=password]",
        "input[name*=password i]",
        "input[id*=password i]",
        "input[placeholder*=비밀번호]",
    ]
    try:
        fill_first(page, id_selectors, employee_id)
        fill_first(page, password_selectors, password)
    except RuntimeError:
        page.wait_for_timeout(1_000)
        if not handle_security_page(page):
            raise
        login_heading.wait_for(state="visible", timeout=8_000)
        fill_first(page, id_selectors, employee_id)
        fill_first(page, password_selectors, password)
    try:
        page.locator("#loginButn").click(timeout=5_000)
    except PlaywrightTimeoutError:
        if not click_text(page, ["로그인"], timeout=5_000):
            page.keyboard.press("Enter")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(5_000)
    if handle_security_page(page):
        login_heading.wait_for(state="visible", timeout=10_000)
        fill_first(page, id_selectors, employee_id)
        fill_first(page, password_selectors, password)
        page.locator("#loginButn").click(timeout=5_000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(5_000)
    if "login" in page.url.lower() or has_visible_text(
        page, re.compile(r"사원번호.*아이디.*로그인")
    ):
        raise RuntimeError("자동 로그인에 실패했습니다. 보안키패드 또는 추가 인증을 확인하세요.")


def select_hall(page: Page) -> None:
    page.goto(APPLICATION_URL, wait_until="domcontentloaded", timeout=30_000)
    if "login" in page.url.lower():
        raise RuntimeError("예약 화면으로 이동하는 동안 로그인 세션이 종료되었습니다.")
    if page.get_by_text(TARGET_HALL, exact=True).count():
        return
    selectors = ["select", "[role=combobox]", "button", ".select", ".dropdown"]
    for selector in selectors:
        try:
            page.locator(selector).filter(has_text=re.compile("웨딩홀|사옥|선택")).first.click(timeout=2_000)
            if click_text(page, [TARGET_HALL], timeout=3_000):
                return
        except PlaywrightTimeoutError:
            continue
    raise RuntimeError("웨딩홀 선택 영역에서 서초사옥을 찾지 못했습니다.")


def month_text(page: Page) -> str:
    match = page.get_by_text(re.compile(r"\d{4}년\s*\d{1,2}월")).first
    return match.inner_text(timeout=5_000)


def go_to_target_month(page: Page) -> None:
    for _ in range(24):
        current = month_text(page)
        found = re.search(r"(\d{4})년\s*(\d{1,2})월", current)
        if not found:
            raise RuntimeError("달력의 연월을 읽지 못했습니다.")
        year, month = map(int, found.groups())
        if (year, month) == (TARGET_YEAR, TARGET_MONTH):
            return
        if (year, month) > (TARGET_YEAR, TARGET_MONTH):
            raise RuntimeError("달력이 목표 월보다 뒤에 있어 자동 이동하지 않았습니다.")

        month_label = page.get_by_text(re.compile(rf"{year}년\s*{month}월")).first
        parent = month_label.locator("xpath=..")
        next_buttons = parent.locator("button, a, [role=button]")
        clicked = False
        for index in range(next_buttons.count() - 1, -1, -1):
            try:
                next_buttons.nth(index).click(timeout=1_500)
                clicked = True
                break
            except PlaywrightTimeoutError:
                continue
        if not clicked:
            for selector in [
                "[aria-label*=다음]",
                "[title*=다음]",
                ".next",
                "button:has-text('>')",
            ]:
                try:
                    page.locator(selector).first.click(timeout=1_500)
                    clicked = True
                    break
                except PlaywrightTimeoutError:
                    continue
        if not clicked:
            raise RuntimeError("달력의 다음 달 버튼을 찾지 못했습니다.")
        page.wait_for_timeout(400)
    raise RuntimeError("24개월 안에서 목표 월을 찾지 못했습니다.")


def closest_status_container(time_locator: Locator) -> Locator:
    for xpath in ["ancestor::li[1]", "ancestor::label[1]", "ancestor::div[1]", "ancestor::td[1]"]:
        candidate = time_locator.locator(f"xpath={xpath}")
        try:
            text = candidate.inner_text(timeout=1_000)
            if "예약" in text:
                return candidate
        except PlaywrightTimeoutError:
            continue
    return time_locator.locator("xpath=..")


def read_target_status(page: Page) -> tuple[str, str]:
    go_to_target_month(page)
    day = page.get_by_text(str(TARGET_DAY), exact=True)
    visible_days = [day.nth(i) for i in range(day.count()) if day.nth(i).is_visible()]
    if not visible_days:
        raise RuntimeError("달력에서 28일을 찾지 못했습니다.")
    visible_days[0].click()
    page.wait_for_timeout(500)

    time_locator = page.get_by_text(TARGET_TIME, exact=True).first
    time_locator.wait_for(state="visible", timeout=5_000)
    container = closest_status_container(time_locator)
    detail = re.sub(r"\s+", " ", container.inner_text()).strip()
    if "예약가능" in detail:
        return "available", detail
    if "예약마감" in detail or "마감" in detail:
        return "unavailable", detail
    return "unknown", detail


def send_telegram(message: str) -> None:
    token = required_env("TELEGRAM_BOT_TOKEN")
    chat_id = required_env("TELEGRAM_CHAT_ID")
    data = urlencode({"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}).encode()
    request = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"텔레그램 전송 실패: HTTP {response.status}")


def load_previous_state() -> MonitorState:
    try:
        return MonitorState(**json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except (FileNotFoundError, TypeError, ValueError, json.JSONDecodeError):
        return MonitorState(status="unknown", checked_at="")


def save_state(state: MonitorState) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run() -> int:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    previous = load_previous_state()
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        employee_id = required_env("SAMSUNG_WEDDING_EMPLOYEE_ID")
        password = required_env("SAMSUNG_WEDDING_EMPLOYEE_PASSWORD")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul")
            page = context.new_page()
            try:
                login(page, employee_id, password)
                select_hall(page)
                status, detail = read_target_status(page)
                page.screenshot(path=ARTIFACT_DIR / "latest.png", full_page=True)
            except Exception:
                try:
                    page.screenshot(path=ARTIFACT_DIR / "failure.png", full_page=True)
                except Exception as screenshot_error:  # noqa: BLE001
                    print(f"실패 화면 저장도 실패했습니다: {screenshot_error}", file=sys.stderr)
                raise
            finally:
                context.close()
                browser.close()

        current = MonitorState(status=status, checked_at=checked_at, detail=detail)
        save_state(current)
        print(json.dumps(asdict(current), ensure_ascii=False))
        if status == "available" and previous.status != "available":
            send_telegram(
                "[삼성 웨딩 취소표 발견]\n"
                "서초사옥 · 2027-08-28 · 17:00\n"
                f"확인 결과: {detail}\n{APPLICATION_URL}"
            )
        return 0
    except Exception as exc:  # noqa: BLE001 - workflow must persist diagnostics
        error = f"{type(exc).__name__}: {exc}"
        save_state(MonitorState(status="error", checked_at=checked_at, detail=error))
        if previous.status != "error":
            try:
                send_telegram(f"[삼성 웨딩 모니터 오류]\n{error}\nGitHub Actions 로그를 확인해 주세요.")
            except Exception as telegram_error:  # noqa: BLE001
                print(f"텔레그램 오류 알림도 실패했습니다: {telegram_error}", file=sys.stderr)
        print(error, file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()

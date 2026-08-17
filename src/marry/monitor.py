from __future__ import annotations

import json
import io
import os
import re
import sys
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pytesseract
from PIL import Image, ImageEnhance, ImageOps
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

LOGIN_URL = "https://s-wedding.samsungcard.com/login/UWDDWSCO02M1.jsp"
APPLICATION_URL = "https://s-wedding.samsungcard.com/internal/add-apply/UWDDWSWH04M0.jsp"
TARGET_HALL = "서초사옥"
HALL_FINANCE = "삼성금융연수원"
KNOWN_HALLS = [TARGET_HALL, "삼성E&A", HALL_FINANCE]
STATE_PATH = Path("state/availability.json")
ARTIFACT_DIR = Path("artifacts")
KST = ZoneInfo("Asia/Seoul")

# 실패 알림에 붙일 최근 진단 로그. GitHub Actions 로그를 따로 열지 않아도
# 텔레그램 메시지만으로 원인을 좁힐 수 있게 해 준다.
DIAGNOSTIC_LINES: deque[str] = deque(maxlen=25)
TELEGRAM_LIMIT = 4_000


@dataclass(frozen=True)
class Target:
    year: int
    month: int
    day: int
    time: str

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d} {self.time}"


TARGETS: list[Target] = [
    Target(2027, 8, 28, "17:00"),
    Target(2027, 9, 4, "11:00"),
    Target(2027, 9, 4, "13:00"),
    Target(2027, 9, 4, "17:00"),
]


@dataclass(frozen=True)
class HallDayScan:
    """특정 날짜 하루의 모든 시간대를 확인한다 (해당 홀은 시간대가 다를 수 있어 고정하지 않음)."""

    hall: str
    year: int
    month: int
    day: int


@dataclass(frozen=True)
class HallMonthScan:
    """한 달 전체를 훑어 마감이 아닌 날짜의 시간대를 확인한다."""

    hall: str
    year: int
    month: int


HALL_DAY_SCANS: list[HallDayScan] = [
    HallDayScan(HALL_FINANCE, 2027, 8, 28),
]

HALL_MONTH_SCANS: list[HallMonthScan] = [
    HallMonthScan(HALL_FINANCE, 2027, 9),
    HallMonthScan(HALL_FINANCE, 2027, 10),
]


def parse_extra_targets() -> list[Target]:
    """EXTRA_TARGETS 환경변수("YYYY-MM-DD HH:MM,YYYY-MM-DD HH:MM")로 지정된
    타깃을 파싱한다. TARGETS를 건드리지 않고 이번 실행에서만 임시로 확인할 때 쓴다."""
    raw = os.getenv("EXTRA_TARGETS", "").strip()
    if not raw:
        return []
    extras: list[Target] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.match(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}:\d{2})$", chunk)
        if not match:
            raise RuntimeError(f"EXTRA_TARGETS 형식이 올바르지 않습니다: {chunk!r}")
        year, month, day, time_str = match.groups()
        extras.append(Target(int(year), int(month), int(day), time_str))
    return extras


@dataclass
class MonitorState:
    run_status: str
    checked_at: str
    targets: dict[str, dict[str, str]] = field(default_factory=dict)
    error: str = ""


class DiagnosticTee(io.TextIOBase):
    """stdout을 그대로 흘려보내면서 마지막 몇 줄을 따로 모아 둔다."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._partial = ""

    def write(self, text: str) -> int:
        self._stream.write(text)
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            line = line.strip()
            if line:
                DIAGNOSTIC_LINES.append(line[:300])
        return len(text)

    def flush(self) -> None:
        self._stream.flush()


def actions_run_url() -> str:
    repository = os.getenv("GITHUB_REPOSITORY", "")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    if not repository or not run_id:
        return ""
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{repository}/actions/runs/{run_id}"


def cap_message(message: str, limit: int = TELEGRAM_LIMIT) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 3] + "..."


def build_error_message(error: str) -> str:
    sections = [f"[삼성 웨딩 모니터 오류]\n{error[:600]}"]
    recent = [line for line in DIAGNOSTIC_LINES if line]
    if recent:
        body = "\n".join(f"- {line}" for line in recent)
        sections.append(f"최근 진단 로그:\n{body}")
    run_url = actions_run_url()
    if run_url:
        sections.append(run_url)
    else:
        sections.append("GitHub Actions 로그를 확인해 주세요.")
    return cap_message("\n\n".join(sections))


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


def click_text_containing(page: Page, text: str, timeout: int = 2_000) -> bool:
    """click_text의 정확매칭이 실패할 때 쓰는 부분매칭 폴백.
    접근성 라벨이 같은 텍스트에 붙어 있어 정확매칭이 안 되는 경우가 있다."""
    matches = page.get_by_text(re.compile(re.escape(text)))
    for index in range(matches.count()):
        candidate = matches.nth(index)
        try:
            if candidate.is_visible():
                candidate.click(timeout=timeout, force=True)
                return True
        except PlaywrightTimeoutError:
            continue
    return False


def click_hall_option(page: Page, hall_name: str) -> bool:
    # 커스텀 드롭다운 옵션은 data-text 속성에 홀 이름을 그대로 담고 있다(확인됨).
    data_attr_option = page.locator(f'a[data-text="{hall_name}"]')
    if data_attr_option.count():
        try:
            data_attr_option.first.click(timeout=2_000, force=True)
            return True
        except PlaywrightTimeoutError:
            pass
    return click_text(page, [hall_name], timeout=3_000) or click_text_containing(page, hall_name)


def fill_first(page: Page, selectors: list[str], value: str) -> None:
    for frame in page.frames:
        for selector in selectors:
            fields = frame.locator(selector)
            for index in range(fields.count()):
                input_field = fields.nth(index)
                try:
                    if not input_field.is_visible():
                        continue
                    input_field.fill(value, timeout=2_000)
                    if input_field.input_value() == value:
                        return
                except (PlaywrightTimeoutError, AssertionError):
                    pass
    raise RuntimeError("로그인 입력란을 찾거나 입력하지 못했습니다.")


def enter_password_with_keypad(page: Page, password: str) -> None:
    password_fields = page.locator("input[type=password]")
    active_field = None
    for index in range(password_fields.count()):
        field = password_fields.nth(index)
        if field.is_visible():
            field.click(force=True, timeout=3_000)
            active_field = field
            break
    page.wait_for_timeout(500)

    if active_field is None:
        raise RuntimeError("보이는 비밀번호 입력란을 찾지 못했습니다.")
    page.keyboard.type(password, delay=80)
    page.wait_for_timeout(300)
    try:
        if active_field.input_value():
            page.keyboard.press("Enter")
            return
    except PlaywrightTimeoutError:
        pass

    visible_heading = None
    for frame in page.frames:
        keypad_heading = frame.get_by_text("보안키패드", exact=True)
        for index in range(keypad_heading.count()):
            if keypad_heading.nth(index).is_visible():
                visible_heading = keypad_heading.nth(index)
                break
        if visible_heading is not None:
            break
    if visible_heading is None:
        if not password.isalnum():
            raise RuntimeError("보안키패드 특수문자 OCR 입력은 아직 지원하지 않습니다.")
        screenshot = Image.open(io.BytesIO(page.screenshot(full_page=False))).convert("RGB")
        width, height = screenshot.size
        crop_left = int(width * 0.27)
        crop_top = int(height * 0.52)
        crop_right = int(width * 0.74)
        crop_bottom = int(height * 0.96)
        keypad_image = screenshot.crop(
            (crop_left, crop_top, crop_right, crop_bottom)
        )
        scale = 4
        processed = keypad_image.resize(
            (keypad_image.width * scale, keypad_image.height * scale)
        )
        processed = ImageOps.grayscale(processed)
        processed = ImageEnhance.Contrast(processed).enhance(2.0)
        ocr = pytesseract.image_to_data(
            processed,
            config=(
                "--psm 11 "
                "-c tessedit_char_whitelist=0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            ),
            output_type=pytesseract.Output.DICT,
        )
        key_positions: dict[str, tuple[float, float]] = {}
        for index, raw_text in enumerate(ocr["text"]):
            text = raw_text.strip()
            if len(text) != 1 or not text.isalnum():
                continue
            x = crop_left + (ocr["left"][index] + ocr["width"][index] / 2) / scale
            y = crop_top + (ocr["top"][index] + ocr["height"][index] / 2) / scale
            key_positions[text.lower()] = (x, y)

        if len(key_positions) < 20:
            pixels = keypad_image.load()
            image_width, image_height = keypad_image.size
            light = set()
            for pixel_y in range(image_height):
                for pixel_x in range(image_width):
                    red, green, blue = pixels[pixel_x, pixel_y]
                    if min(red, green, blue) >= 165:
                        light.add((pixel_x, pixel_y))

            components: list[tuple[int, int, int, int]] = []
            while light:
                start = light.pop()
                stack = [start]
                min_x = max_x = start[0]
                min_y = max_y = start[1]
                count = 1
                while stack:
                    current_x, current_y = stack.pop()
                    for neighbor in (
                        (current_x - 1, current_y),
                        (current_x + 1, current_y),
                        (current_x, current_y - 1),
                        (current_x, current_y + 1),
                    ):
                        if neighbor not in light:
                            continue
                        light.remove(neighbor)
                        stack.append(neighbor)
                        count += 1
                        min_x = min(min_x, neighbor[0])
                        max_x = max(max_x, neighbor[0])
                        min_y = min(min_y, neighbor[1])
                        max_y = max(max_y, neighbor[1])
                component_width = max_x - min_x + 1
                component_height = max_y - min_y + 1
                if (
                    22 <= component_width <= 75
                    and 18 <= component_height <= 48
                    and count >= 350
                ):
                    components.append((min_x, min_y, max_x + 1, max_y + 1))

            for left, top, right, bottom in components:
                key_image = keypad_image.crop((left, top, right, bottom))
                key_image = key_image.resize(
                    (key_image.width * 6, key_image.height * 6)
                )
                key_image = ImageOps.grayscale(key_image)
                key_image = ImageEnhance.Contrast(key_image).enhance(2.5)
                raw_key = pytesseract.image_to_string(
                    key_image,
                    config=(
                        "--psm 10 "
                        "-c tessedit_char_whitelist=0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    ),
                ).strip()
                recognized = next(
                    (character for character in raw_key if character.isalnum()),
                    "",
                )
                if recognized:
                    key_positions[recognized.lower()] = (
                        crop_left + (left + right) / 2,
                        crop_top + (top + bottom) / 2,
                    )

            rows: list[list[tuple[int, int, int, int]]] = []
            for component in sorted(components, key=lambda item: (item[1] + item[3]) / 2):
                center_y = (component[1] + component[3]) / 2
                for row in rows:
                    row_center = sum((item[1] + item[3]) / 2 for item in row) / len(row)
                    if abs(center_y - row_center) <= 8:
                        row.append(component)
                        break
                else:
                    rows.append([component])
            key_rows = [
                sorted(row, key=lambda item: item[0])
                for row in rows
                if len(row) >= 7
            ]
            key_rows.sort(key=lambda row: sum(item[1] for item in row) / len(row))
            layouts = ["1234567890", "qwertyuiop", "asdfghjkl"]
            for row, layout in zip(key_rows[:3], layouts):
                if len(row) != len(layout):
                    continue
                for component, character in zip(row, layout):
                    left, top, right, bottom = component
                    key_positions[character] = (
                        crop_left + (left + right) / 2,
                        crop_top + (top + bottom) / 2,
                    )
            if len(key_rows) >= 4:
                bottom_row = key_rows[3]
                letter_components = (
                    bottom_row[1:-1] if len(bottom_row) >= 9 else bottom_row
                )
                if len(letter_components) == 7:
                    for component, character in zip(letter_components, "zxcvbnm"):
                        left, top, right, bottom = component
                        key_positions[character] = (
                            crop_left + (left + right) / 2,
                            crop_top + (top + bottom) / 2,
                        )
        print(f"보안키패드 OCR 인식 키: {sorted(key_positions)}")

        for character in password:
            key = character.lower()
            if key not in key_positions:
                raise RuntimeError("보안키패드 OCR에서 필요한 문자를 찾지 못했습니다.")
            if character.isupper():
                page.mouse.click(crop_left + 55, crop_top + 210)
                page.wait_for_timeout(100)
            page.mouse.click(*key_positions[key])
            page.wait_for_timeout(80)
        page.mouse.click(crop_left + 505, crop_top + 220)
        page.wait_for_timeout(300)
        return

    keypad = visible_heading.locator("xpath=..")
    for levels_up in range(1, 6):
        candidate = visible_heading.locator("xpath=" + "/.." * levels_up)
        try:
            if "입력완료" in candidate.inner_text(timeout=1_000):
                keypad = candidate
                break
        except PlaywrightTimeoutError:
            continue

    key_elements = keypad.locator("button, a, [role=button], li, td, span")

    def click_key(character: str) -> None:
        target_character = character
        if character.isupper():
            shift_candidates = keypad.locator(
                "[aria-label*=shift i], [title*=shift i], [class*=shift i]"
            )
            clicked_shift = False
            for shift_index in range(shift_candidates.count()):
                shift = shift_candidates.nth(shift_index)
                if shift.is_visible():
                    shift.click(force=True, timeout=2_000)
                    clicked_shift = True
                    break
            if not clicked_shift:
                raise RuntimeError("보안키패드의 대문자 전환 키를 찾지 못했습니다.")
            target_character = character.lower()

        for key_index in range(key_elements.count()):
            key = key_elements.nth(key_index)
            if not key.is_visible():
                continue
            try:
                compact = re.sub(r"\s+", "", key.inner_text(timeout=500))
            except PlaywrightTimeoutError:
                continue
            if not compact or compact[0] != target_character:
                continue
            suffix = compact[1:]
            if suffix and not all("ㄱ" <= char <= "힣" for char in suffix):
                continue
            key.click(force=True, timeout=2_000)
            return
        raise RuntimeError("보안키패드에서 비밀번호 문자를 찾지 못했습니다.")

    for character in password:
        if character.isalnum():
            click_key(character)
        else:
            if not click_text(page, ["특수"], timeout=3_000):
                raise RuntimeError("보안키패드의 특수문자 전환 키를 찾지 못했습니다.")
            click_key(character)

    if not click_text(page, ["입력완료"], timeout=3_000):
        raise RuntimeError("보안키패드의 입력완료 버튼을 찾지 못했습니다.")


def handle_security_page(page: Page) -> bool:
    radios = page.locator("input[type=radio]")
    is_security_url = "UWDDWSCO02M2.jsp" in page.url
    if not is_security_url and radios.count() != 2 and not has_visible_text(
        page, re.compile(r"보안프로그램\s*설치여부")
    ):
        return False
    selected = False
    for index in range(radios.count()):
        radio = radios.nth(index)
        radio_id = radio.get_attribute("id") or ""
        label = page.locator(f'label[for="{radio_id}"]') if radio_id else None
        label_text = ""
        if label is not None and label.count():
            label_text = re.sub(r"\s+", " ", label.first.inner_text()).strip()
        print(
            "보안선택 라디오: "
            f"index={index}, id={radio_id}, name={radio.get_attribute('name') or ''}, "
            f"value={radio.get_attribute('value') or ''}, label={label_text[:40]}"
        )
    if radios.count() >= 2:
        try:
            radio_id = radios.last.get_attribute("id")
            if radio_id and page.locator(f'label[for="{radio_id}"]').count():
                page.locator(f'label[for="{radio_id}"]').first.click(
                    force=True, timeout=3_000
                )
            else:
                radios.last.evaluate("element => element.click()")
            page.wait_for_timeout(150)
            selected = radios.last.is_checked()
        except Exception:  # noqa: BLE001 - fall back to other selection methods
            pass
    if not selected and radios.count() >= 2:
        try:
            radios.last.evaluate(
                """element => {
                    element.checked = true;
                    element.dispatchEvent(new Event('input', { bubbles: true }));
                    element.dispatchEvent(new Event('change', { bubbles: true }));
                }"""
            )
            page.wait_for_timeout(150)
            selected = radios.last.is_checked()
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
                    selected = radios.count() < 2 or radios.last.is_checked()
                    if selected:
                        break
                except PlaywrightTimeoutError:
                    continue
            if selected:
                break
    if not selected:
        raise RuntimeError("보안프로그램 '설치하지 않음'을 선택하지 못했습니다.")
    print(
        "보안선택 결과: "
        + ", ".join(
            f"{index}={radios.nth(index).is_checked()}"
            for index in range(radios.count())
        )
    )
    cookies_before = {
        cookie["name"]: cookie["value"] for cookie in page.context.cookies()
    }
    local_before = set(page.evaluate("Object.keys(localStorage)"))
    session_before = set(page.evaluate("Object.keys(sessionStorage)"))
    if not click_text(page, ["확인"], timeout=5_000):
        raise RuntimeError("보안프로그램 선택 화면의 확인 버튼을 누르지 못했습니다.")
    page.wait_for_timeout(3_000)
    for _ in range(3):
        if "UWDDWSCO02M2" not in page.url:
            break
        click_text(page, ["확인"], timeout=3_000)
        page.wait_for_timeout(2_000)
    if "UWDDWSCO02M2" in page.url:
        raise RuntimeError("보안프로그램 확인 후에도 보안 선택 화면을 벗어나지 못했습니다.")
    cookies_after = {
        cookie["name"]: cookie["value"] for cookie in page.context.cookies()
    }
    local_after = set(page.evaluate("Object.keys(localStorage)"))
    session_after = set(page.evaluate("Object.keys(sessionStorage)"))
    changed_cookies = sorted(
        name
        for name in cookies_before.keys() | cookies_after.keys()
        if cookies_before.get(name) != cookies_after.get(name)
    )
    print(f"보안확인 후 URL: {page.url}")
    print(f"보안확인 변경 쿠키명: {changed_cookies}")
    print(f"보안확인 신규 localStorage 키: {sorted(local_after - local_before)}")
    print(f"보안확인 신규 sessionStorage 키: {sorted(session_after - session_before)}")
    return True


def wait_for_login_form(page: Page, attempts: int = 3) -> bool:
    for attempt in range(attempts):
        try:
            page.locator("#acoEmpno").wait_for(state="visible", timeout=15_000)
            return True
        except PlaywrightTimeoutError:
            print(f"사원번호 입력란 대기 실패(시도 {attempt + 1}/{attempts}) URL: {page.url}")
            if attempt == attempts - 1 or "UWDDWSCO02M2" not in page.url:
                return False
            handle_security_page(page)
    return False


def login(page: Page, employee_id: str, password: str) -> None:
    def accept_dialog(dialog) -> None:
        print(f"브라우저 알림: {dialog.message}")
        dialog.accept()

    page.on("dialog", accept_dialog)
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
            break
        except PlaywrightTimeoutError:
            continue

    if has_visible_text(page, re.compile(r"보안프로그램\s*설치여부")):
        handle_security_page(page)

    login_heading = page.get_by_text(re.compile(r"사원번호.*아이디.*로그인")).first
    if not wait_for_login_form(page):
        raise RuntimeError("회사 선택 후 사원번호 로그인 화면으로 이동하지 못했습니다.")

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
    print(f"로그인 제출 후 URL: {page.url}")
    print(
        "제출 후 요소: "
        f"login_button={page.locator('#loginButn').count()}, "
        f"radio={page.locator('input[type=radio]').count()}, "
        f"confirm_button={page.get_by_role('button', name='확인').count()}"
    )
    if handle_security_page(page):
        if not wait_for_login_form(page):
            raise RuntimeError("보안확인 후 사원번호 로그인 화면으로 이동하지 못했습니다.")
        print(f"보안확인 후 로그인 재입력 시작: {page.url}")
        try:
            fill_first(page, id_selectors, employee_id)
            print("보안확인 후 사원번호 입력 성공")
            try:
                fill_first(page, password_selectors, password)
            except RuntimeError:
                enter_password_with_keypad(page, password)
            print("보안확인 후 비밀번호 입력 성공")
        except RuntimeError:
            print(f"보안확인 후 재입력 실패 URL: {page.url}")
            raise
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


def select_hall(page: Page, hall_name: str = TARGET_HALL) -> None:
    page.goto(APPLICATION_URL, wait_until="domcontentloaded", timeout=30_000)
    if "login" in page.url.lower():
        raise RuntimeError("예약 화면으로 이동하는 동안 로그인 세션이 종료되었습니다.")
    opened_application = click_text(page, ["신청"], timeout=5_000)
    if not opened_application:
        action_elements = page.locator(
            "button, a, input[type=button], input[type=submit]"
        )
        for index in range(action_elements.count()):
            action = action_elements.nth(index)
            if not action.is_visible():
                continue
            box = action.bounding_box()
            if not box:
                continue
            if (
                100 <= box["y"] <= 320
                and 70 <= box["width"] <= 260
                and 25 <= box["height"] <= 100
            ):
                action.click(force=True, timeout=3_000)
                opened_application = True
                break
    if opened_application:
        try:
            page.wait_for_url(re.compile(r"UWDDWSWH04M1"), timeout=10_000)
        except PlaywrightTimeoutError:
            pass
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(1_000)

    close_calendar_notice(page)
    page.wait_for_timeout(500)

    print(
        "웨딩홀 선택 진입 상태: "
        f"URL={page.url}, 신청버튼열림={opened_application}, 프레임수={len(page.frames)}, "
        f"컨텍스트페이지수={len(page.context.pages)}, 목표홀={hall_name}, "
        f"컨텍스트URL목록={[p.url for p in page.context.pages]}"
    )
    if has_visible_text(page, re.compile(rf"^\s*{re.escape(hall_name)}\s*$")):
        return

    # "서초사옥" 텍스트가 정확히 일치하는 요소는 전부 안 보임(is_visible=False)으로
    # 확인됐다 - 실제로 보이는 박스는 "선택됨" 같은 접근성 라벨이 같은 텍스트에 붙어
    # 있을 가능성이 높다(달력 날짜 버튼에서 이미 겪은 패턴). "선택됨"은 실제로 존재가
    # 확인된 텍스트라 이걸 기준으로 진단 영역을 잡는다.
    try:
        value_box = page.get_by_text(re.compile(r"선택됨")).first
        if value_box.count():
            container = value_box.locator("xpath=../../..")
            html = container.evaluate("el => el.outerHTML")
            print(f"홀 선택 영역 HTML(최대 2500자): {html[:2500]}")
        else:
            print("홀 선택 영역 HTML 진단: '선택됨' 텍스트를 찾지 못함")
    except Exception as diag_error:  # noqa: BLE001 - 진단 실패는 무시하고 계속 진행
        print(f"홀 선택 영역 HTML 진단 실패: {diag_error}")

    # 전략 1: 현재 선택된 홀 이름이 표시된 박스를 직접 클릭해 커스텀 드롭다운을 연다.
    # 정확히 일치하는 텍스트가 아니라 포함(substring)으로 찾는다 - 위 진단대로
    # 실제로 보이는 박스는 이름 뒤에 접근성 라벨이 붙어 있을 수 있다.
    for known_hall in KNOWN_HALLS:
        matches = page.get_by_text(re.compile(re.escape(known_hall)))
        count = matches.count()
        visible_index = None
        for match_index in range(count):
            if matches.nth(match_index).is_visible():
                visible_index = match_index
                break
        print(f"홀 선택 - '{known_hall}' 박스 탐색: count={count}, visible_index={visible_index}")
        if visible_index is None:
            continue
        try:
            matches.nth(visible_index).click(timeout=2_000, force=True)
        except PlaywrightTimeoutError as click_error:
            print(f"홀 선택 - '{known_hall}' 박스 클릭 실패: {click_error}")
            continue
        page.wait_for_timeout(300)
        opened = has_visible_text(page, re.compile(rf"^\s*{re.escape(hall_name)}\s*$"))
        print(f"홀 선택 - '{known_hall}' 박스 클릭 후 '{hall_name}' 표시={opened}")
        if click_hall_option(page, hall_name):
            close_calendar_notice(page)
            page.wait_for_timeout(500)
            # 홀을 바꾸면 그 홀의 달력 데이터를 새로 불러오는 것으로 보여, 표시된
            # 월이 안정될 때까지 기다린 뒤 진행한다.
            wait_for_calendar_stable(page)
            return

    # 전략 2: 웨딩홀/선택 문구를 포함한 일반적인 컨테이너를 클릭해 드롭다운을 연다.
    selectors = ["[role=combobox]", "button", ".select", ".dropdown"]
    for frame in page.frames:
        for selector in selectors:
            try:
                candidate = frame.locator(selector).filter(has_text=re.compile("웨딩홀|사옥|선택")).first
                if not candidate.count():
                    continue
                candidate.click(timeout=2_000, force=True)
                page.wait_for_timeout(300)
                opened = has_visible_text(page, re.compile(rf"^\s*{re.escape(hall_name)}\s*$"))
                print(f"홀 선택 - selector={selector!r} 클릭 후 '{hall_name}' 표시={opened}")
                if click_hall_option(page, hall_name):
                    close_calendar_notice(page)
                    page.wait_for_timeout(500)
                    return
            except PlaywrightTimeoutError:
                continue

    # 전략 3(최후): 네이티브 <select>. 화면에 보이는 커스텀 UI와 값이 분리돼 있어
    # select만 바뀌고 화면은 그대로일 수 있으므로 마지막 수단으로만 시도한다.
    native_selects = page.locator("select")
    print(f"홀 선택 - 네이티브 select 개수: {native_selects.count()}")
    for index in range(native_selects.count()):
        select_el = native_selects.nth(index)
        try:
            select_el.select_option(label=hall_name, timeout=2_000)
        except PlaywrightTimeoutError:
            continue
        except Exception as select_error:  # noqa: BLE001 - label option may not exist on this element
            print(f"홀 선택 - select[{index}] select_option 실패: {select_error}")
            continue
        page.wait_for_timeout(300)
        close_calendar_notice(page)
        page.wait_for_timeout(300)
        if has_visible_text(page, re.compile(rf"^\s*{re.escape(hall_name)}\s*$")):
            return

    frame_previews = []
    for frame in page.frames:
        try:
            body_text = re.sub(r"\s+", " ", frame.locator("body").inner_text(timeout=1_000)).strip()
        except PlaywrightTimeoutError:
            body_text = "<시간초과>"
        frame_previews.append(f"{frame.url}: {body_text[:800]}")
        clickable = page.locator("button, a, [role=button]") if frame == page.main_frame else frame.locator(
            "button, a, [role=button]"
        )
        labels = []
        for index in range(min(clickable.count(), 60)):
            element = clickable.nth(index)
            try:
                if element.is_visible():
                    text = re.sub(r"\s+", " ", element.inner_text(timeout=300)).strip()
                    if text:
                        labels.append(text)
            except PlaywrightTimeoutError:
                continue
        frame_previews.append(f"{frame.url} 클릭가능요소: {labels}")
    print("웨딩홀 미발견 진단 - 프레임별 본문 일부:\n" + "\n".join(frame_previews))
    raise RuntimeError(f"웨딩홀 선택 영역에서 {hall_name}을(를) 찾지 못했습니다.")


def month_text(page: Page) -> str:
    match = page.get_by_text(re.compile(r"\d{4}년\s*\d{1,2}월")).first
    return match.inner_text(timeout=5_000)


def wait_for_calendar_stable(page: Page, attempts: int = 8, interval_ms: int = 400) -> None:
    """홀 전환 직후 달력이 비동기로 계속 월을 바꾸는 것처럼 보이는 경우가 있어,
    연속 두 번 같은 월이 읽힐 때까지 기다린 뒤 진행한다."""
    previous = None
    for _ in range(attempts):
        try:
            current = month_text(page)
        except PlaywrightTimeoutError:
            current = None
        if current is not None and current == previous:
            return
        previous = current
        page.wait_for_timeout(interval_ms)


def close_calendar_notice(page: Page) -> None:
    notice = page.get_by_text("안내", exact=True)
    for index in range(notice.count()):
        heading = notice.nth(index)
        if not heading.is_visible():
            continue
        dialog = heading.locator("xpath=ancestor::*[contains(@class, 'pop') or contains(@class, 'modal')][1]")
        if not dialog.count():
            dialog = heading.locator("xpath=../..")
        close_candidates = dialog.locator(
            "button, a, [role=button], [class*=close i], [title*=닫기], [aria-label*=닫기]"
        )
        for close_index in range(close_candidates.count() - 1, -1, -1):
            target = close_candidates.nth(close_index)
            try:
                if target.is_visible():
                    target.click(force=True, timeout=1_500)
                    page.wait_for_timeout(300)
                    return
            except PlaywrightTimeoutError:
                continue
        box = dialog.bounding_box()
        if box:
            page.mouse.click(box["x"] + box["width"] - 32, box["y"] + 32)
            page.wait_for_timeout(300)
            return


def click_month_nav(page: Page, month_label: Locator, current_text: str, direction: str) -> bool:
    """달력의 이전/다음 달 버튼을 찾아 클릭한다. direction은 'next' 또는 'prev'."""
    parent = month_label.locator("xpath=..")
    nav_buttons = parent.locator("button, a, [role=button]")
    count = nav_buttons.count()
    order = range(count - 1, -1, -1) if direction == "next" else range(count)
    for index in order:
        try:
            nav_buttons.nth(index).click(timeout=1_500)
            return True
        except PlaywrightTimeoutError:
            continue

    keyword = "다음" if direction == "next" else "이전"
    arrow = ">" if direction == "next" else "<"
    css_class = ".next" if direction == "next" else ".prev"
    for selector in [f"[aria-label*={keyword}]", f"[title*={keyword}]", css_class, f"button:has-text('{arrow}')"]:
        try:
            page.locator(selector).first.click(timeout=1_500)
            return True
        except PlaywrightTimeoutError:
            continue

    label_box = month_label.bounding_box()
    if label_box:
        offset = label_box["width"] + 28 if direction == "next" else -28
        page.mouse.click(label_box["x"] + offset, label_box["y"] + label_box["height"] / 2)
        page.wait_for_timeout(400)
        try:
            return month_text(page) != current_text
        except PlaywrightTimeoutError:
            return False
    return False


def go_to_target_month(page: Page, target_year: int, target_month: int) -> None:
    """목표 월로 이동한다. 홀 전환 직후 달력이 비동기로 계속 갱신되며 엉뚱한 달에
    멈추는 경우가 있어(자동 스킵 UX로 추정), 목표를 지나쳤으면 "이전달" 버튼으로
    되돌아온다."""
    close_calendar_notice(page)
    wait_for_calendar_stable(page)
    for _ in range(36):
        current = month_text(page)
        found = re.search(r"(\d{4})년\s*(\d{1,2})월", current)
        if not found:
            raise RuntimeError("달력의 연월을 읽지 못했습니다.")
        year, month = map(int, found.groups())
        if (year, month) == (target_year, target_month):
            return

        direction = "next" if (year, month) < (target_year, target_month) else "prev"
        month_label = page.get_by_text(re.compile(rf"{year}년\s*{month}월")).first
        if not click_month_nav(page, month_label, current, direction):
            label = "다음" if direction == "next" else "이전"
            raise RuntimeError(f"달력의 {label} 달 버튼을 찾지 못했습니다.")
        page.wait_for_timeout(400)
    raise RuntimeError("36개월 안에서 목표 월을 찾지 못했습니다.")


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


def classify_status(detail: str) -> str:
    # 사이트가 "예약 가능"처럼 띄어쓰기를 넣어도 놓치지 않도록 공백을 모두 지우고 비교한다.
    compact = re.sub(r"\s+", "", detail)
    if "예약가능" in compact:
        return "available"
    if "예약마감" in compact or "마감" in compact:
        return "unavailable"
    return "unknown"


def click_table_day(page: Page, day: int) -> str | None:
    """구형 <table class="ui_calendar_table"> 달력(예: 삼성금융연수원)에서 날짜 버튼을 찾는다.
    이 달력은 data-date 속성이 없고, 대신 <td> 안에 버튼(날짜 숫자)과
    <span class="end">마감</span>이 형제로 들어 있다. 마감/비활성 날짜는 굳이 클릭하지
    않아도 상태를 바로 알 수 있다.
    반환값: "opened"(클릭 성공, 시간대 패널을 읽으면 됨), "closed"(마감/비활성으로 확인,
    클릭 불필요), 그 날짜를 아예 못 찾으면 None."""
    table = page.locator("table.ui_calendar_table")
    if not table.count():
        return None
    buttons = table.locator("td button")
    for index in range(buttons.count()):
        button = buttons.nth(index)
        try:
            text = re.sub(r"\s+", "", button.inner_text(timeout=500))
        except PlaywrightTimeoutError:
            continue
        digits = re.sub(r"\D", "", text)
        if digits != str(day):
            continue
        class_attr = button.get_attribute("class") or ""
        cell = button.locator("xpath=..")
        try:
            cell_text = re.sub(r"\s+", "", cell.inner_text(timeout=500))
        except PlaywrightTimeoutError:
            cell_text = ""
        if "disabled" in class_attr or "마감" in cell_text:
            print(f"{day}일: 구형 테이블 달력에서 마감/비활성 확인 (class={class_attr!r}, 셀텍스트={cell_text!r})")
            return "closed"
        try:
            button.click(timeout=2_000)
            page.wait_for_timeout(500)
            return "opened"
        except PlaywrightTimeoutError:
            return "closed"
    return None


def click_day_button(page: Page, year: int, month: int, day: int) -> str:
    """반환값: "opened"(클릭 성공, 시간대 패널을 읽으면 됨) 또는 "closed"(구형 테이블
    달력에서 이미 마감/비활성으로 확인돼 클릭이 불필요함). 날짜를 아예 못 찾으면 RuntimeError."""
    go_to_target_month(page, year, month)
    print(f"목표 월 도달: {month_text(page)}")

    target_date = f"{year:04d}{month:02d}{day:02d}"
    day_button = page.locator(f'button[data-date="{target_date}"]')
    for _ in range(10):
        if day_button.count() and day_button.first.is_visible():
            break
        page.wait_for_timeout(500)

    if day_button.count():
        print(
            f"{year}-{month:02d}-{day:02d} 버튼 상태: class={day_button.first.get_attribute('class')}, "
            f"visible={day_button.first.is_visible()}"
        )
        day_button.first.click()
        page.wait_for_timeout(500)
        return "opened"

    # data-date 속성이 없는 구형 <table> 달력(예: 삼성금융연수원)일 수 있다.
    table_result = click_table_day(page, day)
    if table_result is not None:
        return table_result

    all_day_buttons = page.locator("button[data-date]")
    total = all_day_buttons.count()
    sample = [
        all_day_buttons.nth(i).get_attribute("data-date")
        for i in range(min(total, 10))
    ]
    print(f"날짜 버튼 진단: 전체 data-date 버튼 수={total}, 샘플={sample}")
    raise RuntimeError(f"달력에서 {year}-{month:02d}-{day:02d}를 찾지 못했습니다.")


def read_target_status(page: Page, target: Target) -> tuple[str, str]:
    if click_day_button(page, target.year, target.month, target.day) == "closed":
        return "unavailable", "구형 테이블 달력에서 마감/비활성으로 확인"

    time_locator = page.get_by_text(target.time, exact=True).first
    time_locator.wait_for(state="visible", timeout=5_000)
    container = closest_status_container(time_locator)
    detail = re.sub(r"\s+", " ", container.inner_text()).strip()
    return classify_status(detail), detail


TIME_TEXT_PATTERN = re.compile(r"^\d{1,2}:\d{2}$")


def read_all_times_for_day(page: Page) -> dict[str, str]:
    """현재 열려 있는 날짜 상세 패널에서 보이는 모든 시간대의 예약 상태를 읽는다.
    시간대가 홀마다 다를 수 있어, 특정 시간을 고정하지 않고 화면에 보이는 대로 훑는다."""
    time_locators = page.get_by_text(TIME_TEXT_PATTERN)
    results: dict[str, str] = {}
    for index in range(time_locators.count()):
        loc = time_locators.nth(index)
        try:
            if not loc.is_visible():
                continue
            time_text = loc.inner_text(timeout=500).strip()
        except PlaywrightTimeoutError:
            continue
        if time_text in results:
            continue
        container = closest_status_container(loc)
        try:
            detail = re.sub(r"\s+", " ", container.inner_text(timeout=1_000)).strip()
        except PlaywrightTimeoutError:
            continue
        results[time_text] = classify_status(detail)
    return results


def read_day_times(page: Page, year: int, month: int, day: int) -> dict[str, str]:
    if click_day_button(page, year, month, day) == "closed":
        return {"전체": "unavailable"}
    return read_all_times_for_day(page)


def scan_month_table_labels(page: Page, year: int, month: int) -> dict[int, str] | None:
    """구형 <table class="ui_calendar_table"> 달력(예: 삼성금융연수원)에서 각 날짜의
    마감 여부를 클릭 없이 읽는다. 그런 표가 없으면 None."""
    table = page.locator("table.ui_calendar_table")
    if not table.count():
        return None
    cells = table.locator("td")
    labels: dict[int, str] = {}
    for index in range(cells.count()):
        cell = cells.nth(index)
        button = cell.locator("button")
        if not button.count():
            continue
        try:
            text = re.sub(r"\s+", "", button.first.inner_text(timeout=500))
        except PlaywrightTimeoutError:
            continue
        digits = re.sub(r"\D", "", text)
        if not digits:
            continue
        day = int(digits)
        class_attr = button.first.get_attribute("class") or ""
        try:
            cell_text = re.sub(r"\s+", "", cell.inner_text(timeout=500))
        except PlaywrightTimeoutError:
            cell_text = ""
        labels[day] = cell_text if ("마감" in cell_text or "disabled" not in class_attr) else "마감"
    return labels


def scan_month_day_labels(page: Page, year: int, month: int) -> dict[int, str]:
    """이번 달 달력에서 각 날짜 버튼의 표시 텍스트(마감 배지 포함)를 클릭 없이 읽는다."""
    go_to_target_month(page, year, month)
    prefix = f"{year:04d}{month:02d}"
    day_buttons = page.locator(f'button[data-date^="{prefix}"]')
    labels: dict[int, str] = {}
    for index in range(day_buttons.count()):
        button = day_buttons.nth(index)
        date_attr = button.get_attribute("data-date") or ""
        if len(date_attr) != 8:
            continue
        day = int(date_attr[6:8])
        try:
            text = re.sub(r"\s+", "", button.inner_text(timeout=500))
        except PlaywrightTimeoutError:
            text = ""
        labels[day] = text
    if labels:
        return labels
    # data-date 속성이 없는 구형 <table> 달력(예: 삼성금융연수원)일 수 있다.
    return scan_month_table_labels(page, year, month) or {}


def scan_month_for_openings(page: Page, year: int, month: int) -> dict[str, dict[str, str]]:
    """이번 달 전체를 훑어, 마감 배지가 없는 날짜만 클릭해 실제 시간대 상태를 확인한다.
    달력에 이미 마감으로 표시된 날짜는 클릭하지 않아 실행 시간을 아낀다."""
    labels = scan_month_day_labels(page, year, month)
    results: dict[str, dict[str, str]] = {}
    for day, label in sorted(labels.items()):
        if "마감" in label:
            continue
        target_date = f"{year:04d}{month:02d}{day:02d}"
        day_button = page.locator(f'button[data-date="{target_date}"]')
        if day_button.count():
            try:
                day_button.first.click(timeout=1_000)
            except PlaywrightTimeoutError:
                continue
        elif click_table_day(page, day) != "opened":
            continue
        page.wait_for_timeout(400)
        times = read_all_times_for_day(page)
        if times:
            date_key = f"{year:04d}-{month:02d}-{day:02d}"
            results[date_key] = times
            print(f"{date_key} 마감 아님 - 시간대: {times}")
    return results


def send_telegram(message: str) -> None:
    token = re.sub(r"\s+", "", required_env("TELEGRAM_BOT_TOKEN"))
    chat_id = re.sub(r"\s+", "", required_env("TELEGRAM_CHAT_ID"))
    data = urlencode({"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}).encode()
    request = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"텔레그램 전송 실패: HTTP {response.status}")


def load_previous_state() -> MonitorState:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return MonitorState(
            run_status=data.get("run_status", "unknown"),
            checked_at=data.get("checked_at", ""),
            targets=data.get("targets", {}),
            error=data.get("error", ""),
        )
    except (FileNotFoundError, TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return MonitorState(run_status="unknown", checked_at="")


def save_state(state: MonitorState) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run() -> int:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    sys.stdout = DiagnosticTee(sys.stdout)
    previous = load_previous_state()
    now = datetime.now(KST)
    checked_at = now.isoformat(timespec="seconds")
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
                extra_targets = parse_extra_targets()
                if extra_targets:
                    print(f"임시 확인 타깃: {[t.key for t in extra_targets]}")
                results: dict[str, dict[str, str]] = {}
                for target in sorted(
                    TARGETS + extra_targets,
                    key=lambda item: (item.year, item.month, item.day, item.time),
                ):
                    status, detail = read_target_status(page, target)
                    results[target.key] = {"status": status, "detail": detail}

                # 다른 홀(예: 삼성금융연수원)은 실패해도 서초사옥 결과는 그대로 알림이 가도록
                # 홀 단위로 예외를 격리한다.
                scan_halls = sorted(
                    {scan.hall for scan in HALL_DAY_SCANS} | {scan.hall for scan in HALL_MONTH_SCANS}
                )
                for hall in scan_halls:
                    try:
                        select_hall(page, hall)
                    except Exception as hall_error:  # noqa: BLE001 - isolate per-hall failures
                        print(f"{hall} 선택 실패: {hall_error}", file=sys.stderr)
                        results[hall] = {"status": "unknown", "detail": f"홀 선택 실패: {hall_error}"}
                        continue

                    hall_scans = sorted(
                        (scan for scan in HALL_DAY_SCANS if scan.hall == hall),
                        key=lambda scan: (scan.year, scan.month, scan.day),
                    ) + sorted(
                        (scan for scan in HALL_MONTH_SCANS if scan.hall == hall),
                        key=lambda scan: (scan.year, scan.month),
                    )
                    for scan in hall_scans:
                        if isinstance(scan, HallDayScan):
                            date_key = f"{scan.year:04d}-{scan.month:02d}-{scan.day:02d}"
                            try:
                                times = read_day_times(page, scan.year, scan.month, scan.day)
                            except Exception as scan_error:  # noqa: BLE001 - isolate per-scan failures
                                print(f"{hall} {date_key} 확인 실패: {scan_error}", file=sys.stderr)
                                results[f"{hall} {date_key}"] = {
                                    "status": "unknown",
                                    "detail": f"확인 실패: {scan_error}",
                                }
                                continue
                            if not times:
                                results[f"{hall} {date_key}"] = {
                                    "status": "unknown",
                                    "detail": "시간대를 찾지 못했습니다.",
                                }
                            for time_str, status in sorted(times.items()):
                                results[f"{hall} {date_key} {time_str}"] = {
                                    "status": status,
                                    "detail": status,
                                }
                        else:
                            month_key = f"{scan.year:04d}-{scan.month:02d}"
                            try:
                                openings = scan_month_for_openings(page, scan.year, scan.month)
                            except Exception as scan_error:  # noqa: BLE001 - isolate per-scan failures
                                print(f"{hall} {month_key} 스캔 실패: {scan_error}", file=sys.stderr)
                                results[f"{hall} {month_key}"] = {
                                    "status": "unknown",
                                    "detail": f"스캔 실패: {scan_error}",
                                }
                                continue
                            for date_key, times in sorted(openings.items()):
                                for time_str, status in sorted(times.items()):
                                    results[f"{hall} {date_key} {time_str}"] = {
                                        "status": status,
                                        "detail": status,
                                    }

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

        current = MonitorState(run_status="ok", checked_at=checked_at, targets=results)
        save_state(current)
        print(json.dumps(asdict(current), ensure_ascii=False))
        newly_available = {
            key
            for key, result in results.items()
            if result["status"] == "available"
            and previous.targets.get(key, {}).get("status") != "available"
        }
        status_labels = {"available": "예약가능", "unavailable": "예약마감"}

        def format_line(key: str, result: dict[str, str], display: str | None = None) -> str:
            label = status_labels.get(result["status"], "확인불가")
            flag = " 🎉 신규!" if key in newly_available else ""
            return f"- {display if display is not None else key}: {label}{flag}"

        seocho_target_keys = {target.key for target in TARGETS + extra_targets}
        other_halls = sorted({scan.hall for scan in HALL_DAY_SCANS} | {scan.hall for scan in HALL_MONTH_SCANS})
        sections = []
        seocho_lines = [format_line(k, results[k]) for k in sorted(results) if k in seocho_target_keys]
        if seocho_lines:
            sections.append(f"[{TARGET_HALL}]\n" + "\n".join(seocho_lines))
        for hall in other_halls:
            # 섹션 헤더에 이미 홀 이름이 있으니, 각 줄에서는 중복되는 홀 이름 접두어를 뗀다.
            hall_lines = []
            for k in sorted(results):
                if k == hall:
                    hall_lines.append(format_line(k, results[k]))
                elif k.startswith(f"{hall} "):
                    hall_lines.append(format_line(k, results[k], display=k[len(hall) + 1 :]))
            if hall_lines:
                sections.append(f"[{hall}]\n" + "\n".join(hall_lines))
        lines = "\n\n".join(sections)

        if newly_available:
            header = "[삼성 웨딩 취소표 발견]"
        elif any(result["status"] == "unknown" for result in results.values()):
            # 상태 문구를 못 읽은 경우. 사이트 개편으로 판정이 깨졌을 수 있으니 눈에 띄게 알린다.
            header = "[삼성 웨딩 모니터] 상태 확인불가 ⚠️"
        else:
            header = "[삼성 웨딩 모니터] 체크 완료"
        stamp = now.strftime("%m/%d %H:%M")
        try:
            send_telegram(cap_message(f"{header} ({stamp} KST)\n\n{lines}\n\n{APPLICATION_URL}"))
        except Exception as telegram_error:  # noqa: BLE001 - don't let a notification failure erase a successful check
            print(f"상태 알림 전송 실패: {telegram_error}", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001 - workflow must persist diagnostics
        error = f"{type(exc).__name__}: {exc}"
        save_state(MonitorState(run_status="error", checked_at=checked_at, error=error, targets=previous.targets))
        # 같은 오류가 반복될 때만 조용히 넘어간다. 오류 내용이 달라지면 새 실패이므로 알린다.
        if previous.run_status != "error" or previous.error != error:
            try:
                send_telegram(build_error_message(error))
            except Exception as telegram_error:  # noqa: BLE001
                print(f"텔레그램 오류 알림도 실패했습니다: {telegram_error}", file=sys.stderr)
        print(error, file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()

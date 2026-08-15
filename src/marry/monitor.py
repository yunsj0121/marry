from __future__ import annotations

import json
import io
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytesseract
from PIL import Image, ImageEnhance, ImageOps
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
            fields = frame.locator(selector)
            for index in range(fields.count()):
                field = fields.nth(index)
                try:
                    if not field.is_visible():
                        continue
                    field.fill(value, timeout=2_000)
                    if field.input_value() == value:
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
            f"value={radio.get_attribute('value') or ''}, label={label_text}"
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
    try:
        page.locator("#acoEmpno").wait_for(state="visible", timeout=15_000)
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
    print(f"로그인 제출 후 URL: {page.url}")
    print(
        "제출 후 요소: "
        f"login_button={page.locator('#loginButn').count()}, "
        f"radio={page.locator('input[type=radio]').count()}, "
        f"confirm_button={page.get_by_role('button', name='확인').count()}"
    )
    if handle_security_page(page):
        page.locator("#acoEmpno").wait_for(state="visible", timeout=15_000)
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


def select_hall(page: Page) -> None:
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
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(2_000)
    print(
        "웨딩홀 선택 진입 상태: "
        f"URL={page.url}, 신청버튼열림={opened_application}, 프레임수={len(page.frames)}"
    )
    if has_visible_text(page, re.compile(rf"^\s*{re.escape(TARGET_HALL)}\s*$")):
        return
    selectors = ["select", "[role=combobox]", "button", ".select", ".dropdown"]
    for frame in page.frames:
        for selector in selectors:
            try:
                frame.locator(selector).filter(
                    has_text=re.compile("웨딩홀|사옥|선택")
                ).first.click(timeout=2_000)
                if click_text(page, [TARGET_HALL], timeout=3_000):
                    return
            except PlaywrightTimeoutError:
                continue
    frame_previews = []
    for frame in page.frames:
        try:
            body_text = re.sub(r"\s+", " ", frame.locator("body").inner_text(timeout=1_000)).strip()
        except PlaywrightTimeoutError:
            body_text = "<시간초과>"
        frame_previews.append(f"{frame.url}: {body_text[:200]}")
    print("웨딩홀 미발견 진단 - 프레임별 본문 일부:\n" + "\n".join(frame_previews))
    raise RuntimeError("웨딩홀 선택 영역에서 서초사옥을 찾지 못했습니다.")


def month_text(page: Page) -> str:
    match = page.get_by_text(re.compile(r"\d{4}년\s*\d{1,2}월")).first
    return match.inner_text(timeout=5_000)


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


def go_to_target_month(page: Page) -> None:
    close_calendar_notice(page)
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
            label_box = month_label.bounding_box()
            if label_box:
                page.mouse.click(
                    label_box["x"] + label_box["width"] + 28,
                    label_box["y"] + label_box["height"] / 2,
                )
                page.wait_for_timeout(400)
                try:
                    clicked = month_text(page) != current
                except PlaywrightTimeoutError:
                    clicked = False
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

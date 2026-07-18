from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from playwright.sync_api import Page


@dataclass(frozen=True)
class WeddingApplication:
    base_url: str
    hall_name: str
    wedding_date: date
    applicant_name: str
    applicant_phone: str
    company_name: str = "삼성화재"
    employee_id: str | None = None
    employee_password: str | None = None
    preferred_time: str | None = None
    partner_name: str | None = None
    guest_count: int | None = None
    memo: str | None = None
    headless: bool = False
    mobile: bool = False
    device_name: str = "iPhone 14"
    slow_mo_ms: int = 100
    dry_run: bool = True
    manual_timeout_ms: int = 300_000

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WeddingApplication:
        required_fields = ["hall_name", "wedding_date", "applicant_name", "applicant_phone"]
        missing = [field for field in required_fields if not data.get(field)]
        if missing:
            raise ValueError(f"필수 설정값이 없습니다: {', '.join(missing)}")

        base_url = str(data.get("base_url", "https://s-wedding.samsungcard.com"))
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("base_url은 http 또는 https URL이어야 합니다.")

        wedding_date = date.fromisoformat(str(data["wedding_date"]))
        guest_count = data.get("guest_count")
        if guest_count is not None and int(guest_count) < 1:
            raise ValueError("guest_count는 1 이상이어야 합니다.")

        manual_timeout_ms = int(data.get("manual_timeout_ms", 300_000))
        if manual_timeout_ms < 10_000:
            raise ValueError("manual_timeout_ms는 10000 이상이어야 합니다.")

        slow_mo_ms = int(data.get("slow_mo_ms", 100))
        if slow_mo_ms < 0:
            raise ValueError("slow_mo_ms는 0 이상이어야 합니다.")

        return cls(
            base_url=base_url,
            hall_name=str(data["hall_name"]),
            wedding_date=wedding_date,
            applicant_name=str(data["applicant_name"]),
            applicant_phone=str(data["applicant_phone"]),
            company_name=str(data.get("company_name", "삼성화재")),
            employee_id=data.get("employee_id") or os.getenv("SAMSUNG_WEDDING_EMPLOYEE_ID"),
            employee_password=data.get("employee_password")
            or os.getenv("SAMSUNG_WEDDING_EMPLOYEE_PASSWORD"),
            preferred_time=data.get("preferred_time"),
            partner_name=data.get("partner_name"),
            guest_count=int(guest_count) if guest_count is not None else None,
            memo=data.get("memo"),
            headless=bool(data.get("headless", False)),
            mobile=bool(data.get("mobile", False)),
            device_name=str(data.get("device_name", "iPhone 14")),
            slow_mo_ms=slow_mo_ms,
            dry_run=bool(data.get("dry_run", True)),
            manual_timeout_ms=manual_timeout_ms,
        )


def load_application(path: Path) -> WeddingApplication:
    with path.open(encoding="utf-8") as fp:
        return WeddingApplication.from_dict(json.load(fp))


def click_by_text(page: Page, text: str, *, exact: bool = False, timeout: int = 10_000) -> None:
    pattern = f"^{re.escape(text)}$" if exact else re.escape(text)
    page.get_by_text(re.compile(pattern)).first.click(timeout=timeout)


def click_first_text(page: Page, texts: list[str], *, timeout: int = 10_000) -> bool:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    for text in texts:
        try:
            click_by_text(page, text, timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            continue
    return False


def fill_first_available(page: Page, labels: list[str], value: Any) -> bool:
    if value is None:
        return False

    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    text = str(value)
    for label in labels:
        try:
            page.get_by_label(re.compile(label)).first.fill(text, timeout=1_500)
            return True
        except PlaywrightTimeoutError:
            continue

    for placeholder in labels:
        try:
            page.get_by_placeholder(re.compile(placeholder)).first.fill(text, timeout=1_500)
            return True
        except PlaywrightTimeoutError:
            continue

    return False


def fill_by_selector_or_label(
    page: Page, selectors: list[str], labels: list[str], value: Any
) -> bool:
    if value is None:
        return False

    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    text = str(value)
    for selector in selectors:
        try:
            page.locator(selector).first.fill(text, timeout=1_500)
            return True
        except PlaywrightTimeoutError:
            continue

    return fill_first_available(page, labels, text)


def pause_for_manual_step(page: Page, message: str, timeout_ms: int) -> None:
    print(f"\n[수동 확인 필요] {message}")
    print("브라우저에서 필요한 단계를 완료한 뒤 Enter를 누르세요.")
    page.wait_for_timeout(500)
    input()
    page.wait_for_load_state("networkidle", timeout=timeout_ms)


def complete_employee_login(page: Page, application: WeddingApplication) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    if not application.employee_id or not application.employee_password:
        pause_for_manual_step(
            page,
            "사원번호 로그인 정보가 없어 로그인은 직접 완료하세요. "
            "환경변수 또는 로컬 설정에 employee_id/employee_password를 넣으면 자동 입력됩니다.",
            application.manual_timeout_ms,
        )
        return

    fill_by_selector_or_label(
        page,
        [
            "input[type=search]",
            "input[name*=company i]",
            "input[id*=company i]",
            "input[placeholder*=회사]",
            "input[placeholder*=소속]",
        ],
        ["소속", "회사", "소속회사"],
        application.company_name,
    )
    click_first_text(page, ["검색", "조회"], timeout=3_000)
    click_by_text(page, application.company_name, timeout=application.manual_timeout_ms)

    if click_first_text(page, ["설치하지않음", "설치하지 않음"], timeout=5_000):
        click_first_text(page, ["확인", "OK"], timeout=5_000)

    click_first_text(page, ["사원번호", "사번", "임직원"], timeout=5_000)
    fill_by_selector_or_label(
        page,
        [
            "input[name*=id i]",
            "input[id*=id i]",
            "input[name*=emp i]",
            "input[id*=emp i]",
            "input[placeholder*=사원]",
            "input[placeholder*=아이디]",
        ],
        ["아이디", "사원번호", "사번"],
        application.employee_id,
    )
    fill_by_selector_or_label(
        page,
        [
            "input[type=password]",
            "input[name*=password i]",
            "input[id*=password i]",
            "input[placeholder*=비밀번호]",
        ],
        ["비밀번호", "패스워드"],
        application.employee_password,
    )
    try:
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        click_first_text(page, ["로그인", "확인"], timeout=application.manual_timeout_ms)


def apply_for_wedding_hall(application: WeddingApplication) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=application.headless,
            slow_mo=application.slow_mo_ms,
        )
        context_options = {"locale": "ko-KR"}
        if application.mobile:
            if application.device_name not in playwright.devices:
                available_devices = ", ".join(sorted(playwright.devices))
                raise ValueError(
                    f"지원하지 않는 device_name입니다: {application.device_name}. "
                    f"사용 가능한 값: {available_devices}"
                )
            context_options.update(playwright.devices[application.device_name])

        context = browser.new_context(**context_options)
        page = context.new_page()

        page.goto(application.base_url, wait_until="domcontentloaded")
        complete_employee_login(page, application)
        pause_for_manual_step(
            page,
            "로그인 이후 추가 본인인증, 보안문자, 약관 확인이 나오면 직접 완료하세요.",
            application.manual_timeout_ms,
        )

        for menu_text in ["웨딩홀", "홀", "예약", "신청"]:
            try:
                click_by_text(page, menu_text, timeout=3_000)
                page.wait_for_load_state("networkidle", timeout=application.manual_timeout_ms)
                break
            except PlaywrightTimeoutError:
                continue

        click_by_text(page, application.hall_name, timeout=application.manual_timeout_ms)

        date_texts = [
            application.wedding_date.isoformat(),
            application.wedding_date.strftime("%Y.%m.%d"),
            application.wedding_date.strftime("%Y년 %-m월 %-d일"),
            str(application.wedding_date.day),
        ]
        for date_text in date_texts:
            try:
                click_by_text(page, date_text, exact=date_text.isdigit(), timeout=5_000)
                break
            except PlaywrightTimeoutError:
                continue
        else:
            pause_for_manual_step(
                page,
                f"{application.wedding_date.isoformat()} 날짜를 직접 선택하세요.",
                application.manual_timeout_ms,
            )

        if application.preferred_time:
            try:
                click_by_text(page, application.preferred_time, timeout=5_000)
            except PlaywrightTimeoutError:
                pause_for_manual_step(
                    page,
                    f"{application.preferred_time} 시간대를 직접 선택하세요.",
                    application.manual_timeout_ms,
                )

        fill_first_available(page, ["이름", "신청자", "예약자"], application.applicant_name)
        fill_first_available(page, ["전화", "휴대", "연락처", "휴대폰"], application.applicant_phone)
        fill_first_available(page, ["배우자", "상대", "신랑", "신부"], application.partner_name)
        fill_first_available(page, ["인원", "하객", "예상"], application.guest_count)
        fill_first_available(page, ["메모", "요청", "문의", "비고"], application.memo)

        if application.dry_run:
            pause_for_manual_step(
                page,
                "dry_run=true라 제출하지 않습니다. 입력값을 확인하세요.",
                application.manual_timeout_ms,
            )
        else:
            click_by_text(page, "신청", timeout=10_000)
            pause_for_manual_step(
                page,
                "최종 제출 전 확인창이 있다면 내용을 검토하고 직접 확정하세요.",
                application.manual_timeout_ms,
            )

        context.close()
        browser.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Samsung Card 웨딩홀 신청 자동화 도우미")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/application.example.json"),
        help="신청 정보 JSON 파일 경로",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    application = load_application(args.config)
    apply_for_wedding_hall(application)


if __name__ == "__main__":
    main()

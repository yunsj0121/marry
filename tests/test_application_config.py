from __future__ import annotations

from pathlib import Path

import pytest

from marry.apply import WeddingApplication, load_application


def test_loads_example_config() -> None:
    application = load_application(Path("config/application.example.json"))

    assert application.base_url == "https://s-wedding.samsungcard.com"
    assert application.company_name == "삼성화재"
    assert application.hall_name == "원하는 웨딩홀 이름"
    assert application.dry_run is True
    assert application.mobile is False
    assert application.device_name == "iPhone 14"


def test_uses_environment_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMSUNG_WEDDING_EMPLOYEE_ID", "employee-from-env")
    monkeypatch.setenv("SAMSUNG_WEDDING_EMPLOYEE_PASSWORD", "password-from-env")

    application = WeddingApplication.from_dict(
        {
            "hall_name": "테스트홀",
            "wedding_date": "2026-10-03",
            "applicant_name": "홍길동",
            "applicant_phone": "010-0000-0000",
        }
    )

    assert application.employee_id == "employee-from-env"
    assert application.employee_password == "password-from-env"


def test_config_credentials_override_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMSUNG_WEDDING_EMPLOYEE_ID", "employee-from-env")
    monkeypatch.setenv("SAMSUNG_WEDDING_EMPLOYEE_PASSWORD", "password-from-env")

    application = WeddingApplication.from_dict(
        {
            "hall_name": "테스트홀",
            "wedding_date": "2026-10-03",
            "applicant_name": "홍길동",
            "applicant_phone": "010-0000-0000",
            "employee_id": "employee-from-config",
            "employee_password": "password-from-config",
        }
    )

    assert application.employee_id == "employee-from-config"
    assert application.employee_password == "password-from-config"


def test_rejects_invalid_guest_count() -> None:
    with pytest.raises(ValueError, match="guest_count"):
        WeddingApplication.from_dict(
            {
                "hall_name": "테스트홀",
                "wedding_date": "2026-10-03",
                "applicant_name": "홍길동",
                "applicant_phone": "010-0000-0000",
                "guest_count": 0,
            }
        )


def test_accepts_mobile_device_option() -> None:
    application = WeddingApplication.from_dict(
        {
            "hall_name": "테스트홀",
            "wedding_date": "2026-10-03",
            "applicant_name": "홍길동",
            "applicant_phone": "010-0000-0000",
            "mobile": True,
            "device_name": "Pixel 7",
        }
    )

    assert application.mobile is True
    assert application.device_name == "Pixel 7"

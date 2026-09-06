from unittest.mock import MagicMock

import pytest

import marry.rehearse as rehearse


@pytest.mark.parametrize('checked', [True, False])
def test_applicant_role_must_actually_be_selected(monkeypatch, checked):
    values = {
        'APPLICANT_BIRTHDATE_YYYYMMDD': ('#wedgAplcnsBird', '19900101'),
        'APPLICANT_DEPARTMENT': ('#wedgAplcnsDeptNm', 'test department'),
        'APPLICANT_EMAIL_LOCAL': ('#wedgAplcnsEmadre', 'test'),
        'APPLICANT_PHONE_SUFFIX': ('#wedgAplcnsMpnoeB', '12345678'),
        'GROOM_NAME': ('#wedgAplcRlpplFnm1', 'test groom'),
        'BRIDE_NAME': ('#wedgAplcRlpplFnm2', 'test bride'),
    }
    locators = {}
    for env, (selector, value) in values.items():
        monkeypatch.setenv(env, value)
        loc = MagicMock()
        loc.count.return_value = 1
        loc.first.input_value.return_value = value
        locators[selector] = loc
    monkeypatch.setenv('APPLICANT_ROLE', '신랑')
    locators['label[for="fi_rd_groom"]'] = MagicMock()
    role = MagicMock()
    role.is_checked.return_value = checked
    locators['#fi_rd_groom'] = role
    page = MagicMock()
    page.locator.side_effect = lambda selector: locators[selector]
    results = rehearse.fill_applicant_info(page)
    assert (results['구분'] == '성공') is checked
    assert all(results[field] == '성공' for field in rehearse.REQUIRED_APPLICANT_FIELDS if field != '구분')


def test_missing_applicant_values_are_not_success(monkeypatch):
    for key in (
        'APPLICANT_BIRTHDATE_YYYYMMDD', 'APPLICANT_DEPARTMENT', 'APPLICANT_EMAIL_LOCAL',
        'APPLICANT_PHONE_SUFFIX', 'APPLICANT_ROLE', 'GROOM_NAME', 'BRIDE_NAME',
    ):
        monkeypatch.delenv(key, raising=False)
    results = rehearse.fill_applicant_info(MagicMock())
    assert all(results[field] != '성공' for field in rehearse.REQUIRED_APPLICANT_FIELDS)

# marry

Samsung Card 결혼 홈페이지에서 웨딩홀 신청을 보조하는 Playwright 기반 자동화 도구입니다.

> 이 도구는 로그인, 본인인증, 보안문자, 최종 결제/제출 확인처럼 사용자가 직접 확인해야 하는 단계는 자동으로 우회하지 않습니다. 브라우저가 멈추면 안내 문구에 따라 직접 처리한 뒤 터미널에서 Enter를 누르세요.

## 기능

- `https://s-wedding.samsungcard.com` 접속
- 소속회사 `삼성화재` 검색 및 선택
- 보안프로그램 설치 안내에서 `설치하지않음` 선택 후 확인
- 사원번호 로그인 자동 입력 후 추가 본인인증 수동 완료 대기
- 모바일 기기 에뮬레이션으로 핸드폰 화면 테스트 가능
- 설정 파일의 웨딩홀, 날짜, 시간, 신청자 정보를 기반으로 신청 폼 입력
- `dry_run` 기본값으로 실제 제출 전 확인 가능

## 설치

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m playwright install chromium
```

## 설정

샘플 설정을 복사한 뒤 실제 신청 정보로 수정하세요.

```bash
cp config/application.example.json config/application.local.json
```

주요 설정값:

- `company_name`: 소속회사 이름, 기본 예시는 `삼성화재`
- `employee_id`: 사원번호 로그인 아이디. 커밋하지 않는 `config/application.local.json`에만 입력하거나 `SAMSUNG_WEDDING_EMPLOYEE_ID` 환경변수를 사용하세요.
- `employee_password`: 사원번호 로그인 비밀번호. 커밋하지 않는 `config/application.local.json`에만 입력하거나 `SAMSUNG_WEDDING_EMPLOYEE_PASSWORD` 환경변수를 사용하세요.
- `hall_name`: 신청할 웨딩홀 이름
- `wedding_date`: 원하는 예식일, `YYYY-MM-DD` 형식
- `preferred_time`: 원하는 시간대
- `applicant_name`, `applicant_phone`: 신청자 정보
- `mobile`: `true`이면 Playwright 모바일 기기 에뮬레이션으로 실행
- `device_name`: 모바일 에뮬레이션 기기명. 기본값은 `iPhone 14`
- `dry_run`: `true`이면 실제 신청 버튼을 누르지 않고 입력 확인 단계에서 멈춤
- `manual_timeout_ms`: 수동 인증/선택 대기 시간

## 실행

일반 데스크톱 테스트:

```bash
marry-apply --config config/application.local.json
```

모바일 화면 테스트는 `config/application.local.json`에서 아래처럼 설정한 뒤 실행하세요.

```json
{
  "mobile": true,
  "device_name": "iPhone 14"
}
```

또는 패키지 설치 없이 다음처럼 실행할 수 있습니다.

```bash
PYTHONPATH=src python -m marry.apply --config config/application.local.json
```

## 운영 팁

1. 처음에는 반드시 `dry_run: true`로 실행해 페이지 구조와 입력값을 확인하세요.
2. 실제 비밀번호가 들어간 `config/application.local.json`은 `.gitignore`로 제외되어 있으니 저장소에 커밋하지 마세요.
3. 실제 휴대폰 브라우저가 아니라 Playwright의 모바일 에뮬레이션이므로, 최종 제출 전에는 실제 휴대폰에서도 한 번 더 확인하세요.
4. 사이트의 실제 버튼/라벨 문구가 변경되면 `src/marry/apply.py`의 텍스트 후보를 조정해야 할 수 있습니다.
5. 자동화가 사이트 이용약관이나 예약 정책을 위반하지 않는지 확인한 뒤 사용하세요.
6. 취소표 텔레그램 알림 기능은 다음 단계에서 별도 스크립트로 추가할 예정입니다.

## 예약 신뢰성 및 테스트

`marry-book`은 필수 개인정보 7개 항목(생년월일, 부서명, 이메일, 휴대전화번호,
구분, 신랑 성명, 신부 성명)의 입력 결과가 모두 정상일 때만 제출합니다.

- 제출 버튼은 한 번만 클릭합니다. 클릭 시간 초과 또는 완료 확인 실패는 결과 불명으로 처리하고,
  다음 지망을 신청하지 않은 채 종료 코드 1로 끝납니다. 이 경우 예약 내역을 직접 확인하세요.
- 기본 완료 판정은 `신청이 완료되었습니다.` 또는 `예약이 정상적으로 완료되었습니다.`처럼
  명시적인 완료 문장이 화면에 보이는지 확인합니다. 실제 사이트 완료 화면을 자동 제출로
  검증한 것은 아니므로, 문구가 다르면 성공했더라도 결과 불명으로 종료될 수 있습니다.
- 실제 완료 화면에서 확인한 고유 CSS 선택자가 있다면 `BOOKING_SUCCESS_SELECTOR`로 지정할 수 있습니다.
  GitHub Actions에서는 같은 이름의 저장소 Variable을 사용합니다. 이 요소는 제출 전에는 보이지 않고
  신청 성공 후에만 표시되어야 합니다. 일반 메뉴나 항상 보이는 요소를 지정하지 마세요.
- 신청 완료 후 텔레그램 또는 스크린샷 저장 실패는 추가 신청을 유발하지 않습니다.
- 모든 지망 실패, 입력 확인 실패, 제출 미확인은 종료 코드 1입니다.
  완료 확인 또는 `auto_submit=false`의 입력 준비 완료는 종료 코드 0입니다.

로컬 회귀 테스트(실제 로그인·신청·텔레그램 전송 없음):

```bash
pip install -e ".[test]"
python -m pytest -q
```

`Tests` 워크플로우는 Windows/Linux에서 회귀 테스트만 실행합니다.
`Wedding hall booking`도 예약 스크립트 실행 전에 테스트를 통과해야 합니다.

# 취소표 모니터 설정

이 기능은 GitHub Actions에서 한국 시간 기준 매일 7시~22시 사이 30분마다(정각 7분, 37분) 실행되어 서초사옥의 다음 날짜/시간대 예약 상태를 확인합니다. 23시~다음날 7시 사이에는 실행하지 않습니다. 자동 예약이나 신청은 하지 않습니다.

- 2027-08-28 17:00
- 2027-09-04 11:00
- 2027-09-04 13:00
- 2027-09-04 17:00

날짜/시간대를 추가하거나 빼려면 `src/marry/monitor.py`의 `TARGETS` 목록을 수정하세요.

## GitHub Secrets

저장소의 `Settings → Secrets and variables → Actions → New repository secret`에서 다음 네 값을 등록합니다.

- `SAMSUNG_WEDDING_EMPLOYEE_ID`: 삼성 웨딩 로그인 사원번호
- `SAMSUNG_WEDDING_EMPLOYEE_PASSWORD`: 삼성 웨딩 로그인 비밀번호
- `TELEGRAM_BOT_TOKEN`: BotFather가 발급한 봇 토큰
- `TELEGRAM_CHAT_ID`: 알림을 받을 개인 채팅 ID

비밀번호와 토큰을 파일, 커밋, 이슈 또는 Actions 로그에 입력하지 마세요.

## 최초 실행

1. 텔레그램에서 BotFather로 봇을 만들고 해당 봇에게 `/start`를 보냅니다.
2. 위 네 Secrets를 등록합니다.
3. 저장소의 `Actions → Wedding seat monitor → Run workflow`를 누릅니다.
4. 실패하면 실행 화면의 `Artifacts`에서 진단 스크린샷을 확인합니다.

보안키패드나 추가 인증 때문에 자동 로그인이 차단되면 첫 실행 로그와 스크린샷을 기준으로 로그인 선택자를 조정해야 합니다.

## 알림 정책

- 매 체크마다 전체 날짜/시간대 상태를 텔레그램으로 알림 (예약마감이어도 알림)
- 새로 `예약마감 → 예약가능`으로 바뀐 항목은 "🎉 신규!" 표시와 함께 제목이 `[삼성 웨딩 취소표 발견]`으로 바뀜
- 오류가 새로 발생했을 때 한 번만 오류 알림
- 최근 상태는 `state/availability.json`에 날짜/시간대별로 기록

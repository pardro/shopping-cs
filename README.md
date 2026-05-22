# Shopping CS Assistant

카카오톡 비즈니스센터와 스마트스토어 톡톡 문의를 통합 관리하는 대화형 쇼핑몰 CS 비서입니다.
운영자는 Telegram에서 자연어로 요청하고, 메인 에이전트는 ChatGPT로 요청을 분석해 실행 계획을
만든 뒤 조회/동기화/초안/상담 종료 작업은 바로 수행합니다. 고객에게 특정 메시지를 전송하는 작업만
운영자 승인 후 수행합니다.

## 핵심 동작

1. 운영자가 Telegram으로 자연어 요청을 보냅니다.
2. 메인 에이전트가 요청을 분석해 실행 계획을 만듭니다.
3. 각 계획은 실제 수행 가능한 API 작업 형태로 준비됩니다.
4. 고객에게 메시지를 전송하지 않는 작업은 즉시 실행합니다.
5. 고객에게 특정 메시지를 전송하는 작업은 계획, 이유, 준비된 API 작업, 주의 사항을 운영자에게 보여줍니다.
6. 운영자가 `승인` 또는 `실행`이라고 답하면 전송 작업을 수행합니다.
7. 운영자가 `취소`라고 답하면 대기 중인 전송 계획을 폐기합니다.

고객에게 메시지를 보내는 작업만 항상 승인 후 실행됩니다.

## 서비스 구조

- `app/main.py`: FastAPI 앱 진입점
- `app/api.py`: 헬스 체크, 대화 요청 API, 채널 웹훅 API
- `app/telegram_bot.py`: Telegram Bot API long polling 워커
- `app/agents/main_agent.py`: 자연어 분석, 즉시 실행, 전송 승인 대기, 순차 실행
- `app/agents/sub_agent.py`: 채널별 CS 처리 서브 에이전트
- `app/channels/kakao.py`: 카카오톡 비즈니스센터 API 클라이언트
- `app/channels/naver_talktalk.py`: 스마트스토어 톡톡 API 클라이언트
- `app/llm.py`: OpenAI ChatGPT 호출 계층
- `app/storage/repository.py`: SQLite 티켓, 전송 승인 대기 계획, 대화 컨텍스트 저장소

## 지원 작업

메인 에이전트는 자연어 요청을 아래 작업으로 변환합니다.

- `sync`: 카카오/네이버 채널 API에서 최신 대화 동기화
- `summary`: 채널별 미처리 문의 목록 정리
- `draft_reply`: 고객 답변 초안 생성
- `send_reply`: 고객에게 답변 전송
- `close_ticket`: 상담 종료 처리

예를 들어 “카카오랑 네이버 문의를 동기화하고 미처리 건을 정리해줘”는 `sync -> summary`
순서의 계획으로 만들어집니다.

## 대화 예시

운영자:

```text
각 채널별로 미처리 문의 내역 정리해서 나열해 줘
```

메인 에이전트:

```text
실행 결과
1. 완료 - 채널별 CS 현황
- kakao: open 2, pending 0, closed 0
- naver: open 1, pending 0, closed 0

미처리 문의 목록

kakao
1. #kakao-test-001 홍길동 - 배송은 언제 출발하나요?
2. #kakao-test-002 이고객 - 옵션 변경 가능한가요?

naver
1. #naver-test-001 김고객 - 교환 접수 가능한가요?
```

이후 번호를 그대로 참조할 수 있습니다.

운영자:

```text
카카오 1번 문의건에 대해서 "오늘 출고 예정이며 송장 등록 후 다시 안내드리겠습니다"라는 답변 내용으로 초안 만들어 줘
```

메인 에이전트는 직전에 보여준 번호표를 이용해 `카카오 1번`을 `kakao-test-001`로 해석하고,
답변 초안을 바로 생성합니다.

## 실행 준비

Python 3.11 이상을 사용합니다.

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
cp .env.example .env
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .
Copy-Item .env.example .env
```

가상환경은 운영체제마다 실행 파일 구조가 다르므로 저장소에 커밋하지 말고 각 환경에서 새로 만듭니다.

`.env`에 API 키와 파라미터를 입력합니다. 모든 키는 프로젝트 최상단 `.env`에서만 관리합니다.

최소 실행에 필요한 값:

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
TELEGRAM_BOT_TOKEN=123456:telegram-bot-token
AUDIT_LOG_DIR=./logs/audit
```

채널 API까지 사용하려면 아래 값도 채워야 합니다.

```env
KAKAO_API_BASE_URL=https://...
KAKAO_REST_API_KEY=...
KAKAO_CHANNEL_ID=...

NAVER_TALKTALK_API_BASE_URL=https://api.commerce.naver.com/external
NAVER_CLIENT_ID=...
NAVER_CLIENT_SECRET=...
NAVER_ACCOUNT_ID=
NAVER_TOKEN_TYPE=SELF
NAVER_OAUTH_TOKEN_PATH=/v1/oauth2/token
NAVER_LIST_CONVERSATIONS_PATH=/v1/pay-user/inquiries
NAVER_SEND_MESSAGE_PATH=/v1/pay-merchant/inquiries/{conversation_id}/answer
```

## 서버 실행

터미널 1에서 API 서버를 실행합니다.

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

헬스 체크:

```bash
curl http://localhost:8000/health
```

예상 응답:

```json
{"status":"ok"}
```

터미널 2에서 Telegram 워커를 실행합니다.

```bash
python -m app.telegram_bot
```

## API 설정 점검

`.env`에 입력한 값으로 외부 API가 read-only 요청에 응답하는지 확인할 수 있습니다.
민감한 토큰 값은 출력하지 않습니다.

```bash
python -m app.check_apis
```

이 명령은 OpenAI 모델 조회, Telegram `getMe`, Kakao/Naver 대화 목록 조회를 수행합니다.
메시지 전송이나 상담 종료 같은 변경 작업은 실행하지 않습니다.

## 감사 로그

사용자 요청, 전송 승인 요청, 실행 성공/실패 내역은 `AUDIT_LOG_DIR` 폴더에 날짜별 JSONL 파일로 기록됩니다.
기본 경로는 `./logs/audit/YYYY-MM-DD.jsonl`입니다. 날짜가 바뀌면 새 파일에 기록됩니다.

## Telegram 사용법

Telegram에서 BotFather로 만든 봇에게 자연어로 요청합니다.

도움말:

```text
/help
```

미처리 문의 목록:

```text
각 채널별로 미처리 문의 내역 정리해서 나열해 줘
```

동기화 후 목록 확인:

```text
카카오랑 네이버 문의 최신으로 동기화하고 미처리 건 정리해줘
```

번호로 답변 초안 요청:

```text
카카오 1번 문의건에 대해서 "오늘 출고 예정이며 송장 등록 후 다시 안내드리겠습니다"라는 답변 내용으로 초안 만들어 줘
```

직접 답변 전송 요청:

```text
네이버 1번 고객에게 "안녕하세요. 교환 접수 가능하며 접수 방법을 안내드리겠습니다."라고 보내줘
```

상담 종료 요청:

```text
카카오 1번 상담 종료 처리해줘
```

답변 전송 승인:

```text
승인
```

계획 취소:

```text
취소
```

## HTTP API로 테스트

Telegram 없이도 `/commands` API로 같은 흐름을 테스트할 수 있습니다. 같은 대화를 이어가려면
동일한 `user_key`를 사용합니다.

미처리 문의 목록 조회:

```bash
curl -X POST http://localhost:8000/commands \
  -H "Content-Type: application/json" \
  -d '{"user_key":"local-test","text":"각 채널별로 미처리 문의 내역 정리해서 나열해 줘"}'
```

번호 참조로 초안 생성:

```bash
curl -X POST http://localhost:8000/commands \
  -H "Content-Type: application/json" \
  -d '{"user_key":"local-test","text":"카카오 1번 문의건에 대해서 오늘 출고 예정이라고 초안 만들어 줘"}'
```

답변 전송 요청 후 승인:

```bash
curl -X POST http://localhost:8000/commands \
  -H "Content-Type: application/json" \
  -d '{"user_key":"local-test","text":"카카오 1번 고객에게 오늘 출고 예정이라고 보내줘"}'

curl -X POST http://localhost:8000/commands \
  -H "Content-Type: application/json" \
  -d '{"user_key":"local-test","text":"승인"}'
```

## 채널 웹훅 테스트

실제 카카오/네이버 웹훅을 연결하기 전, 로컬에서 문의 저장 흐름을 테스트할 수 있습니다.

카카오 웹훅 예시:

```bash
curl -X POST http://localhost:8000/webhooks/kakao/cs \
  -H "Content-Type: application/json" \
  -d '{
    "conversation_id": "kakao-test-001",
    "customer_name": "홍길동",
    "message": "배송은 언제 출발하나요?"
  }'
```

네이버 웹훅 예시:

```bash
curl -X POST http://localhost:8000/webhooks/naver/cs \
  -H "Content-Type: application/json" \
  -d '{
    "conversation_id": "naver-test-001",
    "customer_name": "김고객",
    "message": "교환 접수 가능한가요?"
  }'
```

저장 후 “각 채널별로 미처리 문의 내역 정리해서 나열해 줘”라고 요청하면 번호가 붙은
미처리 문의 목록으로 표시됩니다.

## 운영 흐름

1. API 서버를 실행합니다.
2. Telegram 워커를 실행합니다.
3. 운영자가 자연어로 원하는 CS 작업을 요청합니다.
4. 메인 에이전트가 고객 메시지 전송이 아닌 작업은 바로 실행합니다.
5. 고객 메시지 전송 작업은 실행 계획과 준비된 API 작업을 제안합니다.
6. 운영자가 전송 계획을 검토하고 `승인`합니다.
7. 실행 결과를 Telegram으로 받습니다.
8. 후속 요청에서 “카카오 1번”, “네이버 2번”처럼 직전 목록 번호를 참조할 수 있습니다.

## API 키 발급

### OpenAI

1. [OpenAI Platform](https://platform.openai.com/)에 로그인합니다.
2. API keys 화면에서 새 secret key를 생성합니다.
3. `.env`에 입력합니다.

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
OPENAI_TEMPERATURE=0.2
```

참고:

- [OpenAI API Key Help](https://help.openai.com/en/articles/4936850-where-do-i-find-my-openai-api-key)
- [OpenAI Quickstart](https://platform.openai.com/docs/quickstart)

### Telegram

1. Telegram에서 `@BotFather`와 대화합니다.
2. `/newbot` 명령으로 봇을 생성합니다.
3. 발급된 token을 `.env`에 입력합니다.
4. 운영자만 사용하도록 `TELEGRAM_ALLOWED_CHAT_IDS` 설정을 권장합니다.

```env
TELEGRAM_BOT_TOKEN=123456:telegram-bot-token
TELEGRAM_ALLOWED_CHAT_IDS=123456789
TELEGRAM_POLL_TIMEOUT_SECONDS=30
```

참고:

- [Telegram Bots](https://core.telegram.org/bots)
- [BotFather Guide](https://core.telegram.org/bots/features)
- [Telegram Bot API](https://core.telegram.org/bots/api)

### Kakao

1. [Kakao Developers](https://developers.kakao.com/)에 로그인합니다.
2. 내 애플리케이션을 생성하거나 기존 비즈니스 앱을 선택합니다.
3. `[앱] > [플랫폼 키]`에서 REST API key를 확인합니다.
4. 카카오톡 비즈니스센터의 상담/메시지 API 사용 권한을 확인합니다.
5. 승인된 API 문서 기준으로 base URL, 채널 ID, path를 `.env`에 입력합니다.

```env
KAKAO_API_BASE_URL=https://...
KAKAO_REST_API_KEY=...
KAKAO_CHANNEL_ID=...
KAKAO_LIST_CONVERSATIONS_PATH=
KAKAO_SEND_MESSAGE_PATH=
KAKAO_UPDATE_STATUS_PATH=
```

주의:

- Kakao Developers의 REST API key와 카카오톡 비즈니스센터 상담 API 권한은 다를 수 있습니다.
- 공개 Kakao REST API에는 `/v1/channels/{channel_id}/conversations` 형식의 상담 대화 목록 API가 없습니다.
- 카카오 상담/메시지 API는 별도 계약 또는 승인 문서의 엔드포인트가 있을 때만 path를 채우세요.

참고:

- [Kakao App Settings](https://developers.kakao.com/docs/latest/en/app-setting/app)
- [Kakao REST API](https://developers.kakao.com/docs/latest/en/rest-api)

### Naver SmartStore TalkTalk

1. [네이버 커머스API 센터](https://apicenter.commerce.naver.com/)에 접속합니다.
2. 스마트스토어 판매자 계정으로 로그인합니다.
3. 애플리케이션을 생성하고 Client ID, Client Secret을 확인합니다.
4. 문의 API 사용 권한을 확인합니다.
5. 승인된 API 문서 기준으로 base URL과 path를 `.env`에 입력합니다.

```env
NAVER_TALKTALK_API_BASE_URL=https://api.commerce.naver.com/external
NAVER_CLIENT_ID=...
NAVER_CLIENT_SECRET=...
NAVER_TALKTALK_CHANNEL_ID=
NAVER_ACCOUNT_ID=
NAVER_TOKEN_TYPE=SELF
NAVER_OAUTH_TOKEN_PATH=/v1/oauth2/token
NAVER_LIST_CONVERSATIONS_PATH=/v1/pay-user/inquiries
NAVER_SEND_MESSAGE_PATH=/v1/pay-merchant/inquiries/{conversation_id}/answer
NAVER_UPDATE_STATUS_PATH=
```

주의:

- 네이버 커머스API는 OAuth 2.0 Client Credentials 방식으로 인증 토큰을 발급받은 뒤 `Authorization: Bearer` 헤더로 호출합니다.
- 토큰 발급 시 Client Secret을 직접 보내지 않고 `client_id`, millisecond timestamp, `client_secret`으로 bcrypt 기반 전자서명을 생성합니다.
- 스마트스토어 판매자가 직접 만든 애플리케이션은 보통 `NAVER_TOKEN_TYPE=SELF`를 사용합니다.
- 솔루션/대행사가 특정 판매자 계정 권한으로 호출해야 하는 경우에만 `NAVER_TOKEN_TYPE=SELLER`와 판매자 UID인 `NAVER_ACCOUNT_ID`를 사용합니다.
- 현재 구현은 커머스 API의 고객 문의 조회/답변 등록 엔드포인트 기준입니다. 톡톡 실시간 대화 API가 별도 제휴 권한으로 제공되는 경우, 발급받은 문서의 URL과 path로 교체하세요.

참고:

- [Naver Commerce API](https://apicenter.commerce.naver.com/docs/commerce-api/current)

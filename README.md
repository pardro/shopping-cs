# Shopping CS Agents

카카오톡 비즈니스센터와 스마트스토어 톡톡 CS를 채널별 서브 에이전트가 처리하고,
메인 에이전트가 텔레그램 Bot API를 통해 운영자 명령을 받아 조율하는 서비스입니다.

## 구조

- `app/main.py`: FastAPI 앱 진입점
- `app/api.py`: 운영 API 및 채널 웹훅 엔드포인트
- `app/telegram_bot.py`: Telegram Bot API long polling 워커
- `app/agents/main_agent.py`: 텔레그램 명령 해석 및 서브 에이전트 라우팅
- `app/agents/sub_agent.py`: 채널별 CS 처리 로직
- `app/channels/*`: 카카오/네이버 채널 API 클라이언트
- `app/llm.py`: OpenAI ChatGPT 호출 계층
- `app/storage/repository.py`: SQLite 기반 티켓/메시지 저장소

## 실행

```bash
cp .env.example .env
pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

텔레그램 워커는 별도 프로세스로 실행합니다.

```bash
python -m app.telegram_bot
```

## 텔레그램 명령

- `/help`: 명령 목록
- `/summary`: 채널별 대기 티켓 요약
- `/sync`: 모든 채널의 최신 대화 동기화
- `/draft <channel> <conversation_id>`: 답변 초안 생성
- `/send <channel> <conversation_id> <message>`: 고객에게 답변 전송
- `/close <channel> <conversation_id>`: 대화 종료 처리

`channel` 값은 `kakao` 또는 `naver`를 사용합니다.

## API 키 발급 및 환경 변수 설정

모든 키와 실행 파라미터는 프로젝트 최상단의 `.env`에서만 관리합니다. 실제 운영 키는
Git에 커밋하지 말고, 키가 노출되었거나 분실되면 즉시 콘솔에서 재발급한 뒤 `.env` 값을
교체하세요.

### 1. OpenAI ChatGPT API

이 서비스의 메인 에이전트와 채널별 CS 서브 에이전트는 모두 OpenAI API를 사용합니다.

발급 절차:

1. [OpenAI Platform](https://platform.openai.com/)에 로그인합니다.
2. API keys 화면에서 새 secret key를 생성합니다.
3. 생성 직후에만 전체 키를 확인할 수 있으므로 안전한 비밀 저장소에 보관합니다.
4. 과금 수단과 사용 한도, 프로젝트 권한을 확인합니다.
5. `.env`에 아래 값을 입력합니다.

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
OPENAI_TEMPERATURE=0.2
```

참고:

- OpenAI 공식 도움말: [Where do I find my OpenAI API Key?](https://help.openai.com/en/articles/4936850-where-do-i-find-my-openai-api-key)
- OpenAI Quickstart: [Developer quickstart](https://platform.openai.com/docs/quickstart)

### 2. Telegram Bot API

메인 에이전트는 Telegram Bot API로 운영자와 대화합니다.

발급 절차:

1. Telegram 앱에서 공식 봇인 `@BotFather`와 대화를 시작합니다.
2. `/newbot` 명령을 입력합니다.
3. 봇 표시 이름을 입력합니다.
4. 봇 username을 입력합니다. username은 `bot`으로 끝나야 합니다.
5. BotFather가 발급하는 token을 복사합니다.
6. 운영자만 접근하도록 하려면 본인 Telegram chat id를 확인해 허용 목록에 넣습니다.
7. `.env`에 아래 값을 입력합니다.

```env
TELEGRAM_BOT_TOKEN=123456:telegram-bot-token
TELEGRAM_ALLOWED_CHAT_IDS=123456789,987654321
TELEGRAM_POLL_TIMEOUT_SECONDS=30
```

`TELEGRAM_ALLOWED_CHAT_IDS`를 비워두면 모든 사용자의 명령을 받을 수 있으므로 운영 환경에서는
반드시 허용할 chat id만 입력하는 것을 권장합니다.

참고:

- Telegram 공식 소개: [Bots: An introduction for developers](https://core.telegram.org/bots)
- BotFather 공식 가이드: [Telegram Bot Features - BotFather](https://core.telegram.org/bots/features)
- Bot API 형식: [Telegram Bot API](https://core.telegram.org/bots/api)

### 3. 카카오톡 비즈니스센터 / Kakao API

이 프로젝트는 카카오톡 비즈니스센터 CS 연동을 API 클라이언트로 분리해 두었습니다. 실제
대화 조회, 답변 전송, 상태 변경 엔드포인트는 사용 중인 카카오 비즈니스 상품과 승인 권한에
따라 달라질 수 있으므로, 발급 후 `.env`의 URL과 path를 실제 계약/문서 기준으로 맞춰야 합니다.

기본 확인 절차:

1. [Kakao Developers](https://developers.kakao.com/)에 로그인합니다.
2. 내 애플리케이션을 생성하거나 기존 비즈니스 앱을 선택합니다.
3. 앱 관리 화면의 `[앱] > [플랫폼 키]`에서 REST API key를 확인합니다.
4. REST API key 설정에서 허용 IP, Redirect URI, Client secret 필요 여부를 확인합니다.
5. 카카오톡 비즈니스센터에서 실제 상담/채널/비즈니스 메시지 API 사용 권한이 필요한지 확인합니다.
6. 비즈니스 상품이 별도 승인형 API라면 카카오 비즈니스센터 또는 담당 대행사를 통해 API 사용 승인을 진행합니다.
7. 승인된 API 문서의 base URL, 채널 ID, 대화 조회/발송/상태 변경 path를 `.env`에 입력합니다.

```env
KAKAO_API_BASE_URL=https://...
KAKAO_REST_API_KEY=...
KAKAO_CHANNEL_ID=...
KAKAO_LIST_CONVERSATIONS_PATH=/v1/channels/{channel_id}/conversations
KAKAO_SEND_MESSAGE_PATH=/v1/channels/{channel_id}/conversations/{conversation_id}/messages
KAKAO_UPDATE_STATUS_PATH=/v1/channels/{channel_id}/conversations/{conversation_id}/status
```

주의 사항:

- Kakao Developers의 REST API key와 카카오톡 비즈니스센터의 상담/메시지 API 권한은 같은 것이 아닐 수 있습니다.
- 카카오 문서상 REST API key는 `[앱] > [플랫폼 키]`에서 관리하며, 허용 IP 설정으로 서버 IP를 제한할 수 있습니다.
- Client secret이 활성화된 키는 토큰 발급 요청 시 `client_secret` 파라미터가 필요할 수 있습니다.
- 이 저장소의 기본 path는 예시입니다. 실제 카카오 비즈니스 API 문서가 제공하는 엔드포인트로 교체하세요.

참고:

- Kakao Developers 앱 키 문서: [App settings - Platform key / Admin key](https://developers.kakao.com/docs/latest/en/app-setting/app)
- Kakao REST API 시작하기: [Getting started](https://developers.kakao.com/docs/latest/en/rest-api)
- Kakao REST API 레퍼런스: [Reference](https://developers.kakao.com/docs/latest/ko/rest-api/reference)

### 4. 스마트스토어 톡톡 / Naver Commerce API

네이버 스마트스토어 CS 자동화는 네이버 커머스API 센터의 공식 API 권한을 먼저 확인해야 합니다.
공식 커머스API 문서에는 인증, 상품, 주문, 정산, 판매자정보, 문의 API 등이 제공됩니다. 톡톡
대화 자체를 직접 제어하는 API 권한은 계정/상품/제휴 상태에 따라 제공 범위가 달라질 수 있으므로,
운영 전 커머스API 센터에서 사용 가능한 API 목록과 권한을 확인하세요.

발급 절차:

1. [네이버 커머스API 센터](https://apicenter.commerce.naver.com/)에 접속합니다.
2. 스마트스토어 판매자 계정으로 로그인합니다.
3. 커머스API 이용 계정 또는 애플리케이션을 생성합니다.
4. 애플리케이션의 Client ID와 Client Secret을 확인합니다.
5. API 호출 IP 제한 항목이 있으면 이 서버의 고정 outbound IP를 등록합니다.
6. 문의/CS/톡톡 관련 API 사용 권한이 필요한 경우 사용 API 범위를 신청하거나 승인 상태를 확인합니다.
7. `.env`에 아래 값을 입력합니다.

```env
NAVER_TALKTALK_API_BASE_URL=https://api.commerce.naver.com
NAVER_CLIENT_ID=...
NAVER_CLIENT_SECRET=...
NAVER_TALKTALK_CHANNEL_ID=...
NAVER_LIST_CONVERSATIONS_PATH=/v1/channels/{channel_id}/conversations
NAVER_SEND_MESSAGE_PATH=/v1/channels/{channel_id}/conversations/{conversation_id}/messages
NAVER_UPDATE_STATUS_PATH=/v1/channels/{channel_id}/conversations/{conversation_id}/status
```

주의 사항:

- 네이버 커머스API는 OAuth 2.0 기반 인증을 사용합니다. 현재 코드는 `Client ID`와 `Client Secret`을 헤더로 보내는 단순 어댑터 구조이므로, 실제 API가 access token을 요구하면 채널 클라이언트에 토큰 발급 단계를 추가해야 합니다.
- 공식 문서에 있는 “문의” API가 프로젝트의 CS 요구를 충족하는지 먼저 확인하세요.
- 톡톡 실시간 대화 API가 별도 제휴/솔루션 권한으로 제공되는 경우, 발급받은 문서의 base URL과 path로 `.env` 값을 교체해야 합니다.

참고:

- 네이버 커머스API 문서: [커머스API](https://apicenter.commerce.naver.com/docs/commerce-api/current)
- 네이버 커머스API AI 활용 가이드: [AI 활용 가이드](https://apicenter.commerce.naver.com/docs/ai-use-guide)
- 네이버 오픈API 인증 방식 참고: [네이버 오픈API 종류](https://developers.naver.com/docs/common/openapiguide/apilist.md)

### 5. 발급 후 연결 확인 순서

1. `.env`를 채웁니다.
2. 서버를 실행합니다.

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

3. 헬스 체크를 호출합니다.

```bash
curl http://localhost:8000/health
```

4. 텔레그램 워커를 실행합니다.

```bash
python -m app.telegram_bot
```

5. Telegram에서 `/summary`를 보내 메인 에이전트 응답을 확인합니다.
6. 채널 API 설정이 끝난 뒤 `/sync`를 실행해 카카오/네이버 대화 동기화를 확인합니다.

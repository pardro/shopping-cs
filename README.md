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

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
from openai import AsyncOpenAI

from app.channels import KakaoBizCenterClient, NaverTalkTalkClient
from app.config import get_settings


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


async def _run_check(name: str, check: Callable[[], Awaitable[str]]) -> CheckResult:
    try:
        detail = await check()
    except httpx.HTTPStatusError as exc:
        response_text = exc.response.text[:1000]
        detail = (
            f"{exc.response.status_code} {exc.response.reason_phrase} for "
            f"{exc.request.method} {exc.request.url}; response={response_text}"
        )
        return CheckResult(name=name, ok=False, detail=_redact(detail))
    except Exception as exc:
        return CheckResult(name=name, ok=False, detail=_redact(str(exc)))
    return CheckResult(name=name, ok=True, detail=detail)


def _redact(text: str) -> str:
    settings = get_settings()
    secrets = [
        settings.openai_api_key,
        settings.telegram_bot_token,
        settings.kakao_rest_api_key,
        settings.naver_client_id,
        settings.naver_client_secret,
        settings.naver_account_id,
    ]
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


async def check_openai() -> str:
    settings = get_settings()
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is empty")
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    model = await client.models.retrieve(settings.openai_model)
    return f"model reachable: {model.id}"


async def check_telegram() -> str:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is empty")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe"
        )
        response.raise_for_status()
        payload = response.json()
    if not payload.get("ok"):
        raise ValueError(f"Telegram getMe returned ok=false: {payload}")
    bot = payload.get("result", {})
    return f"bot reachable: @{bot.get('username', 'unknown')}"


async def check_kakao() -> str:
    settings = get_settings()
    if not settings.kakao_list_conversations_path:
        return "conversation list API is not configured; public Kakao REST API does not provide this endpoint"
    conversations = await KakaoBizCenterClient(get_settings()).list_conversations()
    return f"list conversations reachable: {len(conversations)} items"


async def check_naver() -> str:
    conversations = await NaverTalkTalkClient(get_settings()).list_conversations()
    return f"list conversations reachable: {len(conversations)} items"


async def main() -> int:
    checks = [
        ("openai", check_openai),
        ("telegram", check_telegram),
        ("kakao", check_kakao),
        ("naver", check_naver),
    ]
    results = await asyncio.gather(*(_run_check(name, check) for name, check in checks))
    for result in results:
        status = "OK" if result.ok else "FAIL"
        print(f"[{status}] {result.name}: {result.detail}")
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

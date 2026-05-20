from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.agents import MainAgent
from app.container import get_main_agent, get_repository
from app.models import ChannelName, CommandResult
from app.storage import CsRepository

router = APIRouter()


class CommandRequest(BaseModel):
    text: str
    user_key: str = "api"


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/commands", response_model=CommandResult)
async def command(
    request: CommandRequest,
    agent: MainAgent = Depends(get_main_agent),
) -> CommandResult:
    return await agent.handle_message(request.text, user_key=request.user_key)


@router.post("/webhooks/kakao/cs")
async def kakao_webhook(
    payload: dict[str, Any],
    repository: CsRepository = Depends(get_repository),
) -> dict[str, Any]:
    try:
        conversation = repository.ingest_webhook(ChannelName.KAKAO, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "conversation_id": conversation.conversation_id}


@router.post("/webhooks/naver/cs")
async def naver_webhook(
    payload: dict[str, Any],
    repository: CsRepository = Depends(get_repository),
) -> dict[str, Any]:
    try:
        conversation = repository.ingest_webhook(ChannelName.NAVER, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "conversation_id": conversation.conversation_id}

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ChannelName(StrEnum):
    KAKAO = "kakao"
    NAVER = "naver"


class TicketStatus(StrEnum):
    OPEN = "open"
    PENDING = "pending"
    CLOSED = "closed"


class ConversationMessage(BaseModel):
    message_id: str
    sender: str
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict[str, Any] = Field(default_factory=dict)


class Conversation(BaseModel):
    channel: ChannelName
    conversation_id: str
    customer_name: str | None = None
    status: TicketStatus = TicketStatus.OPEN
    messages: list[ConversationMessage] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class DraftReply(BaseModel):
    channel: ChannelName
    conversation_id: str
    reply: str
    rationale: str


class ChannelSummary(BaseModel):
    channel: ChannelName
    open_count: int
    pending_count: int
    closed_count: int


class CommandResult(BaseModel):
    ok: bool
    message: str
    data: dict[str, Any] = Field(default_factory=dict)

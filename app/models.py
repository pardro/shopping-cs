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


class ActionType(StrEnum):
    SYNC = "sync"
    SUMMARY = "summary"
    CONVERSATION_DETAIL = "conversation_detail"
    ORDER_LOOKUP = "order_lookup"
    DRAFT_REPLY = "draft_reply"
    SEND_REPLY = "send_reply"
    CLOSE_TICKET = "close_ticket"


class PlannedAction(BaseModel):
    type: ActionType
    channel: ChannelName | None = None
    conversation_id: str | None = None
    message: str | None = None
    reason: str
    prepared_api: str | None = None


class ExecutionPlan(BaseModel):
    user_goal: str
    summary: str
    actions: list[PlannedAction] = Field(default_factory=list)
    needs_more_info: bool = False
    question: str | None = None
    risk_notes: list[str] = Field(default_factory=list)


class ChannelSummary(BaseModel):
    channel: ChannelName
    open_count: int
    pending_count: int
    closed_count: int


class CommandResult(BaseModel):
    ok: bool
    message: str
    data: dict[str, Any] = Field(default_factory=dict)

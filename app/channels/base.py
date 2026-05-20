from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.models import ChannelName, Conversation, ConversationMessage, TicketStatus


class ChannelClient(ABC):
    channel: ChannelName

    @abstractmethod
    async def list_conversations(self, status: TicketStatus | None = None) -> list[Conversation]:
        raise NotImplementedError

    @abstractmethod
    async def send_message(self, conversation_id: str, text: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def update_status(self, conversation_id: str, status: TicketStatus) -> dict[str, Any]:
        raise NotImplementedError


class ApiChannelClient(ChannelClient):
    def __init__(self, base_url: str, timeout_seconds: float = 15.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._base_url:
            raise ValueError(f"{self.channel.value} API base URL is required")
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            response = await client.request(
                method,
                path,
                headers=headers,
                json=json,
                params=params,
            )
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()


def normalize_conversation(
    *,
    channel: ChannelName,
    item: dict[str, Any],
    id_keys: tuple[str, ...] = ("conversation_id", "conversationId", "id"),
    messages_keys: tuple[str, ...] = ("messages", "contents"),
) -> Conversation:
    conversation_id = first_present(item, id_keys)
    messages_payload = first_present(item, messages_keys, default=[])
    status_value = item.get("status") or item.get("state") or TicketStatus.OPEN
    messages = [
        ConversationMessage(
            message_id=str(first_present(message, ("message_id", "messageId", "id"), default="")),
            sender=str(message.get("sender") or message.get("from") or "customer"),
            text=str(message.get("text") or message.get("content") or message.get("message") or ""),
            raw=message,
        )
        for message in messages_payload
        if isinstance(message, dict)
    ]
    return Conversation(
        channel=channel,
        conversation_id=str(conversation_id),
        customer_name=item.get("customer_name") or item.get("customerName") or item.get("nickname"),
        status=normalize_status(status_value),
        messages=messages,
        raw=item,
    )


def first_present(
    payload: dict[str, Any],
    keys: tuple[str, ...],
    default: Any | None = None,
) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return default


def normalize_status(value: Any) -> TicketStatus:
    text = str(value).lower()
    if text in {"closed", "close", "done", "resolved"}:
        return TicketStatus.CLOSED
    if text in {"pending", "hold", "waiting"}:
        return TicketStatus.PENDING
    return TicketStatus.OPEN

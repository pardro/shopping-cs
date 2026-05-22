from typing import Any

from app.channels.base import ApiChannelClient, normalize_conversation
from app.config import Settings
from app.models import ChannelName, Conversation, TicketStatus


class KakaoBizCenterClient(ApiChannelClient):
    channel = ChannelName.KAKAO

    def __init__(self, settings: Settings):
        super().__init__(settings.kakao_api_base_url)
        self._api_key = settings.kakao_rest_api_key
        self._channel_id = settings.kakao_channel_id
        self._list_path = settings.kakao_list_conversations_path
        self._send_path = settings.kakao_send_message_path
        self._status_path = settings.kakao_update_status_path

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ValueError("KAKAO_REST_API_KEY is required")
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _path(self, template: str, conversation_id: str | None = None) -> str:
        return template.format(channel_id=self._channel_id, conversation_id=conversation_id or "")

    async def list_conversations(self, status: TicketStatus | None = None) -> list[Conversation]:
        if not self._list_path:
            return []
        payload = await self._request(
            "GET",
            self._path(self._list_path),
            headers=self._headers(),
            params={"status": status.value} if status else None,
        )
        items = payload.get("conversations") or payload.get("items") or payload.get("data") or []
        return [normalize_conversation(channel=self.channel, item=item) for item in items]

    async def send_message(self, conversation_id: str, text: str) -> dict[str, Any]:
        if not self._send_path:
            raise ValueError("KAKAO_SEND_MESSAGE_PATH is not configured")
        return await self._request(
            "POST",
            self._path(self._send_path, conversation_id),
            headers=self._headers(),
            json={"text": text},
        )

    async def update_status(self, conversation_id: str, status: TicketStatus) -> dict[str, Any]:
        if not self._status_path:
            return {"skipped": True, "reason": "KAKAO_UPDATE_STATUS_PATH is not configured"}
        return await self._request(
            "PATCH",
            self._path(self._status_path, conversation_id),
            headers=self._headers(),
            json={"status": status.value},
        )

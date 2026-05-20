from typing import Any

from app.channels.base import ApiChannelClient, normalize_conversation
from app.config import Settings
from app.models import ChannelName, Conversation, TicketStatus


class NaverTalkTalkClient(ApiChannelClient):
    channel = ChannelName.NAVER

    def __init__(self, settings: Settings):
        super().__init__(settings.naver_talktalk_api_base_url)
        self._client_id = settings.naver_client_id
        self._client_secret = settings.naver_client_secret
        self._channel_id = settings.naver_talktalk_channel_id
        self._list_path = settings.naver_list_conversations_path
        self._send_path = settings.naver_send_message_path
        self._status_path = settings.naver_update_status_path

    def _headers(self) -> dict[str, str]:
        if not self._client_id or not self._client_secret:
            raise ValueError("NAVER_CLIENT_ID and NAVER_CLIENT_SECRET are required")
        return {
            "X-Naver-Client-Id": self._client_id,
            "X-Naver-Client-Secret": self._client_secret,
            "Content-Type": "application/json",
        }

    def _path(self, template: str, conversation_id: str | None = None) -> str:
        return template.format(channel_id=self._channel_id, conversation_id=conversation_id or "")

    async def list_conversations(self, status: TicketStatus | None = None) -> list[Conversation]:
        payload = await self._request(
            "GET",
            self._path(self._list_path),
            headers=self._headers(),
            params={"status": status.value} if status else None,
        )
        items = payload.get("conversations") or payload.get("items") or payload.get("data") or []
        return [normalize_conversation(channel=self.channel, item=item) for item in items]

    async def send_message(self, conversation_id: str, text: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            self._path(self._send_path, conversation_id),
            headers=self._headers(),
            json={"text": text},
        )

    async def update_status(self, conversation_id: str, status: TicketStatus) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            self._path(self._status_path, conversation_id),
            headers=self._headers(),
            json={"status": status.value},
        )

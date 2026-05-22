import base64
import time
from typing import Any

import bcrypt
import httpx

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
        self._account_id = settings.naver_account_id
        self._token_type = settings.naver_token_type
        self._token_path = settings.naver_oauth_token_path
        self._list_path = settings.naver_list_conversations_path
        self._send_path = settings.naver_send_message_path
        self._status_path = settings.naver_update_status_path
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0

    async def _headers(self) -> dict[str, str]:
        token = await self._get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._access_token_expires_at:
            return self._access_token
        if not self._client_id or not self._client_secret:
            raise ValueError("NAVER_CLIENT_ID and NAVER_CLIENT_SECRET are required")
        if self._token_type == "SELLER" and not self._account_id:
            raise ValueError("NAVER_ACCOUNT_ID is required when NAVER_TOKEN_TYPE=SELLER")

        timestamp = str(int(time.time() * 1000))
        signature = self._generate_client_secret_sign(timestamp)
        data = {
            "client_id": self._client_id,
            "timestamp": timestamp,
            "client_secret_sign": signature,
            "grant_type": "client_credentials",
            "type": self._token_type,
        }
        if self._token_type == "SELLER":
            data["account_id"] = self._account_id
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            response = await client.post(
                self._token_path,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            payload = response.json()

        token = payload.get("access_token")
        if not token:
            raise ValueError("Naver Commerce API did not return access_token")
        expires_in = int(payload.get("expires_in") or 10800)
        self._access_token = str(token)
        self._access_token_expires_at = time.time() + max(expires_in - 300, 60)
        return self._access_token

    def _generate_client_secret_sign(self, timestamp: str) -> str:
        password = f"{self._client_id}_{timestamp}".encode("utf-8")
        hashed = bcrypt.hashpw(password, self._client_secret.encode("utf-8"))
        return base64.b64encode(hashed).decode("utf-8")

    def _path(self, template: str, conversation_id: str | None = None) -> str:
        return template.format(channel_id=self._channel_id, conversation_id=conversation_id or "")

    async def list_conversations(self, status: TicketStatus | None = None) -> list[Conversation]:
        payload = await self._request(
            "GET",
            self._path(self._list_path),
            headers=await self._headers(),
            params={"status": status.value} if status else None,
        )
        items = (
            payload.get("contents")
            or payload.get("content")
            or payload.get("inquiries")
            or payload.get("items")
            or payload.get("data")
            or []
        )
        if isinstance(items, dict):
            items = items.get("contents") or items.get("items") or []
        return [
            normalize_conversation(
                channel=self.channel,
                item=item,
                id_keys=("inquiryNo", "inquiry_no", "conversation_id", "conversationId", "id"),
                messages_keys=("messages", "contents", "inquiryContents"),
            )
            for item in items
            if isinstance(item, dict)
        ]

    async def send_message(self, conversation_id: str, text: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            self._path(self._send_path, conversation_id),
            headers=await self._headers(),
            json={"answerContent": text},
        )

    async def update_status(self, conversation_id: str, status: TicketStatus) -> dict[str, Any]:
        if not self._status_path:
            return {"skipped": True, "reason": "Naver Commerce inquiry close API is not configured"}
        return await self._request(
            "PATCH",
            self._path(self._status_path, conversation_id),
            headers=await self._headers(),
            json={"status": status.value},
        )

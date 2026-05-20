from app.channels.base import ChannelClient
from app.llm import ChatGPTClient
from app.models import ChannelName, Conversation, DraftReply, TicketStatus
from app.storage import CsRepository


class ChannelCsAgent:
    def __init__(self, channel_client: ChannelClient, llm: ChatGPTClient, repository: CsRepository):
        self.channel = channel_client.channel
        self._client = channel_client
        self._llm = llm
        self._repository = repository

    async def sync(self) -> int:
        conversations = await self._client.list_conversations(status=None)
        for conversation in conversations:
            self._repository.upsert_conversation(conversation)
        return len(conversations)

    async def draft_reply(self, conversation_id: str, guidance: str | None = None) -> DraftReply:
        conversation = self._load_conversation(conversation_id)
        latest_context = self._format_conversation(conversation)
        guidance_text = guidance or "별도 지시 없음"
        system_prompt = (
            "You are a Korean customer support agent for an online store. "
            "Write concise, polite, action-oriented Korean replies. "
            "Do not promise refunds, exchanges, or shipment changes unless the conversation data "
            "explicitly confirms that the store can do it. Ask for missing order details when needed."
        )
        user_prompt = (
            f"판매 채널: {self.channel.value}\n"
            f"고객명: {conversation.customer_name or '미확인'}\n"
            f"대화 ID: {conversation.conversation_id}\n\n"
            f"대화 내용:\n{latest_context}\n\n"
            f"운영자가 원하는 답변 방향 또는 포함 문구:\n{guidance_text}\n\n"
            "고객에게 보낼 답변 초안을 작성하고, 마지막 줄에 '근거:'로 짧은 판단 근거를 써주세요."
        )
        raw_reply = await self._llm.complete(system_prompt, user_prompt)
        reply, rationale = self._split_rationale(raw_reply)
        return DraftReply(
            channel=self.channel,
            conversation_id=conversation_id,
            reply=reply,
            rationale=rationale,
        )

    async def send_reply(self, conversation_id: str, text: str) -> None:
        await self._client.send_message(conversation_id, text)
        self._repository.record_outbound_message(self.channel, conversation_id, text)

    async def close(self, conversation_id: str) -> None:
        await self._client.update_status(conversation_id, TicketStatus.CLOSED)
        conversation = self._load_conversation(conversation_id)
        conversation.status = TicketStatus.CLOSED
        self._repository.upsert_conversation(conversation)

    def _load_conversation(self, conversation_id: str) -> Conversation:
        conversation = self._repository.get_conversation(self.channel, conversation_id)
        if not conversation:
            raise ValueError(
                f"{self.channel.value} conversation '{conversation_id}' was not found. "
                "먼저 채널 동기화를 요청하고 승인해주세요."
            )
        return conversation

    @staticmethod
    def _format_conversation(conversation: Conversation) -> str:
        if not conversation.messages:
            return "저장된 메시지가 없습니다. 원본 payload만 확인되었습니다."
        return "\n".join(
            f"- {message.sender}: {message.text}"
            for message in conversation.messages[-20:]
            if message.text
        )

    @staticmethod
    def _split_rationale(text: str) -> tuple[str, str]:
        marker = "근거:"
        if marker not in text:
            return text.strip(), ""
        reply, rationale = text.rsplit(marker, 1)
        return reply.strip(), rationale.strip()

from app.llm import ChatGPTClient
from app.models import Conversation, ConversationMessage, DraftReply


class DraftReplyAgent:
    def __init__(self, llm: ChatGPTClient):
        self._llm = llm

    async def draft_reply(
        self,
        conversation: Conversation,
        guidance: str | None = None,
    ) -> DraftReply:
        conversation_history = self.format_conversation_history(conversation)
        guidance_text = guidance or "별도 지시 없음"
        system_prompt = (
            "You are a Korean customer support draft-writing sub-agent for an online store. "
            "Use the full conversation history as the source of truth. "
            "Write concise, polite, action-oriented Korean replies. "
            "Do not promise refunds, exchanges, shipment changes, discounts, or compensation unless "
            "the conversation history explicitly confirms that the store can do it. "
            "If required order details or facts are missing, ask for the missing information. "
            "Do not invent facts outside the conversation history."
        )
        user_prompt = (
            f"판매 채널: {conversation.channel.value}\n"
            f"고객명: {conversation.customer_name or '미확인'}\n"
            f"대화 ID: {conversation.conversation_id}\n\n"
            f"전체 이전 대화 기록:\n{conversation_history}\n\n"
            f"운영자가 원하는 답변 방향 또는 포함 문구:\n{guidance_text}\n\n"
            "위 전체 이전 대화 기록을 기반으로 고객에게 보낼 답변 초안을 작성하고, "
            "마지막 줄에 '근거:'로 어떤 대화 내용을 근거로 판단했는지 짧게 써주세요."
        )
        raw_reply = await self._llm.complete(system_prompt, user_prompt)
        reply, rationale = self._split_rationale(raw_reply)
        return DraftReply(
            channel=conversation.channel,
            conversation_id=conversation.conversation_id,
            reply=reply,
            rationale=rationale,
        )

    @staticmethod
    def format_conversation_history(conversation: Conversation) -> str:
        if not conversation.messages:
            return "저장된 메시지가 없습니다. 원본 payload만 확인되었습니다."
        return "\n".join(
            DraftReplyAgent._format_message(index, message)
            for index, message in enumerate(conversation.messages, start=1)
            if message.text
        )

    @staticmethod
    def _format_message(index: int, message: ConversationMessage) -> str:
        created_at = message.created_at.isoformat()
        return f"{index}. [{created_at}] {message.sender}: {message.text}"

    @staticmethod
    def _split_rationale(text: str) -> tuple[str, str]:
        marker = "근거:"
        if marker not in text:
            return text.strip(), ""
        reply, rationale = text.rsplit(marker, 1)
        return reply.strip(), rationale.strip()

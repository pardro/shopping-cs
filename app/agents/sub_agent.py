from app.agents.draft_reply_agent import DraftReplyAgent
from app.channels.base import ChannelClient
from app.models import ChannelName, Conversation, DraftReply, TicketStatus
from app.storage import CsRepository


class ChannelCsAgent:
    def __init__(
        self,
        channel_client: ChannelClient,
        draft_reply_agent: DraftReplyAgent,
        repository: CsRepository,
    ):
        self.channel = channel_client.channel
        self._client = channel_client
        self._draft_reply_agent = draft_reply_agent
        self._repository = repository

    async def sync(self) -> int:
        conversations = await self._client.list_conversations(status=None)
        for conversation in conversations:
            self._repository.upsert_conversation(conversation)
        return len(conversations)

    async def draft_reply(self, conversation_id: str, guidance: str | None = None) -> DraftReply:
        conversation = self._load_conversation(conversation_id)
        return await self._draft_reply_agent.draft_reply(conversation, guidance=guidance)

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

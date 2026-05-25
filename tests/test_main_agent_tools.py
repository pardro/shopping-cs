import pytest

from app.agents.main_agent import MainAgent
from app.models import ChannelName, Conversation, ConversationMessage
from app.storage import CsRepository


class FakeChannelAgent:
    def __init__(self, *, sync_error: Exception | None = None):
        self._sync_error = sync_error

    async def sync(self) -> int:
        if self._sync_error:
            raise self._sync_error
        return 1


class FakeLlm:
    pass


@pytest.fixture
def repository(tmp_path):
    repo = CsRepository(str(tmp_path / "shopping_cs.sqlite3"))
    repo.initialize()
    return repo


def make_agent(repository, sub_agents=None):
    return MainAgent(
        sub_agents=sub_agents
        or {
            ChannelName.KAKAO: FakeChannelAgent(),
            ChannelName.NAVER: FakeChannelAgent(),
        },
        repository=repository,
        llm=FakeLlm(),
    )


@pytest.mark.asyncio
async def test_sync_tool_reports_channel_failures(repository):
    agent = make_agent(
        repository,
        {
            ChannelName.KAKAO: FakeChannelAgent(sync_error=RuntimeError("kakao down")),
            ChannelName.NAVER: FakeChannelAgent(sync_error=RuntimeError("naver down")),
        },
    )

    result = await agent._execute_cs_tool("sync_conversations", {"channel": "all"}, "user-1")

    assert result["ok"] is False
    assert "kakao 동기화 실패" in result["content"]
    assert "naver 동기화 실패" in result["content"]


@pytest.mark.asyncio
async def test_prepare_send_replies_resolves_numbered_reference_and_korean_channel(repository):
    repository.upsert_conversation(
        Conversation(
            channel=ChannelName.NAVER,
            conversation_id="naver-001",
            customer_name="홍길동",
            messages=[
                ConversationMessage(
                    message_id="m1",
                    sender="customer",
                    text="배송이 지연되고 있나요?",
                )
            ],
        )
    )
    agent = make_agent(repository)
    await agent._execute_cs_tool("summarize_open_tickets", {"channel": "네이버"}, "user-1")

    result = await agent._execute_cs_tool(
        "prepare_send_replies",
        {
            "replies": [
                {
                    "channel": "네이버",
                    "conversation_id": "1번",
                    "message": "확인 후 안내드리겠습니다.",
                }
            ],
            "reason": "고객 답변 전송 요청",
            "risk_notes": "운영자 승인 필요",
        },
        "user-1",
    )

    pending = repository.get_pending_plan("user-1")

    assert result["ok"] is True
    assert pending is not None
    assert pending.actions[0].channel == ChannelName.NAVER
    assert pending.actions[0].conversation_id == "naver-001"
    assert pending.risk_notes == ["운영자 승인 필요"]

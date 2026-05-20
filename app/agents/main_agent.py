from collections.abc import Mapping

from app.agents.sub_agent import ChannelCsAgent
from app.models import ChannelName, CommandResult, TicketStatus
from app.storage import CsRepository


class MainAgent:
    def __init__(self, sub_agents: Mapping[ChannelName, ChannelCsAgent], repository: CsRepository):
        self._sub_agents = sub_agents
        self._repository = repository

    async def handle_command(self, text: str) -> CommandResult:
        command, args = self._parse(text)
        try:
            if command in {"/help", "help"}:
                return CommandResult(ok=True, message=self.help_text())
            if command in {"/summary", "summary"}:
                return CommandResult(ok=True, message=self._summary_text())
            if command in {"/sync", "sync"}:
                return await self._sync_all()
            if command in {"/draft", "draft"}:
                return await self._draft(args)
            if command in {"/send", "send"}:
                return await self._send(args)
            if command in {"/close", "close"}:
                return await self._close(args)
        except Exception as exc:
            return CommandResult(ok=False, message=f"명령 처리 실패: {exc}")
        return CommandResult(ok=False, message=f"알 수 없는 명령입니다.\n\n{self.help_text()}")

    @staticmethod
    def help_text() -> str:
        return "\n".join(
            [
                "사용 가능한 명령:",
                "/summary - 채널별 CS 현황",
                "/sync - 모든 채널 최신 대화 동기화",
                "/draft <kakao|naver> <conversation_id> - 답변 초안 생성",
                "/send <kakao|naver> <conversation_id> <message> - 답변 전송",
                "/close <kakao|naver> <conversation_id> - 대화 종료",
            ]
        )

    async def _sync_all(self) -> CommandResult:
        synced: dict[str, int] = {}
        for channel, agent in self._sub_agents.items():
            synced[channel.value] = await agent.sync()
        lines = [f"{channel}: {count}건 동기화" for channel, count in synced.items()]
        return CommandResult(ok=True, message="\n".join(lines), data={"synced": synced})

    async def _draft(self, args: list[str]) -> CommandResult:
        if len(args) != 2:
            return CommandResult(ok=False, message="형식: /draft <kakao|naver> <conversation_id>")
        channel = self._channel(args[0])
        draft = await self._sub_agents[channel].draft_reply(args[1])
        return CommandResult(
            ok=True,
            message=f"[{draft.channel.value} #{draft.conversation_id}]\n{draft.reply}\n\n근거: {draft.rationale}",
            data=draft.model_dump(),
        )

    async def _send(self, args: list[str]) -> CommandResult:
        if len(args) < 3:
            return CommandResult(ok=False, message="형식: /send <kakao|naver> <conversation_id> <message>")
        channel = self._channel(args[0])
        conversation_id = args[1]
        message = " ".join(args[2:]).strip()
        await self._sub_agents[channel].send_reply(conversation_id, message)
        return CommandResult(ok=True, message=f"{channel.value} #{conversation_id} 답변 전송 완료")

    async def _close(self, args: list[str]) -> CommandResult:
        if len(args) != 2:
            return CommandResult(ok=False, message="형식: /close <kakao|naver> <conversation_id>")
        channel = self._channel(args[0])
        await self._sub_agents[channel].close(args[1])
        return CommandResult(ok=True, message=f"{channel.value} #{args[1]} 종료 완료")

    def _summary_text(self) -> str:
        summaries = self._repository.summarize()
        lines = ["채널별 CS 현황"]
        for summary in summaries:
            lines.append(
                f"- {summary.channel.value}: open {summary.open_count}, "
                f"pending {summary.pending_count}, closed {summary.closed_count}"
            )
        open_tickets = self._repository.list_conversations(status=TicketStatus.OPEN, limit=10)
        if open_tickets:
            lines.append("\n최근 open 티켓")
            lines.extend(
                f"- {ticket.channel.value} #{ticket.conversation_id} "
                f"{ticket.customer_name or ''}".rstrip()
                for ticket in open_tickets
            )
        return "\n".join(lines)

    def _channel(self, value: str) -> ChannelName:
        try:
            return ChannelName(value.lower())
        except ValueError as exc:
            raise ValueError("channel must be 'kakao' or 'naver'") from exc

    @staticmethod
    def _parse(text: str) -> tuple[str, list[str]]:
        parts = text.strip().split()
        if not parts:
            return "/help", []
        return parts[0].lower(), parts[1:]

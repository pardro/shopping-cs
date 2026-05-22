from collections.abc import Mapping

from app.agents.sub_agent import ChannelCsAgent
from app.audit import AuditLogger
from app.llm import ChatGPTClient
from app.models import (
    ActionType,
    ChannelName,
    CommandResult,
    ExecutionPlan,
    PlannedAction,
    TicketStatus,
)
from app.storage import CsRepository


APPROVAL_WORDS = {"승인", "실행", "진행", "좋아", "네", "응", "yes", "y", "ok", "okay"}
CANCEL_WORDS = {"취소", "중단", "아니", "아니요", "no", "n", "cancel"}


class MainAgent:
    def __init__(
        self,
        sub_agents: Mapping[ChannelName, ChannelCsAgent],
        repository: CsRepository,
        llm: ChatGPTClient,
        audit_logger: AuditLogger | None = None,
    ):
        self._sub_agents = sub_agents
        self._repository = repository
        self._llm = llm
        self._audit_logger = audit_logger

    async def handle_command(self, text: str, user_key: str = "default") -> CommandResult:
        return await self.handle_message(text, user_key=user_key)

    async def handle_message(self, text: str, user_key: str = "default") -> CommandResult:
        normalized = text.strip()
        self._audit(
            "user_request",
            {
                "user_key": user_key,
                "text": text,
            },
        )
        if not normalized:
            return CommandResult(ok=True, message=self.help_text())
        if normalized.lower() in {"/help", "help", "도움말"}:
            return CommandResult(ok=True, message=self.help_text())
        if self._is_cancel(normalized):
            self._repository.clear_pending_plan(user_key)
            self._audit("pending_plan_cancelled", {"user_key": user_key})
            return CommandResult(ok=True, message="대기 중인 실행 계획을 취소했습니다.")
        if self._is_approval(normalized):
            return await self._execute_pending_plan(user_key)

        try:
            plan = await self._create_plan(normalized, user_key)
            plan = self._prepare_plan(plan, user_key)
        except Exception as exc:
            return CommandResult(ok=False, message=f"요청 분석 실패: {exc}")

        if plan.needs_more_info or not plan.actions:
            question = plan.question or "수행에 필요한 정보가 부족합니다. 조금 더 구체적으로 말씀해주세요."
            self._audit(
                "plan_needs_more_info",
                {"user_key": user_key, "question": question, "plan": plan.model_dump()},
            )
            return CommandResult(ok=True, message=question, data={"plan": plan.model_dump()})

        immediate_plan, approval_plan = self._split_plan_by_approval_requirement(plan)
        messages: list[str] = []
        results: list[str] = []
        ok = True

        if immediate_plan.actions:
            execution_result = await self._execute_plan(immediate_plan, user_key)
            ok = execution_result.ok
            messages.append(execution_result.message)
            results.extend(execution_result.data.get("results", []))
            if not execution_result.ok:
                return CommandResult(
                    ok=False,
                    message="\n\n".join(messages),
                    data={"plan": plan.model_dump(), "results": results},
                )

        if approval_plan.actions:
            self._repository.save_pending_plan(user_key, approval_plan)
            self._audit(
                "send_reply_approval_requested",
                {"user_key": user_key, "plan": approval_plan.model_dump()},
            )
            messages.append(self._format_plan_for_approval(approval_plan))

        if not messages:
            messages.append("수행할 작업이 없습니다.")

        return CommandResult(
            ok=ok,
            message="\n\n".join(messages),
            data={"plan": plan.model_dump(), "results": results},
        )

    @staticmethod
    def help_text() -> str:
        return "\n".join(
            [
                "쇼핑몰 CS 비서 사용 예시:",
                "카카오랑 네이버 문의를 동기화하고 미처리 건 요약해줘",
                "카카오 kakao-test-001 고객에게 보낼 답변 초안을 만들어줘",
                "네이버 naver-test-001 고객에게 '확인 후 안내드리겠습니다'라고 보내줘",
                "카카오 kakao-test-001 상담 종료 처리해줘",
                "",
                "흐름:",
                "1. 조회, 동기화, 초안, 상담 종료는 바로 실행합니다.",
                "2. 고객에게 특정 메시지를 전송하는 작업만 먼저 승인 요청을 드립니다.",
                "3. 전송 계획이 맞으면 '승인' 또는 '실행'이라고 답장하세요.",
                "4. 취소하려면 '취소'라고 답장하세요.",
            ]
        )

    async def _create_plan(self, user_request: str, user_key: str) -> ExecutionPlan:
        parsed = await self._llm.complete_json(
            system_prompt=self._planner_system_prompt(),
            user_prompt=self._planner_user_prompt(user_request, user_key),
            schema=ExecutionPlan,
        )
        if not isinstance(parsed, ExecutionPlan):
            raise ValueError("Invalid planner response")
        return parsed

    async def _execute_pending_plan(self, user_key: str) -> CommandResult:
        plan = self._repository.get_pending_plan(user_key)
        if not plan:
            return CommandResult(
                ok=False,
                message="승인할 실행 계획이 없습니다. 먼저 원하는 CS 작업을 자연어로 요청해주세요.",
            )

        self._audit("pending_plan_approved", {"user_key": user_key, "plan": plan.model_dump()})
        result = await self._execute_plan(plan, user_key)
        self._repository.clear_pending_plan(user_key)
        return result

    async def _execute_plan(self, plan: ExecutionPlan, user_key: str) -> CommandResult:
        results: list[str] = []
        ok = True
        for index, action in enumerate(plan.actions, start=1):
            try:
                result = await self._execute_action(action, user_key)
                line = f"{index}. 완료 - {result}"
                results.append(line)
                self._audit(
                    "action_executed",
                    {
                        "user_key": user_key,
                        "action": action.model_dump(),
                        "result": result,
                    },
                )
            except Exception as exc:
                ok = False
                line = f"{index}. 실패 - {self._describe_action(action)}: {exc}"
                results.append(line)
                self._audit(
                    "action_failed",
                    {
                        "user_key": user_key,
                        "action": action.model_dump(),
                        "error": str(exc),
                    },
                )
                remaining = len(plan.actions) - index
                if remaining:
                    results.append(f"남은 {remaining}개 작업은 실행하지 않았습니다.")
                break

        prefix = "실행 결과" if ok else "일부 작업이 실패했습니다"
        return CommandResult(
            ok=ok,
            message=f"{prefix}\n" + "\n".join(results),
            data={"plan": plan.model_dump(), "results": results},
        )

    async def _execute_action(self, action: PlannedAction, user_key: str) -> str:
        if action.type == ActionType.SYNC:
            synced: dict[str, int] = {}
            targets = [action.channel] if action.channel else list(self._sub_agents.keys())
            for channel in targets:
                if channel is None:
                    continue
                synced[channel.value] = await self._sub_agents[channel].sync()
            return ", ".join(f"{channel} {count}건 동기화" for channel, count in synced.items())

        if action.type == ActionType.SUMMARY:
            return self._summary_text(user_key=user_key)

        channel = self._require_channel(action)
        conversation_id = self._require_conversation_id(action)
        agent = self._sub_agents[channel]

        if action.type == ActionType.DRAFT_REPLY:
            draft = await agent.draft_reply(conversation_id, guidance=action.message)
            return f"{channel.value} #{conversation_id} 답변 초안:\n{draft.reply}\n근거: {draft.rationale}"

        if action.type == ActionType.SEND_REPLY:
            if not action.message:
                raise ValueError("전송할 메시지가 없습니다")
            await agent.send_reply(conversation_id, action.message)
            return f"{channel.value} #{conversation_id} 답변 전송"

        if action.type == ActionType.CLOSE_TICKET:
            await agent.close(conversation_id)
            return f"{channel.value} #{conversation_id} 상담 종료"

        raise ValueError(f"지원하지 않는 액션입니다: {action.type}")

    def _summary_text(self, user_key: str | None = None) -> str:
        summaries = self._repository.summarize()
        lines = ["채널별 CS 현황"]
        for summary in summaries:
            lines.append(
                f"- {summary.channel.value}: open {summary.open_count}, "
                f"pending {summary.pending_count}, closed {summary.closed_count}"
            )
        open_tickets = self._repository.list_conversations(status=TicketStatus.OPEN, limit=50)
        mapping: dict[str, dict[str, str]] = {channel.value: {} for channel in ChannelName}
        if open_tickets:
            lines.append("\n미처리 문의 목록")
            for channel in ChannelName:
                channel_tickets = [ticket for ticket in open_tickets if ticket.channel == channel]
                if not channel_tickets:
                    continue
                lines.append(f"\n{channel.value}")
                for index, ticket in enumerate(channel_tickets, start=1):
                    mapping[channel.value][str(index)] = ticket.conversation_id
                    lines.append(f"{index}. {self._ticket_line(ticket)}")
        if user_key:
            self._repository.save_user_context(
                user_key,
                {
                    "last_open_ticket_mapping": mapping,
                    "last_open_ticket_text": "\n".join(lines),
                },
            )
        return "\n".join(lines)

    def _format_plan_for_approval(self, plan: ExecutionPlan) -> str:
        lines = [
            "요청을 분석했습니다.",
            f"목표: {plan.user_goal}",
            f"요약: {plan.summary}",
            "",
            "수행 계획:",
        ]
        for index, action in enumerate(plan.actions, start=1):
            lines.append(f"{index}. {self._describe_action(action)}")
            lines.append(f"   이유: {action.reason}")
            lines.append(f"   준비된 API 작업: {action.prepared_api or self._prepared_api_text(action)}")
        if plan.risk_notes:
            lines.append("")
            lines.append("주의 사항:")
            lines.extend(f"- {note}" for note in plan.risk_notes)
        lines.append("")
        lines.append("고객에게 위 메시지를 전송하려면 '승인' 또는 '실행'이라고 답장하세요. 취소하려면 '취소'라고 답장하세요.")
        return "\n".join(lines)

    @staticmethod
    def _describe_action(action: PlannedAction) -> str:
        channel = action.channel.value if action.channel else "전체 채널"
        target = f" #{action.conversation_id}" if action.conversation_id else ""
        if action.type == ActionType.SYNC:
            return f"{channel} 대화 동기화"
        if action.type == ActionType.SUMMARY:
            return "CS 현황 요약"
        if action.type == ActionType.DRAFT_REPLY:
            return f"{channel}{target} 답변 초안 생성"
        if action.type == ActionType.SEND_REPLY:
            return f"{channel}{target} 고객 답변 전송: {action.message}"
        if action.type == ActionType.CLOSE_TICKET:
            return f"{channel}{target} 상담 종료"
        return str(action.type)

    def _planner_user_prompt(self, user_request: str, user_key: str) -> str:
        return "\n".join(
            [
                f"사용자 요청: {user_request}",
                "",
                "현재 CS 상태:",
                self._summary_text(),
                "",
                "직전에 사용자에게 보여준 문의 번호표:",
                self._last_ticket_mapping_text(user_key),
                "",
                "최근 open 티켓:",
                self._recent_open_ticket_text(),
            ]
        )

    @staticmethod
    def _planner_system_prompt() -> str:
        return (
            "You are a Korean shopping mall CS assistant planner. "
            "Analyze the user's natural language request and create a safe execution plan. "
            "Do not execute anything. Return only structured data matching the schema. "
            "Available actions are: sync, summary, draft_reply, send_reply, close_ticket. "
            "Use channel values only when known: kakao or naver. "
            "If the user asks to list unresolved/open inquiries by channel, use summary. "
            "If the user asks to sync and then list or summarize, create sync first and summary second. "
            "If the user says '초안', use draft_reply, not send_reply. "
            "If the user says '보내줘' or clearly asks to send to the customer, use send_reply. "
            "If the user references a numbered inquiry such as '카카오 1번', resolve it using the provided ticket mapping. "
            "For send_reply and close_ticket, channel and conversation_id are required. "
            "For draft_reply, put any requested answer direction or quoted content in message. "
            "For send_reply, message is required and must be exactly what should be sent to the customer. "
            "If required information is missing, set needs_more_info=true and ask a concise Korean question. "
            "For risky customer-facing actions, add a risk note. "
            "Only send_reply actions require later approval. Other actions will be executed immediately. "
            "Never mark actions as already done in the plan."
        )

    def _prepare_plan(self, plan: ExecutionPlan, user_key: str) -> ExecutionPlan:
        for action in plan.actions:
            action.conversation_id = self._resolve_numbered_reference(action, user_key)
            action.prepared_api = self._prepared_api_text(action)
        return plan

    @staticmethod
    def _split_plan_by_approval_requirement(
        plan: ExecutionPlan,
    ) -> tuple[ExecutionPlan, ExecutionPlan]:
        immediate_actions = [
            action for action in plan.actions if action.type != ActionType.SEND_REPLY
        ]
        approval_actions = [
            action for action in plan.actions if action.type == ActionType.SEND_REPLY
        ]
        immediate_plan = plan.model_copy(update={"actions": immediate_actions})
        approval_plan = plan.model_copy(update={"actions": approval_actions})
        return immediate_plan, approval_plan

    def _resolve_numbered_reference(self, action: PlannedAction, user_key: str) -> str | None:
        conversation_id = action.conversation_id
        if not action.channel or not conversation_id:
            return conversation_id
        number = conversation_id.strip().replace("번", "")
        if not number.isdigit():
            return conversation_id
        context = self._repository.get_user_context(user_key)
        mapping = context.get("last_open_ticket_mapping", {})
        channel_mapping = mapping.get(action.channel.value, {})
        return channel_mapping.get(number, conversation_id)

    @staticmethod
    def _prepared_api_text(action: PlannedAction) -> str:
        channel = action.channel.value if action.channel else "kakao/naver"
        conversation_id = action.conversation_id or "{conversation_id}"
        if action.type == ActionType.SYNC:
            return f"GET {channel} 대화 목록 API"
        if action.type == ActionType.SUMMARY:
            return "로컬 DB 미처리 문의 조회"
        if action.type == ActionType.DRAFT_REPLY:
            return f"OpenAI ChatGPT 답변 초안 생성, 대상 {channel} #{conversation_id}"
        if action.type == ActionType.SEND_REPLY:
            return f"POST {channel} 메시지 전송 API, 대상 #{conversation_id}"
        if action.type == ActionType.CLOSE_TICKET:
            return f"PATCH {channel} 상담 상태 변경 API, 대상 #{conversation_id}"
        return "지원하지 않는 API 작업"

    def _last_ticket_mapping_text(self, user_key: str) -> str:
        context = self._repository.get_user_context(user_key)
        mapping = context.get("last_open_ticket_mapping", {})
        if not mapping:
            return "저장된 번호표 없음"
        lines: list[str] = []
        for channel, items in mapping.items():
            if not items:
                continue
            pairs = ", ".join(f"{number}번 -> {conversation_id}" for number, conversation_id in items.items())
            lines.append(f"{channel}: {pairs}")
        return "\n".join(lines) if lines else "저장된 번호표 없음"

    def _recent_open_ticket_text(self) -> str:
        tickets = self._repository.list_conversations(status=TicketStatus.OPEN, limit=10)
        if not tickets:
            return "open 티켓 없음"
        return "\n".join(
            f"- {ticket.channel.value} #{ticket.conversation_id} {ticket.customer_name or ''}".rstrip()
            for ticket in tickets
        )

    @staticmethod
    def _ticket_line(ticket) -> str:
        latest_message = ""
        if ticket.messages:
            latest_message = ticket.messages[-1].text[:80]
        parts = [f"#{ticket.conversation_id}"]
        if ticket.customer_name:
            parts.append(ticket.customer_name)
        if latest_message:
            parts.append(f"- {latest_message}")
        return " ".join(parts)

    @staticmethod
    def _require_channel(action: PlannedAction) -> ChannelName:
        if not action.channel:
            raise ValueError("채널 정보가 필요합니다")
        return action.channel

    @staticmethod
    def _require_conversation_id(action: PlannedAction) -> str:
        if not action.conversation_id:
            raise ValueError("대화 ID가 필요합니다")
        return action.conversation_id

    @staticmethod
    def _is_approval(text: str) -> bool:
        return text.strip().lower() in APPROVAL_WORDS

    @staticmethod
    def _is_cancel(text: str) -> bool:
        return text.strip().lower() in CANCEL_WORDS

    def _audit(self, event_type: str, payload: dict) -> None:
        if not self._audit_logger:
            return
        try:
            self._audit_logger.write(event_type, payload)
        except OSError:
            pass

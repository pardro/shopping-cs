import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.agents.draft_reply_agent import DraftReplyAgent
from app.agents.information_collector_agent import InformationCollectorAgent
from app.agents.judgement_agent import JudgementAgent
from app.agents.planning_agent import PlanningAgent
from app.agents.sub_agent import ChannelCsAgent
from app.agents.tool_calling_agent import ToolCallingAgent
from app.audit import AuditLogger
from app.llm import ChatGPTClient
from app.models import (
    ActionType,
    ChannelName,
    CommandResult,
    ExecutionPlan,
    PlannedAction,
    TargetScope,
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
        self._planning_agent = PlanningAgent(llm, self._planner_context_text)
        self._information_collector_agent = InformationCollectorAgent()
        self._judgement_agent = JudgementAgent(repository, llm)
        self._tool_calling_agent = ToolCallingAgent(llm)

    async def handle_command(self, text: str, user_key: str = "default") -> CommandResult:
        return await self.handle_message(text, user_key=user_key)

    async def handle_message(self, text: str, user_key: str = "default") -> CommandResult:
        normalized = text.strip()
        user_timestamp = datetime.now().astimezone()
        process_log: list[dict[str, Any]] = []
        self._audit(
            "user_request",
            {
                "user_key": user_key,
                "text": text,
            },
            process_log=process_log,
        )
        if not normalized:
            return self._finalize_turn(
                CommandResult(ok=True, message=self.help_text()),
                user_key=user_key,
                user_text=text,
                user_timestamp=user_timestamp,
                process_log=process_log,
            )
        if normalized.lower() in {"/help", "help", "도움말"}:
            return self._finalize_turn(
                CommandResult(ok=True, message=self.help_text()),
                user_key=user_key,
                user_text=text,
                user_timestamp=user_timestamp,
                process_log=process_log,
            )
        if self._is_cancel(normalized):
            self._repository.clear_pending_plan(user_key)
            self._audit(
                "pending_plan_cancelled",
                {"user_key": user_key},
                process_log=process_log,
            )
            return self._finalize_turn(
                CommandResult(ok=True, message="대기 중인 실행 계획을 취소했습니다."),
                user_key=user_key,
                user_text=text,
                user_timestamp=user_timestamp,
                process_log=process_log,
            )
        if self._is_approval(normalized):
            result = await self._execute_pending_plan(user_key, process_log=process_log)
            return self._finalize_turn(
                result,
                user_key=user_key,
                user_text=text,
                user_timestamp=user_timestamp,
                process_log=process_log,
            )

        try:
            result = await self._handle_with_tools(normalized, user_key, process_log)
        except Exception as exc:
            self._audit(
                "tool_pipeline_failed",
                {"error": str(exc)},
                process_log=process_log,
            )
            result = CommandResult(ok=False, message=f"요청 처리 실패: {exc}")

        return self._finalize_turn(
            result,
            user_key=user_key,
            user_text=text,
            user_timestamp=user_timestamp,
            process_log=process_log,
        )

    @staticmethod
    def help_text() -> str:
        return "\n".join(
            [
                "쇼핑몰 CS 비서 사용 예시:",
                "카카오랑 네이버 문의를 동기화하고 미처리 건 요약해줘",
                "네이버 채널 문의건들만 최신화해서 보여줘",
                "카카오 kakao-test-001 고객에게 보낼 답변 초안을 만들어줘",
                "지금 나열해준 배송지연 문의건들에 대해 연휴 배송지연 답변 초안 작성해줘",
                "네이버 1번 고객에게 '확인 후 안내드리겠습니다'라고 보내줘",
                "카카오 kakao-test-001 상담 종료 처리해줘",
                "",
                "흐름:",
                "1. LLM이 사용 가능한 CS 툴 명세를 기준으로 필요한 툴을 직접 선택합니다.",
                "2. 최신 정보가 필요하면 동기화 툴을 먼저 호출합니다.",
                "3. 번호표, 조건, 범위는 선택 툴과 조회 툴로 실제 문의 ID에 연결합니다.",
                "4. 조회, 동기화, 주문조회, 초안, 상담 종료는 즉시 실행합니다.",
                "5. 고객에게 특정 메시지를 전송하는 작업만 승인 대기 계획으로 저장합니다.",
                "6. 전송 계획이 맞으면 '승인' 또는 '실행'이라고 답장하세요.",
                "7. 취소하려면 '취소'라고 답장하세요.",
            ]
        )

    async def _handle_with_tools(
        self,
        user_request: str,
        user_key: str,
        process_log: list[dict[str, Any]],
    ) -> CommandResult:
        self._audit(
            "tool_pipeline_started",
            {
                "agent": "ToolCallingAgent",
                "tools": self._tool_calling_agent.tool_names,
            },
            process_log=process_log,
        )

        async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return await self._execute_cs_tool(name, arguments, user_key, process_log)

        message, trace = await self._tool_calling_agent.handle(
            user_request=user_request,
            user_key=user_key,
            context_text=self._tool_context_text(user_key),
            tool_executor=executor,
        )
        self._audit(
            "tool_pipeline_completed",
            {"tool_call_count": len(trace), "trace": trace},
            process_log=process_log,
        )
        return CommandResult(
            ok=not any(not item.get("result", {}).get("ok", True) for item in trace),
            message=message.strip() or "수행할 작업이 없습니다.",
            data={"tool_trace": trace},
        )

    def _tool_context_text(self, user_key: str) -> str:
        pending_plan = self._repository.get_pending_plan(user_key)
        pending_text = "없음"
        if pending_plan:
            pending_text = self._format_plan_for_approval(pending_plan)
        active = self._active_conversation(user_key)
        active_text = "없음"
        if active:
            active_text = f"{active[0].value} #{active[1]}"
        return "\n".join(
            [
                "현재 날짜/시간:",
                datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
                "",
                "현재 CS 상태:",
                self._summary_text(),
                "",
                "직전에 사용자에게 보여준 문의 번호표:",
                self._last_ticket_mapping_text(user_key),
                "",
                "현재 상담 컨텍스트:",
                active_text,
                "",
                "승인 대기 계획:",
                pending_text,
            ]
        )

    async def _execute_cs_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        user_key: str,
        process_log: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            if name == "sync_conversations":
                result = await self._tool_sync_conversations(arguments, user_key)
            elif name == "summarize_open_tickets":
                result = self._tool_summarize_open_tickets(arguments, user_key)
            elif name == "select_conversations":
                result = self._tool_select_conversations(arguments, user_key)
            elif name == "get_conversation_detail":
                result = await self._tool_conversation_action(
                    ActionType.CONVERSATION_DETAIL,
                    arguments,
                    user_key,
                )
            elif name == "lookup_order":
                result = await self._tool_conversation_action(
                    ActionType.ORDER_LOOKUP,
                    arguments,
                    user_key,
                )
            elif name == "draft_reply":
                result = await self._tool_conversation_action(
                    ActionType.DRAFT_REPLY,
                    arguments,
                    user_key,
                )
            elif name == "prepare_send_replies":
                result = self._tool_prepare_send_replies(arguments, user_key)
            elif name == "close_ticket":
                result = await self._tool_conversation_action(
                    ActionType.CLOSE_TICKET,
                    arguments,
                    user_key,
                )
            else:
                result = self._tool_result(False, f"지원하지 않는 툴입니다: {name}")
        except Exception as exc:
            result = self._tool_result(False, str(exc))

        self._audit(
            "tool_called",
            {"tool": name, "arguments": arguments, "result": result},
            process_log=process_log,
        )
        return result

    async def _tool_sync_conversations(
        self,
        arguments: dict[str, Any],
        user_key: str,
    ) -> dict[str, Any]:
        channel = self._channel_from_tool(arguments.get("channel"))
        action = PlannedAction(
            type=ActionType.SYNC,
            channel=channel,
            reason="LLM tool-call requested fresh channel data.",
        )
        result = await self._execute_action(action, user_key)
        ok = "동기화 실패" not in result and bool(result.strip())
        return self._tool_result(
            ok,
            result or "동기화된 채널이 없습니다.",
            {"channel": channel.value if channel else "all"},
        )

    def _tool_summarize_open_tickets(
        self,
        arguments: dict[str, Any],
        user_key: str,
    ) -> dict[str, Any]:
        channel = self._channel_from_tool(arguments.get("channel"))
        result = self._summary_text(user_key=user_key, channel=channel)
        return self._tool_result(True, result, {"channel": channel.value if channel else "all"})

    async def _tool_conversation_action(
        self,
        action_type: ActionType,
        arguments: dict[str, Any],
        user_key: str,
    ) -> dict[str, Any]:
        channel = self._channel_from_tool(arguments.get("channel"), allow_all=False)
        if channel is None:
            raise ValueError("channel is required")
        conversation_id = self._resolve_tool_conversation_id(
            channel,
            str(arguments.get("conversation_id") or ""),
            user_key,
        )
        action = PlannedAction(
            type=action_type,
            channel=channel,
            conversation_id=conversation_id,
            message=arguments.get("guidance"),
            reason="LLM tool-call requested this CS operation.",
        )
        result = await self._execute_action(action, user_key)
        return self._tool_result(
            True,
            result,
            {"channel": channel.value, "conversation_id": conversation_id},
        )

    def _tool_select_conversations(
        self,
        arguments: dict[str, Any],
        user_key: str,
    ) -> dict[str, Any]:
        scope = TargetScope(str(arguments.get("scope") or TargetScope.LAST_LISTED.value))
        channel = self._channel_from_tool(arguments.get("channel"))
        target_filter = str(arguments.get("target_filter") or "").strip()
        limit = int(arguments.get("limit") or 20)
        candidates = self._tool_candidate_conversations(scope, channel, user_key)
        selected = candidates
        if target_filter:
            selected = self._judgement_agent._fallback_select(target_filter, candidates)
        selected = selected[: max(1, min(limit, 50))]
        payload = [self._conversation_brief(conversation) for conversation in selected]
        if not payload:
            return self._tool_result(
                True,
                "조건에 맞는 문의를 찾지 못했습니다.",
                {"selected": [], "target_filter": target_filter, "scope": scope.value},
            )
        lines = ["선택된 문의:"]
        lines.extend(
            f"- {item['channel']} #{item['conversation_id']} {item['customer_name']} "
            f"{item['latest_message']}".rstrip()
            for item in payload
        )
        return self._tool_result(
            True,
            "\n".join(lines),
            {"selected": payload, "target_filter": target_filter, "scope": scope.value},
        )

    def _tool_prepare_send_replies(
        self,
        arguments: dict[str, Any],
        user_key: str,
    ) -> dict[str, Any]:
        replies = arguments.get("replies") or []
        if not isinstance(replies, list) or not replies:
            raise ValueError("replies must include at least one message")
        actions: list[PlannedAction] = []
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            channel = self._channel_from_tool(reply.get("channel"), allow_all=False)
            if channel is None:
                raise ValueError("reply channel is required")
            conversation_id = self._resolve_tool_conversation_id(
                channel,
                str(reply.get("conversation_id") or ""),
                user_key,
            )
            message = str(reply.get("message") or "").strip()
            if not message:
                raise ValueError("reply message is required")
            action = PlannedAction(
                type=ActionType.SEND_REPLY,
                channel=channel,
                conversation_id=conversation_id,
                message=message,
                reason=str(arguments.get("reason") or "고객 답변 전송 요청"),
            )
            action.prepared_api = self._prepared_api_text(action)
            actions.append(action)
        if not actions:
            raise ValueError("no valid replies were provided")

        existing = self._repository.get_pending_plan(user_key)
        raw_risk_notes = arguments.get("risk_notes") or []
        if isinstance(raw_risk_notes, str):
            risk_notes = [raw_risk_notes]
        else:
            risk_notes = [str(note) for note in raw_risk_notes]
        if existing and existing.actions:
            plan = existing.model_copy(
                update={
                    "actions": [*existing.actions, *actions],
                    "risk_notes": [*existing.risk_notes, *risk_notes],
                }
            )
        else:
            plan = ExecutionPlan(
                user_goal="고객 답변 전송 승인 대기",
                summary=f"{len(actions)}건의 고객 답변 전송을 승인 대기 상태로 저장합니다.",
                actions=actions,
                risk_notes=risk_notes,
            )
        self._repository.save_pending_plan(user_key, plan)
        approval_text = self._format_plan_for_approval(plan)
        return self._tool_result(
            True,
            approval_text,
            {"pending_actions": [action.model_dump() for action in plan.actions]},
        )

    def _tool_candidate_conversations(
        self,
        scope: TargetScope,
        channel: ChannelName | None,
        user_key: str,
    ) -> list:
        if scope == TargetScope.LAST_LISTED:
            context = self._repository.get_user_context(user_key)
            mapping = context.get("last_open_ticket_mapping", {})
            if not isinstance(mapping, dict):
                return []
            conversations = []
            for channel_value, indexed_ids in mapping.items():
                if channel and channel_value != channel.value:
                    continue
                try:
                    current_channel = ChannelName(channel_value)
                except ValueError:
                    continue
                if not isinstance(indexed_ids, dict):
                    continue
                for conversation_id in indexed_ids.values():
                    conversation = self._repository.get_conversation(
                        current_channel,
                        str(conversation_id),
                    )
                    if conversation:
                        conversations.append(conversation)
            return conversations
        if scope == TargetScope.CHANNEL_OPEN:
            return self._repository.list_conversations(
                channel=channel,
                status=TicketStatus.OPEN,
                limit=100,
            )
        return self._repository.list_conversations(status=TicketStatus.OPEN, limit=100)

    @staticmethod
    def _conversation_brief(conversation) -> dict[str, str]:
        latest_message = ""
        if conversation.messages:
            latest_message = conversation.messages[-1].text[:160]
        return {
            "channel": conversation.channel.value,
            "conversation_id": conversation.conversation_id,
            "customer_name": conversation.customer_name or "",
            "latest_message": latest_message,
            "status": conversation.status.value,
        }

    def _resolve_tool_conversation_id(
        self,
        channel: ChannelName,
        conversation_id: str,
        user_key: str,
    ) -> str:
        if not conversation_id.strip():
            raise ValueError("conversation_id is required")
        action = PlannedAction(
            type=ActionType.CONVERSATION_DETAIL,
            channel=channel,
            conversation_id=conversation_id,
            reason="툴 입력 번호표 해석",
        )
        resolved = self._resolve_numbered_reference(action, user_key)
        return self._normalize_conversation_id(resolved or conversation_id)

    @staticmethod
    def _channel_from_tool(value: Any, allow_all: bool = True) -> ChannelName | None:
        text = str(value or "all").strip().lower()
        aliases = {
            "카카오": ChannelName.KAKAO.value,
            "kakao": ChannelName.KAKAO.value,
            "네이버": ChannelName.NAVER.value,
            "naver": ChannelName.NAVER.value,
        }
        if allow_all and text in {"", "all", "전체", "both", "kakao/naver"}:
            return None
        text = aliases.get(text, text)
        return ChannelName(text)

    @staticmethod
    def _tool_result(
        ok: bool,
        content: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"ok": ok, "content": content, "data": data or {}}

    async def _create_plan(self, user_request: str, user_key: str) -> ExecutionPlan:
        return await self._planning_agent.create_plan(user_request, user_key)

    async def _execute_pending_plan(
        self,
        user_key: str,
        process_log: list[dict[str, Any]] | None = None,
    ) -> CommandResult:
        plan = self._repository.get_pending_plan(user_key)
        if not plan:
            self._audit(
                "pending_plan_missing",
                {"user_key": user_key},
                process_log=process_log,
            )
            return CommandResult(
                ok=False,
                message="승인할 실행 계획이 없습니다. 먼저 원하는 CS 작업을 자연어로 요청해주세요.",
            )

        self._audit(
            "pending_plan_approved",
            {"user_key": user_key, "plan": plan.model_dump()},
            process_log=process_log,
        )
        result = await self._execute_plan(plan, user_key, process_log=process_log)
        self._repository.clear_pending_plan(user_key)
        self._audit(
            "pending_plan_cleared",
            {"user_key": user_key},
            process_log=process_log,
        )
        return result

    async def _execute_plan(
        self,
        plan: ExecutionPlan,
        user_key: str,
        process_log: list[dict[str, Any]] | None = None,
    ) -> CommandResult:
        if any(action.type == ActionType.ORDER_LOOKUP for action in plan.actions):
            return await self._execute_order_lookup_plan(
                plan,
                user_key,
                process_log=process_log,
            )

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
                    process_log=process_log,
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
                    process_log=process_log,
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

    async def _execute_order_lookup_plan(
        self,
        plan: ExecutionPlan,
        user_key: str,
        process_log: list[dict[str, Any]] | None = None,
    ) -> CommandResult:
        result_lines: list[str] = []
        content_blocks: list[str] = []
        raw_results: list[str] = []
        ok = True

        for index, action in enumerate(plan.actions, start=1):
            try:
                result = await self._execute_action(action, user_key)
                raw_results.append(f"{index}. 완료 - {result}")
                if action.type == ActionType.SYNC:
                    result_lines.extend(self._format_sync_result_lines(result))
                elif action.type == ActionType.ORDER_LOOKUP:
                    if content_blocks:
                        content_blocks.append("---")
                    content_blocks.append(result)
                else:
                    result_lines.append(f"{self._describe_action(action)} 성공")
                self._audit(
                    "action_executed",
                    {
                        "user_key": user_key,
                        "action": action.model_dump(),
                        "result": result,
                    },
                    process_log=process_log,
                )
            except Exception as exc:
                ok = False
                failure = f"{index}. 실패 - {self._describe_action(action)}: {exc}"
                raw_results.append(failure)
                result_lines.append(failure)
                self._audit(
                    "action_failed",
                    {
                        "user_key": user_key,
                        "action": action.model_dump(),
                        "error": str(exc),
                    },
                    process_log=process_log,
                )
                remaining = len(plan.actions) - index
                if remaining:
                    result_lines.append(f"남은 {remaining}개 작업은 실행하지 않았습니다.")
                break

        message_parts = ["# 실행 결과", *result_lines]
        if content_blocks:
            message_parts.extend(["", "# 내용", *content_blocks])
        return CommandResult(
            ok=ok,
            message="\n".join(message_parts),
            data={"plan": plan.model_dump(), "results": raw_results},
        )

    @staticmethod
    def _format_sync_result_lines(result: str) -> list[str]:
        lines: list[str] = []
        for part in result.split(","):
            normalized = part.strip()
            if not normalized:
                continue
            if "건 동기화" in normalized:
                channel, count = normalized.split(" ", 1)
                lines.append(f"{channel} 동기화 성공 : {count.replace(' 동기화', '')}")
            else:
                lines.append(f"{normalized} 성공")
        return lines

    async def _execute_action(self, action: PlannedAction, user_key: str) -> str:
        if action.type == ActionType.SYNC:
            synced: dict[str, int] = {}
            failures: dict[str, str] = {}
            targets = [action.channel] if action.channel else list(self._sub_agents.keys())
            for channel in targets:
                if channel is None:
                    continue
                try:
                    synced[channel.value] = await self._sub_agents[channel].sync()
                except Exception as exc:
                    if action.channel:
                        raise
                    failures[channel.value] = str(exc)
            parts = [f"{channel} {count}건 동기화" for channel, count in synced.items()]
            parts.extend(f"{channel} 동기화 실패: {error}" for channel, error in failures.items())
            return ", ".join(parts)

        if action.type == ActionType.SUMMARY:
            return self._summary_text(user_key=user_key, channel=action.channel)

        channel = self._require_channel(action)
        conversation_id = self._require_conversation_id(action)
        agent = self._sub_agents[channel]

        if action.type == ActionType.CONVERSATION_DETAIL:
            conversation = self._repository.get_conversation(channel, conversation_id)
            if not conversation:
                raise ValueError(
                    f"{channel.value} conversation '{conversation_id}' was not found. "
                    "먼저 채널 동기화를 요청해주세요."
                )
            self._set_active_conversation(user_key, channel, conversation_id)
            history = DraftReplyAgent.format_conversation_history(conversation)
            return f"{channel.value} #{conversation_id} 이전 대화 기록\n{history}"

        if action.type == ActionType.ORDER_LOOKUP:
            return await self._order_lookup_text(channel, conversation_id, user_key)

        if action.type == ActionType.DRAFT_REPLY:
            conversation = self._repository.get_conversation(channel, conversation_id)
            if not conversation:
                raise ValueError(
                    f"{channel.value} conversation '{conversation_id}' was not found. "
                    "먼저 채널 동기화를 요청해주세요."
                )
            draft = await agent.draft_reply(conversation_id, guidance=action.message)
            self._set_active_conversation(user_key, channel, conversation_id)
            history = DraftReplyAgent.format_conversation_history(conversation)
            return (
                f"{channel.value} #{conversation_id} 이전 대화 기록\n"
                f"{history}\n\n"
                f"답변 초안:\n{draft.reply}\n"
                f"근거: {draft.rationale}"
            )

        if action.type == ActionType.SEND_REPLY:
            if not action.message:
                raise ValueError("전송할 메시지가 없습니다")
            await agent.send_reply(conversation_id, action.message)
            self._set_active_conversation(user_key, channel, conversation_id)
            return f"{channel.value} #{conversation_id} 답변 전송"

        if action.type == ActionType.CLOSE_TICKET:
            await agent.close(conversation_id)
            self._clear_active_conversation(user_key, channel, conversation_id)
            return f"{channel.value} #{conversation_id} 상담 종료"

        raise ValueError(f"지원하지 않는 액션입니다: {action.type}")

    def _summary_text(
        self,
        user_key: str | None = None,
        channel: ChannelName | None = None,
    ) -> str:
        summaries = self._repository.summarize()
        lines = [f"{channel.value} CS 현황" if channel else "채널별 CS 현황"]
        for summary in summaries:
            if channel and summary.channel != channel:
                continue
            lines.append(
                f"- {summary.channel.value}: open {summary.open_count}, "
                f"pending {summary.pending_count}, closed {summary.closed_count}"
            )
        open_tickets = self._repository.list_conversations(
            channel=channel,
            status=TicketStatus.OPEN,
            limit=50,
        )
        mapping: dict[str, dict[str, str]] = {channel.value: {} for channel in ChannelName}
        if open_tickets:
            lines.append("\n미처리 문의 목록")
            channels = [channel] if channel else list(ChannelName)
            for current_channel in channels:
                channel_tickets = [
                    ticket for ticket in open_tickets if ticket.channel == current_channel
                ]
                if not channel_tickets:
                    continue
                lines.append(f"\n{current_channel.value}")
                for index, ticket in enumerate(channel_tickets, start=1):
                    mapping[current_channel.value][str(index)] = ticket.conversation_id
                    lines.append(f"{index}. {self._ticket_line(ticket)}")
        if user_key:
            self._update_user_context(
                user_key,
                {
                    "last_open_ticket_mapping": mapping,
                    "last_open_ticket_text": "\n".join(lines),
                },
            )
        return "\n".join(lines)

    async def _order_lookup_text(
        self,
        channel: ChannelName,
        conversation_id: str,
        user_key: str,
    ) -> str:
        conversation = self._repository.get_conversation(channel, conversation_id)
        if not conversation:
            raise ValueError(
                f"{channel.value} conversation '{conversation_id}' was not found. "
                "먼저 채널 동기화를 요청해주세요."
            )
        self._set_active_conversation(user_key, channel, conversation_id)
        order_details = await self._load_order_details(channel, conversation)
        latest_message = self._latest_message_text(conversation)
        order_summary = self._format_order_summary(conversation.raw, order_details)
        order_status = self._format_order_status(conversation.raw, order_details)
        timestamp = self._conversation_display_time(conversation)
        channel_label = channel.value.upper()
        return "\n".join(
            [
                f"[1] {channel_label} #{conversation_id}",
                f"{timestamp} | {order_status}",
                "",
                "문의",
                latest_message,
                "",
                "주문",
                order_summary,
            ]
        )

    async def _load_order_details(
        self,
        channel: ChannelName,
        conversation,
    ) -> list[dict[str, Any]]:
        product_order_ids = self._product_order_ids(conversation.raw)
        if not product_order_ids:
            return []
        agent = self._sub_agents[channel]
        try:
            payload = await agent.get_order_details(product_order_ids)
        except Exception as exc:
            return [{"orderLookupError": str(exc)}]
        return self._order_detail_items(payload)

    @staticmethod
    def _product_order_ids(raw: dict[str, Any]) -> list[str]:
        value = (
            raw.get("productOrderIdList")
            or raw.get("productOrderIds")
            or raw.get("product_order_ids")
            or raw.get("productOrderId")
        )
        if not value:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()]

    @staticmethod
    def _order_detail_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data", payload)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        contents = data.get("contents") or data.get("productOrders") or data.get("orders")
        if isinstance(contents, list):
            return [item for item in contents if isinstance(item, dict)]
        if "productOrder" in data or "order" in data:
            return [data]
        return []

    def _format_order_summary(
        self,
        raw: dict[str, Any],
        details: list[dict[str, Any]],
    ) -> str:
        if details:
            summaries = []
            for detail in details:
                if detail.get("orderLookupError"):
                    continue
                product_order = detail.get("productOrder") or detail
                order = detail.get("order") or {}
                product_name = (
                    product_order.get("productName")
                    or raw.get("productName")
                    or "상품명 미확인"
                )
                option = self._format_order_option(
                    product_order.get("productOption") or raw.get("productOrderOption")
                )
                quantity = product_order.get("quantity")
                order_id = (
                    order.get("orderId")
                    or product_order.get("orderId")
                    or raw.get("orderId")
                )
                parts = [str(product_name)]
                if option:
                    parts.append(f"- 옵션: {option}")
                if quantity:
                    parts.append(f"- 수량: {self._format_order_quantity(quantity)}")
                if order_id:
                    parts.append(f"- 주문번호: {order_id}")
                summaries.append("\n".join(parts))
            if summaries:
                return "\n\n".join(summaries)

        product_name = raw.get("productName") or "상품명 미확인"
        option = self._format_order_option(raw.get("productOrderOption"))
        quantity = (
            raw.get("quantity")
            or raw.get("orderQuantity")
            or raw.get("productOrderQuantity")
        )
        order_id = raw.get("orderId")
        product_order_ids = raw.get("productOrderIdList")
        parts = [str(product_name)]
        if option:
            parts.append(f"- 옵션: {option}")
        if quantity:
            parts.append(f"- 수량: {self._format_order_quantity(quantity)}")
        if order_id:
            parts.append(f"- 주문번호: {order_id}")
        if product_order_ids:
            parts.append(f"- 상품주문번호: {product_order_ids}")
        return "\n".join(parts)

    @staticmethod
    def _format_order_option(option: Any) -> str:
        if not option:
            return ""
        values: list[str] = []
        for raw_part in str(option).split("/"):
            part = raw_part.strip()
            if not part:
                continue
            if ":" in part:
                part = part.split(":", 1)[1].strip()
            part = re.sub(r"^\(([^)]+)\)\s*", r"\1 ", part).strip()
            values.append(part)
        return " / ".join(values)

    @staticmethod
    def _format_order_quantity(quantity: Any) -> str:
        text = str(quantity).strip()
        if not text:
            return ""
        return text if text.endswith("개") else f"{text}개"

    @staticmethod
    def _format_order_status(raw: dict[str, Any], details: list[dict[str, Any]]) -> str:
        statuses = []
        for detail in details:
            if detail.get("orderLookupError"):
                statuses.append(f"주문 상세 조회 실패: {detail['orderLookupError']}")
                continue
            product_order = detail.get("productOrder") or detail
            claim = detail.get("claim") or {}
            delivery = detail.get("delivery") or {}
            status = (
                product_order.get("productOrderStatus")
                or product_order.get("placeOrderStatus")
                or claim.get("claimStatus")
                or delivery.get("deliveryStatus")
            )
            if status:
                statuses.append(str(status))
        if statuses:
            return ", ".join(dict.fromkeys(statuses))
        return str(
            raw.get("productOrderStatus")
            or raw.get("claimStatus")
            or raw.get("deliveryStatus")
            or "상태 미확인"
        )

    @staticmethod
    def _latest_message_text(conversation) -> str:
        if not conversation.messages:
            return "저장된 대화 없음"
        return conversation.messages[-1].text[:120]

    @staticmethod
    def _conversation_display_time(conversation) -> str:
        raw_time = (
            conversation.raw.get("inquiryRegistrationDateTime")
            or conversation.raw.get("created_at")
            or conversation.raw.get("createdAt")
        )
        parsed: datetime | None = None
        if raw_time:
            try:
                parsed = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            except ValueError:
                parsed = None
        if not parsed and conversation.messages:
            parsed = conversation.messages[-1].created_at
        if not parsed:
            parsed = datetime.now().astimezone()
        return parsed.astimezone().strftime("%y-%m-%d %H:%M")

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
        selector = MainAgent._target_selector_text(action)
        if action.type == ActionType.SYNC:
            return f"{channel} 대화 동기화"
        if action.type == ActionType.SUMMARY:
            return f"{channel} CS 현황 요약" if action.channel else "CS 현황 요약"
        if action.type == ActionType.CONVERSATION_DETAIL:
            return f"{channel}{target or selector} 이전 대화 기록 조회"
        if action.type == ActionType.ORDER_LOOKUP:
            return f"{channel}{target or selector} 주문내역 조회"
        if action.type == ActionType.DRAFT_REPLY:
            return f"{channel}{target or selector} 답변 초안 생성"
        if action.type == ActionType.SEND_REPLY:
            return f"{channel}{target or selector} 고객 답변 전송: {action.message}"
        if action.type == ActionType.CLOSE_TICKET:
            return f"{channel}{target or selector} 상담 종료"
        return str(action.type)

    @staticmethod
    def _target_selector_text(action: PlannedAction) -> str:
        if action.target_scope == TargetScope.EXPLICIT:
            return ""
        labels = {
            TargetScope.LAST_LISTED: " 직전 목록",
            TargetScope.CHANNEL_OPEN: " 미처리 전체",
            TargetScope.ALL_OPEN: " 전체 미처리",
        }
        selector = labels.get(action.target_scope, f" {action.target_scope.value}")
        if action.target_filter:
            selector = f"{selector} 중 '{action.target_filter}'"
        return selector

    def _planner_context_text(self, user_key: str) -> str:
        return "\n".join(
            [
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

    def _prepare_plan(self, plan: ExecutionPlan, user_key: str) -> ExecutionPlan:
        for action in plan.actions:
            action.conversation_id = self._resolve_numbered_reference(action, user_key)
            action.prepared_api = self._prepared_api_text(action)
        return plan

    def _optimize_plan_for_fresh_context(self, plan: ExecutionPlan) -> ExecutionPlan:
        optimized = self._information_collector_agent.add_freshness_steps(plan)
        for action in optimized.actions:
            action.prepared_api = self._prepared_api_text(action)
        return optimized

    def _append_contextual_close_if_needed(
        self,
        plan: ExecutionPlan,
        user_key: str,
    ) -> ExecutionPlan:
        active = self._active_conversation(user_key)
        if not active:
            return plan
        active_channel, active_conversation_id = active
        if any(
            action.channel == active_channel and action.conversation_id == active_conversation_id
            for action in plan.actions
        ):
            return plan
        close_action = PlannedAction(
            type=ActionType.CLOSE_TICKET,
            channel=active_channel,
            conversation_id=active_conversation_id,
            reason="새 요청이 직전에 다루던 상담 맥락이 아니므로 이전 상담을 자동 종료합니다.",
            prepared_api="맥락 전환 감지에 따른 상담 종료 API 호출",
        )
        return plan.model_copy(update={"actions": [*plan.actions, close_action]})

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
        normalized_id = self._normalize_conversation_id(conversation_id)
        number = normalized_id.replace("번", "")
        if not number.isdigit():
            return normalized_id
        context = self._repository.get_user_context(user_key)
        mapping = context.get("last_open_ticket_mapping", {})
        channel_mapping = mapping.get(action.channel.value, {})
        return channel_mapping.get(number, normalized_id)

    @staticmethod
    def _normalize_conversation_id(conversation_id: str) -> str:
        normalized = str(conversation_id).strip().strip("`'\"")
        while normalized.startswith("#"):
            normalized = normalized[1:].strip()
        if normalized.endswith("번"):
            normalized = normalized[:-1].strip()
        return normalized

    @staticmethod
    def _prepared_api_text(action: PlannedAction) -> str:
        channel = action.channel.value if action.channel else "kakao/naver"
        conversation_id = action.conversation_id or "{conversation_id}"
        selector = MainAgent._target_selector_text(action).strip()
        if action.type == ActionType.SYNC:
            return f"GET {channel} 대화 목록 API"
        if action.type == ActionType.SUMMARY:
            return f"로컬 DB {channel} 미처리 문의 조회"
        if action.type == ActionType.CONVERSATION_DETAIL:
            target = f"{channel} #{conversation_id}" if action.conversation_id else selector
            return f"로컬 DB 전체 이전 대화 기록 조회, 대상 {target}"
        if action.type == ActionType.ORDER_LOOKUP:
            target = f"{channel} #{conversation_id}" if action.conversation_id else selector
            return f"로컬 DB 문의 정보와 채널 주문 상세 API 조회, 대상 {target}"
        if action.type == ActionType.DRAFT_REPLY:
            target = f"{channel} #{conversation_id}" if action.conversation_id else selector
            return (
                "로컬 DB 전체 이전 대화 기록 조회 후 "
                f"OpenAI ChatGPT 답변 초안 생성, 대상 {target}"
            )
        if action.type == ActionType.SEND_REPLY:
            target = f"#{conversation_id}" if action.conversation_id else selector
            return f"POST {channel} 메시지 전송 API, 대상 {target}"
        if action.type == ActionType.CLOSE_TICKET:
            target = f"#{conversation_id}" if action.conversation_id else selector
            return f"PATCH {channel} 상담 상태 변경 API, 대상 {target}"
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
            pairs = ", ".join(
                f"{number}번 -> {conversation_id}"
                for number, conversation_id in items.items()
            )
            lines.append(f"{channel}: {pairs}")
        return "\n".join(lines) if lines else "저장된 번호표 없음"

    def _recent_open_ticket_text(self) -> str:
        tickets = self._repository.list_conversations(status=TicketStatus.OPEN, limit=10)
        if not tickets:
            return "open 티켓 없음"
        return "\n".join(
            (
                f"- {ticket.channel.value} #{ticket.conversation_id} "
                f"{ticket.customer_name or ''}"
            ).rstrip()
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

    def _active_conversation(self, user_key: str) -> tuple[ChannelName, str] | None:
        context = self._repository.get_user_context(user_key)
        active = context.get("active_conversation")
        if not isinstance(active, dict):
            return None
        channel_value = active.get("channel")
        conversation_id = active.get("conversation_id")
        if not channel_value or not conversation_id:
            return None
        try:
            channel = ChannelName(channel_value)
        except ValueError:
            return None
        return channel, str(conversation_id)

    def _set_active_conversation(
        self,
        user_key: str,
        channel: ChannelName,
        conversation_id: str,
    ) -> None:
        self._update_user_context(
            user_key,
            {
                "active_conversation": {
                    "channel": channel.value,
                    "conversation_id": conversation_id,
                }
            },
        )

    def _clear_active_conversation(
        self,
        user_key: str,
        channel: ChannelName,
        conversation_id: str,
    ) -> None:
        active = self._active_conversation(user_key)
        if not active:
            return
        active_channel, active_conversation_id = active
        if active_channel != channel or active_conversation_id != conversation_id:
            return
        self._update_user_context(user_key, {"active_conversation": None})

    def _update_user_context(self, user_key: str, updates: dict) -> None:
        context = self._repository.get_user_context(user_key)
        context.update(updates)
        self._repository.save_user_context(user_key, context)

    @staticmethod
    def _require_channel(action: PlannedAction) -> ChannelName:
        if not action.channel:
            raise ValueError("채널 정보가 필요합니다")
        return action.channel

    @staticmethod
    def _require_conversation_id(action: PlannedAction) -> str:
        if not action.conversation_id:
            raise ValueError("대화 ID가 필요합니다")
        return MainAgent._normalize_conversation_id(action.conversation_id)

    @staticmethod
    def _is_approval(text: str) -> bool:
        return text.strip().lower() in APPROVAL_WORDS

    @staticmethod
    def _is_cancel(text: str) -> bool:
        return text.strip().lower() in CANCEL_WORDS

    def _finalize_turn(
        self,
        result: CommandResult,
        *,
        user_key: str,
        user_text: str,
        user_timestamp: datetime,
        process_log: list[dict[str, Any]],
    ) -> CommandResult:
        self._audit(
            "final_response",
            {
                "ok": result.ok,
                "message": result.message,
                "data": result.data,
            },
            process_log=process_log,
        )
        if not self._audit_logger:
            return result
        try:
            self._audit_logger.write_turn(
                user_key=user_key,
                user_text=user_text,
                chatbot_text=result.message,
                actions=process_log,
                user_timestamp=user_timestamp,
            )
        except OSError:
            pass
        return result

    @staticmethod
    def _process_log_record(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "action_type": event_type,
            "timestamp": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            **payload,
        }

    def _audit(
        self,
        event_type: str,
        payload: dict[str, Any],
        process_log: list[dict[str, Any]] | None = None,
    ) -> None:
        if process_log is None:
            return
        process_log.append(self._process_log_record(event_type, payload))

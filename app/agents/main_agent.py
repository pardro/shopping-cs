import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.agents.draft_reply_agent import DraftReplyAgent
from app.agents.information_collector_agent import InformationCollectorAgent
from app.agents.judgement_agent import JudgementAgent
from app.agents.planning_agent import PlanningAgent
from app.agents.sub_agent import ChannelCsAgent
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
            plan = self._optimize_plan_for_fresh_context(plan)
            plan = self._append_contextual_close_if_needed(plan, user_key)
        except Exception as exc:
            return CommandResult(ok=False, message=f"요청 분석 실패: {exc}")

        if plan.needs_more_info or not plan.actions:
            question = plan.question or "수행에 필요한 정보가 부족합니다. 조금 더 구체적으로 말씀해주세요."
            self._audit(
                "plan_needs_more_info",
                {"user_key": user_key, "question": question, "plan": plan.model_dump()},
            )
            return CommandResult(ok=True, message=question, data={"plan": plan.model_dump()})

        messages: list[str] = []
        results: list[str] = []
        ok = True

        collector = self._information_collector_agent
        information_plan, execution_plan = collector.split_information_actions(plan)
        if information_plan.actions:
            information_result = await self._execute_plan(information_plan, user_key)
            ok = information_result.ok
            messages.append(information_result.message)
            results.extend(information_result.data.get("results", []))
            if not information_result.ok:
                return CommandResult(
                    ok=False,
                    message="\n\n".join(messages),
                    data={"plan": plan.model_dump(), "results": results},
                )

        judged_plan_result = await self._judgement_agent.finalize_plan(execution_plan, user_key)
        execution_plan = self._prepare_plan(judged_plan_result.plan, user_key)
        if judged_plan_result.notes:
            self._audit(
                "plan_judged",
                {
                    "user_key": user_key,
                    "notes": judged_plan_result.notes,
                    "plan": execution_plan.model_dump(),
                },
            )
        if execution_plan.needs_more_info or not execution_plan.actions:
            if execution_plan.needs_more_info:
                question = execution_plan.question or "수행할 대상 문의를 판단하려면 정보가 더 필요합니다."
                self._audit(
                    "plan_needs_more_info",
                    {
                        "user_key": user_key,
                        "question": question,
                        "plan": execution_plan.model_dump(),
                    },
                )
                messages.append(question)
            elif not messages:
                messages.append("수행할 작업이 없습니다.")
            return CommandResult(
                ok=ok,
                message="\n\n".join(messages),
                data={
                    "plan": plan.model_dump(),
                    "execution_plan": execution_plan.model_dump(),
                    "results": results,
                    "judgement_notes": judged_plan_result.notes,
                },
            )

        immediate_plan, approval_plan = self._split_plan_by_approval_requirement(execution_plan)

        if immediate_plan.actions:
            execution_result = await self._execute_plan(immediate_plan, user_key)
            ok = execution_result.ok
            messages.append(execution_result.message)
            results.extend(execution_result.data.get("results", []))
            if not execution_result.ok:
                return CommandResult(
                    ok=False,
                    message="\n\n".join(messages),
                    data={
                        "plan": plan.model_dump(),
                        "execution_plan": execution_plan.model_dump(),
                        "results": results,
                        "judgement_notes": judged_plan_result.notes,
                    },
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
            data={
                "plan": plan.model_dump(),
                "execution_plan": execution_plan.model_dump(),
                "results": results,
                "judgement_notes": judged_plan_result.notes,
            },
        )

    @staticmethod
    def help_text() -> str:
        return "\n".join(
            [
                "쇼핑몰 CS 비서 사용 예시:",
                "카카오랑 네이버 문의를 동기화하고 미처리 건 요약해줘",
                "네이버 채널 문의건들만 최신화해서 보여줘",
                "카카오 kakao-test-001 고객에게 보낼 답변 초안을 만들어줘",
                "카카오 채널 문의건 전체에 배송지연 안내 답변 초안 작성해줘",
                "지금 나열해준 배송지연 문의건들에 대해 연휴 배송지연 답변 초안 작성해줘",
                "네이버 naver-test-001 고객에게 '확인 후 안내드리겠습니다'라고 보내줘",
                "카카오 kakao-test-001 상담 종료 처리해줘",
                "",
                "흐름:",
                "1. 분석/계획 에이전트가 요청을 해석하고 필요한 서브 에이전트를 고릅니다.",
                "2. 필요하면 채널 최신화로 정보를 먼저 수집합니다.",
                "3. 취합 및 판단 에이전트가 조건에 맞는 문의 대상을 확정합니다.",
                "4. 조회, 동기화, 초안, 상담 종료는 바로 실행합니다.",
                "5. 고객에게 특정 메시지를 전송하는 작업만 먼저 승인 요청을 드립니다.",
                "6. 전송 계획이 맞으면 '승인' 또는 '실행'이라고 답장하세요.",
                "7. 취소하려면 '취소'라고 답장하세요.",
            ]
        )

    async def _create_plan(self, user_request: str, user_key: str) -> ExecutionPlan:
        return await self._planning_agent.create_plan(user_request, user_key)

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
        if any(action.type == ActionType.ORDER_LOOKUP for action in plan.actions):
            return await self._execute_order_lookup_plan(plan, user_key)

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

    async def _execute_order_lookup_plan(
        self,
        plan: ExecutionPlan,
        user_key: str,
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

    def _audit(self, event_type: str, payload: dict) -> None:
        if not self._audit_logger:
            return
        try:
            self._audit_logger.write(event_type, payload)
        except OSError:
            pass

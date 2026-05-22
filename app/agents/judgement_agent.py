import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from app.models import (
    ActionType,
    ChannelName,
    Conversation,
    ExecutionPlan,
    PlannedAction,
    TargetScope,
    TicketStatus,
)
from app.storage import CsRepository


class CandidateSelection(BaseModel):
    channel: ChannelName
    conversation_id: str
    selected: bool
    reason: str = ""


class CandidateSelectionResult(BaseModel):
    selections: list[CandidateSelection] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


@dataclass
class JudgedPlan:
    plan: ExecutionPlan
    notes: list[str] = field(default_factory=list)


class JudgementAgent:
    def __init__(self, repository: CsRepository, llm: Any):
        self._repository = repository
        self._llm = llm

    async def finalize_plan(self, plan: ExecutionPlan, user_key: str) -> JudgedPlan:
        finalized_actions: list[PlannedAction] = []
        notes: list[str] = []

        for action in plan.actions:
            if not self._requires_target_expansion(action):
                finalized_actions.append(action)
                continue

            candidates = self._candidate_conversations(action, user_key)
            if not candidates:
                question = self._missing_target_question(action)
                return JudgedPlan(
                    plan=plan.model_copy(
                        update={
                            "actions": finalized_actions,
                            "needs_more_info": True,
                            "question": question,
                        }
                    ),
                    notes=notes,
                )

            selected = await self._select_candidates(plan, action, candidates)
            if not selected:
                question = self._no_match_question(action)
                return JudgedPlan(
                    plan=plan.model_copy(
                        update={
                            "actions": finalized_actions,
                            "needs_more_info": True,
                            "question": question,
                        }
                    ),
                    notes=notes,
                )

            notes.append(
                f"{self._scope_label(action.target_scope)}에서 {len(selected)}건을 실행 대상으로 판단했습니다."
            )
            finalized_actions.extend(self._expand_action(action, selected))

        return JudgedPlan(plan=plan.model_copy(update={"actions": finalized_actions}), notes=notes)

    @staticmethod
    def _requires_target_expansion(action: PlannedAction) -> bool:
        return (
            action.type
            in {
                ActionType.CONVERSATION_DETAIL,
                ActionType.ORDER_LOOKUP,
                ActionType.DRAFT_REPLY,
                ActionType.SEND_REPLY,
                ActionType.CLOSE_TICKET,
            }
            and not action.conversation_id
        )

    def _candidate_conversations(
        self,
        action: PlannedAction,
        user_key: str,
    ) -> list[Conversation]:
        if action.target_scope == TargetScope.LAST_LISTED:
            return self._last_listed_conversations(action, user_key)
        if action.target_scope == TargetScope.CHANNEL_OPEN:
            return self._repository.list_conversations(
                channel=action.channel,
                status=TicketStatus.OPEN,
                limit=100,
            )
        if action.target_scope == TargetScope.ALL_OPEN:
            return self._repository.list_conversations(status=TicketStatus.OPEN, limit=100)
        return []

    def _last_listed_conversations(
        self,
        action: PlannedAction,
        user_key: str,
    ) -> list[Conversation]:
        context = self._repository.get_user_context(user_key)
        mapping = context.get("last_open_ticket_mapping", {})
        if not isinstance(mapping, dict):
            return []
        conversations: list[Conversation] = []
        for channel_value, indexed_ids in mapping.items():
            if action.channel and channel_value != action.channel.value:
                continue
            try:
                channel = ChannelName(channel_value)
            except ValueError:
                continue
            if not isinstance(indexed_ids, dict):
                continue
            for conversation_id in indexed_ids.values():
                conversation = self._repository.get_conversation(channel, str(conversation_id))
                if conversation:
                    conversations.append(conversation)
        return conversations

    async def _select_candidates(
        self,
        plan: ExecutionPlan,
        action: PlannedAction,
        candidates: list[Conversation],
    ) -> list[Conversation]:
        if not action.target_filter:
            return candidates
        try:
            result = await self._llm.complete_json(
                system_prompt=self._selection_system_prompt(),
                user_prompt=self._selection_user_prompt(plan, action, candidates),
                schema=CandidateSelectionResult,
            )
        except Exception:
            return self._fallback_select(action.target_filter, candidates)
        if not isinstance(result, CandidateSelectionResult):
            return self._fallback_select(action.target_filter, candidates)

        selected_keys = {
            (selection.channel, selection.conversation_id)
            for selection in result.selections
            if selection.selected
        }
        selected = [
            candidate
            for candidate in candidates
            if (candidate.channel, candidate.conversation_id) in selected_keys
        ]
        return selected or self._fallback_select(action.target_filter, candidates)

    @staticmethod
    def _selection_system_prompt() -> str:
        return (
            "You are a Korean shopping mall CS aggregation and judgement sub-agent. "
            "Select which candidate inquiries match the target_filter and should receive "
            "the planned action. Base the decision only on the candidate conversation "
            "messages and raw order fields. Return JSON only. Do not invent facts."
        )

    def _selection_user_prompt(
        self,
        plan: ExecutionPlan,
        action: PlannedAction,
        candidates: list[Conversation],
    ) -> str:
        return "\n".join(
            [
                f"사용자 목표: {plan.user_goal}",
                f"계획 요약: {plan.summary}",
                f"대상 조건: {action.target_filter}",
                f"답변 방향: {action.message or '없음'}",
                "",
                "후보 문의:",
                *[
                    self._candidate_text(index, candidate)
                    for index, candidate in enumerate(candidates, 1)
                ],
            ]
        )

    @staticmethod
    def _candidate_text(index: int, conversation: Conversation) -> str:
        messages = "\n".join(
            f"- {message.sender}: {message.text[:500]}"
            for message in conversation.messages[-5:]
            if message.text
        )
        raw_text = json.dumps(conversation.raw, ensure_ascii=False, default=str)[:1200]
        return "\n".join(
            [
                f"[{index}] channel={conversation.channel.value} id={conversation.conversation_id}",
                f"customer={conversation.customer_name or ''}",
                f"messages:\n{messages or '(저장된 메시지 없음)'}",
                f"raw:\n{raw_text}",
            ]
        )

    def _fallback_select(
        self,
        target_filter: str,
        candidates: list[Conversation],
    ) -> list[Conversation]:
        tokens = self._filter_tokens(target_filter)
        if not tokens:
            return candidates
        return [
            candidate
            for candidate in candidates
            if self._matches_tokens(self._conversation_search_text(candidate), tokens)
        ]

    @staticmethod
    def _filter_tokens(target_filter: str) -> list[str]:
        lowered = target_filter.lower()
        if "배송" in lowered and "지연" in lowered:
            return ["배송", "지연"]
        if "3mm" in lowered:
            return ["3mm", "s|l"]
        return [
            token
            for token in re.split(r"[\s,/.]+", lowered)
            if token and token not in {"또는", "혹은", "구매건", "문의", "문의건"}
        ]

    @staticmethod
    def _matches_tokens(text: str, tokens: list[str]) -> bool:
        lowered = text.lower()
        for token in tokens:
            if token == "s|l":
                if not re.search(r"(^|[^a-z])s([^a-z]|$)|(^|[^a-z])l([^a-z]|$)", lowered):
                    return False
                continue
            if token not in lowered:
                return False
        return True

    @staticmethod
    def _conversation_search_text(conversation: Conversation) -> str:
        payload: dict[str, Any] = {
            "customer_name": conversation.customer_name,
            "messages": [message.text for message in conversation.messages],
            "raw": conversation.raw,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _expand_action(
        action: PlannedAction,
        conversations: list[Conversation],
    ) -> list[PlannedAction]:
        return [
            action.model_copy(
                update={
                    "channel": conversation.channel,
                    "conversation_id": conversation.conversation_id,
                    "target_scope": TargetScope.EXPLICIT,
                    "prepared_api": None,
                }
            )
            for conversation in conversations
        ]

    @staticmethod
    def _missing_target_question(action: PlannedAction) -> str:
        if action.target_scope == TargetScope.LAST_LISTED:
            return "직전에 나열한 문의 목록이 없습니다. 먼저 조회할 문의건들을 보여달라고 요청해주세요."
        return "실행할 대상 문의를 찾지 못했습니다. 채널이나 문의 번호를 더 구체적으로 알려주세요."

    @staticmethod
    def _no_match_question(action: PlannedAction) -> str:
        condition = action.target_filter or "요청 조건"
        return f"{condition}에 맞는 문의를 찾지 못했습니다. 먼저 해당 문의 목록을 최신화해 보여드릴까요?"

    @staticmethod
    def _scope_label(scope: TargetScope) -> str:
        labels = {
            TargetScope.EXPLICIT: "지정 문의",
            TargetScope.LAST_LISTED: "직전 목록",
            TargetScope.CHANNEL_OPEN: "채널 미처리 문의",
            TargetScope.ALL_OPEN: "전체 미처리 문의",
        }
        return labels.get(scope, str(scope))

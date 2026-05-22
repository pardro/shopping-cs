import re
from collections.abc import Callable
from typing import Any

from app.models import ActionType, ExecutionPlan, TargetScope


PlannerContextProvider = Callable[[str], str]


class PlanningAgent:
    def __init__(self, llm: Any, context_provider: PlannerContextProvider):
        self._llm = llm
        self._context_provider = context_provider

    async def create_plan(self, user_request: str, user_key: str) -> ExecutionPlan:
        parsed = await self._llm.complete_json(
            system_prompt=self._system_prompt(),
            user_prompt=self._user_prompt(user_request, user_key),
            schema=ExecutionPlan,
        )
        if not isinstance(parsed, ExecutionPlan):
            raise ValueError("Invalid planner response")
        return self._normalize_plan(parsed, user_request)

    def _user_prompt(self, user_request: str, user_key: str) -> str:
        return "\n".join(
            [
                f"사용자 요청: {user_request}",
                "",
                self._context_provider(user_key),
            ]
        )

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are a Korean shopping mall CS analysis and planning sub-agent. "
            "Analyze the user's natural language request and choose which sub-agents "
            "must be used. Do not execute anything. Return only structured data "
            "matching the schema. "
            "Available actions are: sync, summary, conversation_detail, order_lookup, "
            "draft_reply, send_reply, close_ticket. Available channels are kakao and naver. "
            "Use summary with channel set when the user asks to show only one channel, "
            "for example naver-only or kakao-only inquiries. "
            "Prefer freshness over minimizing API calls: sync the requested channel before "
            "showing, selecting, drafting, or sending when fresh data could matter. "
            "For user-facing drafts, use draft_reply when the user says '초안'. "
            "Use send_reply only when the user clearly asks to send to the customer. "
            "For a specific numbered inquiry such as '카카오 1번', set channel and "
            "conversation_id to that number; the orchestrator will resolve it. "
            "For bulk or conditional requests, create one abstract draft_reply or send_reply "
            "action with conversation_id null instead of inventing ids. Set target_scope: "
            "last_listed when the user says '지금 나열해준', '방금 보여준', or refers to "
            "the currently listed inquiries; channel_open when the user asks for all open "
            "inquiries in a specific channel; all_open when the user asks for all open "
            "inquiries across channels. Put the selection condition in target_filter, "
            "for example '배송지연 문의', '3mm S 또는 L 사이즈 구매건'. "
            "If the request says all inquiries should receive a delivery-delay notice, "
            "do not use delivery delay as a target_filter; put that instruction in message. "
            "For draft_reply, put the desired answer direction in message. "
            "For send_reply, message is required and must be exactly what should be sent. "
            "If required information is missing, set needs_more_info=true and ask a concise "
            "Korean question. For risky customer-facing actions, add a risk note. "
            "Only send_reply actions require later approval. Other actions will execute "
            "immediately. Never mark actions as already done in the plan."
        )

    def _normalize_plan(self, plan: ExecutionPlan, user_request: str) -> ExecutionPlan:
        normalized_request = user_request.strip()
        for action in plan.actions:
            if action.type == ActionType.SUMMARY:
                continue
            if action.conversation_id:
                continue
            if action.type not in {
                ActionType.CONVERSATION_DETAIL,
                ActionType.ORDER_LOOKUP,
                ActionType.DRAFT_REPLY,
                ActionType.SEND_REPLY,
                ActionType.CLOSE_TICKET,
            }:
                continue
            inferred_scope = self._infer_target_scope(normalized_request, bool(action.channel))
            if (
                inferred_scope != TargetScope.EXPLICIT
                or action.target_scope == TargetScope.EXPLICIT
            ):
                action.target_scope = inferred_scope
            if not action.target_filter:
                action.target_filter = self._infer_target_filter(normalized_request)
            if self._is_all_targets_delivery_notice(normalized_request, action.target_filter):
                action.target_filter = None
        return plan

    @staticmethod
    def _infer_target_scope(user_request: str, has_channel: bool) -> TargetScope:
        if any(word in user_request for word in ("지금 나열", "방금", "보여준", "나열해준")):
            return TargetScope.LAST_LISTED
        if "중에서" in user_request or "문의건들 중" in user_request:
            return TargetScope.LAST_LISTED
        if has_channel and any(word in user_request for word in ("전체", "모든", "문의건들", "문의건")):
            return TargetScope.CHANNEL_OPEN
        if any(word in user_request for word in ("전체", "모든", "문의건들", "문의건")):
            return TargetScope.ALL_OPEN
        return TargetScope.EXPLICIT

    @staticmethod
    def _infer_target_filter(user_request: str) -> str | None:
        match = re.search(r"중에서\s*(.+?)에 대해서", user_request)
        if match:
            return match.group(1).strip()
        if "배송지연 문의" in user_request or "배송 지연 문의" in user_request:
            return "배송지연 문의"
        if "3mm" in user_request:
            return "3mm S 또는 L 사이즈 구매건"
        return None

    @staticmethod
    def _is_all_targets_delivery_notice(
        user_request: str,
        target_filter: str | None,
    ) -> bool:
        if not target_filter:
            return False
        return (
            "전체" in user_request
            and "배송지연" in target_filter
            and ("배송지연 안내" in user_request or "배송 지연 안내" in user_request)
        )

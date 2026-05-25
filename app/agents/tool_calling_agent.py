from collections.abc import Awaitable, Callable
from typing import Any

from app.llm import ChatGPTClient

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

CHANNEL_SCHEMA = {"type": "string", "enum": ["kakao", "naver", "all"]}

CS_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "sync_conversations",
            "description": "Kakao/Naver 문의 목록을 외부 API에서 동기화해 로컬 DB를 최신화합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        **CHANNEL_SCHEMA,
                        "description": "동기화할 채널. 전체 동기화는 all.",
                    }
                },
                "required": ["channel"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_open_tickets",
            "description": "로컬 DB의 미처리 문의 현황과 번호표를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        **CHANNEL_SCHEMA,
                        "description": "조회할 채널. 전체 조회는 all.",
                    }
                },
                "required": ["channel"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_conversations",
            "description": "직전 번호표, 특정 채널 미처리, 전체 미처리 범위에서 조건에 맞는 문의를 고릅니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["last_listed", "channel_open", "all_open"],
                        "description": "선택 범위.",
                    },
                    "channel": {
                        **CHANNEL_SCHEMA,
                        "description": "channel_open일 때 사용할 채널. 그 외에는 all 가능.",
                    },
                    "target_filter": {
                        "type": "string",
                        "description": "예: 배송지연 문의, 3mm S 또는 L 사이즈 구매건.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_conversation_detail",
            "description": "특정 문의의 전체 이전 대화 기록을 조회합니다. 번호표의 1번 같은 값도 사용 가능합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": ["kakao", "naver"]},
                    "conversation_id": {"type": "string"},
                },
                "required": ["channel", "conversation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_order",
            "description": "특정 문의와 연결된 주문 상세, 옵션, 수량, 주문번호, 상태를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": ["kakao", "naver"]},
                    "conversation_id": {"type": "string"},
                },
                "required": ["channel", "conversation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_reply",
            "description": "특정 문의의 전체 이전 대화 기록을 기반으로 고객 답변 초안을 생성합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": ["kakao", "naver"]},
                    "conversation_id": {"type": "string"},
                    "guidance": {
                        "type": "string",
                        "description": "운영자가 원하는 답변 방향 또는 포함할 문구.",
                    },
                },
                "required": ["channel", "conversation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prepare_send_replies",
            "description": "고객에게 보낼 메시지를 승인 대기 계획으로 저장합니다. 실제 전송은 승인 후에만 됩니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "replies": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "channel": {"type": "string", "enum": ["kakao", "naver"]},
                                "conversation_id": {"type": "string"},
                                "message": {"type": "string"},
                            },
                            "required": ["channel", "conversation_id", "message"],
                        },
                    },
                    "reason": {"type": "string"},
                    "risk_notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["replies", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_ticket",
            "description": "특정 문의의 상담 종료 처리를 수행합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": ["kakao", "naver"]},
                    "conversation_id": {"type": "string"},
                },
                "required": ["channel", "conversation_id"],
            },
        },
    },
]


class ToolCallingAgent:
    def __init__(self, llm: ChatGPTClient):
        self._llm = llm

    @property
    def tool_names(self) -> list[str]:
        return [tool["function"]["name"] for tool in CS_TOOL_SPECS]

    @staticmethod
    def tool_specification_text() -> str:
        lines = ["사용 가능한 툴 명세:"]
        for index, tool in enumerate(CS_TOOL_SPECS, start=1):
            function = tool["function"]
            required = function.get("parameters", {}).get("required", [])
            required_text = ", ".join(required) if required else "없음"
            lines.append(
                f"{index}. {function['name']} - {function['description']} "
                f"필수 인자: {required_text}"
            )
        return "\n".join(lines)

    async def handle(
        self,
        *,
        user_request: str,
        user_key: str,
        context_text: str,
        tool_executor: ToolExecutor,
    ) -> tuple[str, list[dict[str, Any]]]:
        return await self._llm.complete_with_tools(
            system_prompt=self._system_prompt(),
            user_prompt=self._user_prompt(user_request, user_key, context_text),
            tools=CS_TOOL_SPECS,
            tool_executor=tool_executor,
        )

    def _user_prompt(self, user_request: str, user_key: str, context_text: str) -> str:
        return "\n".join(
            [
                f"사용자 식별자: {user_key}",
                f"사용자 요청: {user_request}",
                "",
                self.tool_specification_text(),
                "",
                context_text,
            ]
        )

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are a Korean shopping mall CS tool-calling agent. "
            "Use the provided tools actively whenever current CS data, conversation history, "
            "order information, draft generation, approval preparation, or status changes are "
            "needed. Do not invent channel data. If freshness matters, call sync_conversations "
            "before summarizing, selecting, drafting, ordering, or sending. For numbered Korean "
            "references like '네이버 1번', pass conversation_id as '1' and let the tool resolve it. "
            "For bulk or conditional requests, call select_conversations first, then call the "
            "needed tool once per selected inquiry. Customer-facing send requests must call "
            "prepare_send_replies only; never claim a message was sent until the user later "
            "approves. 조회, 동기화, 주문조회, 초안, 상담 종료는 즉시 실행 가능한 작업입니다. "
            "Final answers must be concise Korean, mobile-readable, and based only on "
            "tool results. "
            "When a send plan is prepared, include that approval is required and tell the user to "
            "reply '승인' or '취소'."
        )

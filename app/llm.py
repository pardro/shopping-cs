import json
from collections.abc import Awaitable, Callable
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings


class ChatGPTClient:
    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self._settings = settings
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        response = await self._client.chat.completions.create(
            model=self._settings.openai_model,
            temperature=self._settings.openai_temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or ""

    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        schema_prompt = (
            f"{system_prompt}\n\n"
            "Return a single JSON object that validates against this JSON schema:\n"
            f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
        )
        response = await self._client.chat.completions.create(
            model=self._settings.openai_model,
            temperature=self._settings.openai_temperature,
            messages=[
                {"role": "system", "content": schema_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("LLM did not return structured output")
        return schema.model_validate_json(content)

    async def complete_with_tools(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        max_tool_rounds: int = 10,
    ) -> tuple[str, list[dict[str, Any]]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        trace: list[dict[str, Any]] = []

        for _ in range(max_tool_rounds):
            response = await self._client.chat.completions.create(
                model=self._settings.openai_model,
                temperature=self._settings.openai_temperature,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls or []
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": message.content,
            }
            if tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                    for tool_call in tool_calls
                ]
            messages.append(assistant_message)
            if not tool_calls:
                return message.content or "", trace

            for tool_call in tool_calls:
                name = tool_call.function.name
                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                result = await tool_executor(name, arguments)
                trace.append({"tool": name, "arguments": arguments, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

        return (
            "요청을 처리하는 중 필요한 도구 호출 횟수가 너무 많아 중단했습니다. "
            "요청 범위를 조금 더 좁혀 다시 말씀해주세요.",
            trace,
        )

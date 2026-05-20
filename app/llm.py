import json

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

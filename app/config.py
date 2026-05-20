from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    openai_temperature: float = Field(default=0.2, alias="OPENAI_TEMPERATURE")

    app_env: Literal["local", "dev", "stage", "prod"] = Field(default="local", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    app_log_level: str = Field(default="INFO", alias="APP_LOG_LEVEL")
    sqlite_path: str = Field(default="./shopping_cs.sqlite3", alias="SQLITE_PATH")

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_chat_ids: str = Field(default="", alias="TELEGRAM_ALLOWED_CHAT_IDS")
    telegram_poll_timeout_seconds: int = Field(default=30, alias="TELEGRAM_POLL_TIMEOUT_SECONDS")

    kakao_api_base_url: str = Field(default="", alias="KAKAO_API_BASE_URL")
    kakao_rest_api_key: str = Field(default="", alias="KAKAO_REST_API_KEY")
    kakao_channel_id: str = Field(default="", alias="KAKAO_CHANNEL_ID")
    kakao_list_conversations_path: str = Field(
        default="/v1/channels/{channel_id}/conversations",
        alias="KAKAO_LIST_CONVERSATIONS_PATH",
    )
    kakao_send_message_path: str = Field(
        default="/v1/channels/{channel_id}/conversations/{conversation_id}/messages",
        alias="KAKAO_SEND_MESSAGE_PATH",
    )
    kakao_update_status_path: str = Field(
        default="/v1/channels/{channel_id}/conversations/{conversation_id}/status",
        alias="KAKAO_UPDATE_STATUS_PATH",
    )

    naver_talktalk_api_base_url: str = Field(default="", alias="NAVER_TALKTALK_API_BASE_URL")
    naver_client_id: str = Field(default="", alias="NAVER_CLIENT_ID")
    naver_client_secret: str = Field(default="", alias="NAVER_CLIENT_SECRET")
    naver_talktalk_channel_id: str = Field(default="", alias="NAVER_TALKTALK_CHANNEL_ID")
    naver_list_conversations_path: str = Field(
        default="/v1/channels/{channel_id}/conversations",
        alias="NAVER_LIST_CONVERSATIONS_PATH",
    )
    naver_send_message_path: str = Field(
        default="/v1/channels/{channel_id}/conversations/{conversation_id}/messages",
        alias="NAVER_SEND_MESSAGE_PATH",
    )
    naver_update_status_path: str = Field(
        default="/v1/channels/{channel_id}/conversations/{conversation_id}/status",
        alias="NAVER_UPDATE_STATUS_PATH",
    )

    @computed_field
    @property
    def allowed_telegram_chat_ids(self) -> set[int]:
        if not self.telegram_allowed_chat_ids.strip():
            return set()
        return {
            int(chat_id.strip())
            for chat_id in self.telegram_allowed_chat_ids.split(",")
            if chat_id.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

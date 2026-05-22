import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, log_dir: str):
        self._log_dir = Path(log_dir)

    def write_turn(
        self,
        *,
        user_key: str,
        user_text: str,
        chatbot_text: str,
        actions: list[dict[str, Any]],
        user_timestamp: datetime,
        chatbot_timestamp: datetime | None = None,
    ) -> None:
        now = chatbot_timestamp or datetime.now().astimezone()
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_path(now, user_key)
        payload = self._load_payload(log_path, user_key, now)
        payload["conversations"].append(
            {
                "user": {
                    "text": user_text,
                    "timestamp": self._format_timestamp(user_timestamp),
                },
                "chatbot": {
                    "text": chatbot_text,
                    "timestamp": self._format_timestamp(now),
                    "action": actions,
                },
            }
        )
        self._write_json(log_path, payload)

    def _load_payload(self, log_path: Path, user_key: str, now: datetime) -> dict[str, Any]:
        if log_path.exists():
            try:
                with log_path.open("r", encoding="utf-8") as log_file:
                    payload = json.load(log_file)
                if isinstance(payload, dict):
                    meta_data = payload.setdefault("meta_data", {})
                    meta_data.setdefault("user_key", user_key)
                    meta_data.setdefault("logging_start_time", self._format_timestamp(now))
                    meta_data.setdefault("log_date", f"{now:%Y-%m-%d}")
                    if not isinstance(payload.get("conversations"), list):
                        payload["conversations"] = []
                    return payload
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "meta_data": {
                "user_key": user_key,
                "logging_start_time": self._format_timestamp(now),
                "log_date": f"{now:%Y-%m-%d}",
            },
            "conversations": [],
        }

    def _write_json(self, log_path: Path, payload: dict[str, Any]) -> None:
        jsonable_payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        temp_path = log_path.with_suffix(log_path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as log_file:
            json.dump(jsonable_payload, log_file, ensure_ascii=False, indent=2)
            log_file.write("\n")
        temp_path.replace(log_path)

    def _log_path(self, now: datetime, user_key: str) -> Path:
        return self._log_dir / f"{now:%Y-%m-%d}_{self._safe_user_key(user_key)}.json"

    @staticmethod
    def _safe_user_key(user_key: str) -> str:
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", user_key).strip("_")
        return safe_key[:80] or "unknown"

    @staticmethod
    def _format_timestamp(timestamp: datetime) -> str:
        return timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, log_dir: str):
        self._log_dir = Path(log_dir)

    def write(self, event_type: str, payload: dict[str, Any]) -> None:
        now = datetime.now().astimezone()
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / f"{now:%Y-%m-%d}.jsonl"
        record = {
            "timestamp": now.isoformat(),
            "event_type": event_type,
            **payload,
        }
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

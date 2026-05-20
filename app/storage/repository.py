import json
import sqlite3
from pathlib import Path

from app.models import (
    ChannelName,
    ChannelSummary,
    Conversation,
    ConversationMessage,
    ExecutionPlan,
    TicketStatus,
)


class CsRepository:
    def __init__(self, sqlite_path: str):
        self._path = Path(sqlite_path)

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    customer_name TEXT,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (channel, conversation_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbound_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_plans (
                    user_key TEXT PRIMARY KEY,
                    plan_payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_contexts (
                    user_key TEXT PRIMARY KEY,
                    context_payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def upsert_conversation(self, conversation: Conversation) -> None:
        payload = conversation.model_dump_json()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations
                    (channel, conversation_id, customer_name, status, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(channel, conversation_id) DO UPDATE SET
                    customer_name = excluded.customer_name,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    conversation.channel.value,
                    conversation.conversation_id,
                    conversation.customer_name,
                    conversation.status.value,
                    payload,
                ),
            )

    def get_conversation(
        self,
        channel: ChannelName,
        conversation_id: str,
    ) -> Conversation | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload FROM conversations
                WHERE channel = ? AND conversation_id = ?
                """,
                (channel.value, conversation_id),
            ).fetchone()
        if not row:
            return None
        return Conversation.model_validate_json(row["payload"])

    def list_conversations(
        self,
        channel: ChannelName | None = None,
        status: TicketStatus | None = None,
        limit: int = 50,
    ) -> list[Conversation]:
        where: list[str] = []
        params: list[str | int] = []
        if channel:
            where.append("channel = ?")
            params.append(channel.value)
        if status:
            where.append("status = ?")
            params.append(status.value)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT payload FROM conversations
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [Conversation.model_validate_json(row["payload"]) for row in rows]

    def summarize(self) -> list[ChannelSummary]:
        summaries: list[ChannelSummary] = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT channel, status, COUNT(*) AS count
                FROM conversations
                GROUP BY channel, status
                """
            ).fetchall()
        grouped: dict[str, dict[str, int]] = {}
        for row in rows:
            grouped.setdefault(row["channel"], {})[row["status"]] = row["count"]
        for channel in ChannelName:
            counts = grouped.get(channel.value, {})
            summaries.append(
                ChannelSummary(
                    channel=channel,
                    open_count=counts.get(TicketStatus.OPEN.value, 0),
                    pending_count=counts.get(TicketStatus.PENDING.value, 0),
                    closed_count=counts.get(TicketStatus.CLOSED.value, 0),
                )
            )
        return summaries

    def record_outbound_message(self, channel: ChannelName, conversation_id: str, text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO outbound_messages (channel, conversation_id, text)
                VALUES (?, ?, ?)
                """,
                (channel.value, conversation_id, text),
            )

    def save_pending_plan(self, user_key: str, plan: ExecutionPlan) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_plans (user_key, plan_payload, created_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_key) DO UPDATE SET
                    plan_payload = excluded.plan_payload,
                    created_at = CURRENT_TIMESTAMP
                """,
                (user_key, plan.model_dump_json()),
            )

    def get_pending_plan(self, user_key: str) -> ExecutionPlan | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT plan_payload FROM pending_plans
                WHERE user_key = ?
                """,
                (user_key,),
            ).fetchone()
        if not row:
            return None
        return ExecutionPlan.model_validate_json(row["plan_payload"])

    def clear_pending_plan(self, user_key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_plans WHERE user_key = ?", (user_key,))

    def save_user_context(self, user_key: str, context: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_contexts (user_key, context_payload, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_key) DO UPDATE SET
                    context_payload = excluded.context_payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_key, json.dumps(context, ensure_ascii=False)),
            )

    def get_user_context(self, user_key: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT context_payload FROM user_contexts
                WHERE user_key = ?
                """,
                (user_key,),
            ).fetchone()
        if not row:
            return {}
        return json.loads(row["context_payload"])

    def ingest_webhook(self, channel: ChannelName, payload: dict) -> Conversation:
        conversation_id = str(
            payload.get("conversation_id")
            or payload.get("conversationId")
            or payload.get("id")
            or payload.get("room_id")
            or payload.get("roomId")
        )
        if not conversation_id or conversation_id == "None":
            raise ValueError("Webhook payload does not include a conversation id")
        existing = self.get_conversation(channel, conversation_id)
        merged_payload = existing.raw if existing else {}
        merged_payload.update(payload)
        messages = existing.messages if existing else []
        message_text = payload.get("text") or payload.get("content") or payload.get("message")
        if message_text:
            message_id = str(
                payload.get("message_id")
                or payload.get("messageId")
                or payload.get("event_id")
                or f"{conversation_id}-{len(messages) + 1}"
            )
            if all(message.message_id != message_id for message in messages):
                messages.append(
                    ConversationMessage(
                        message_id=message_id,
                        sender=str(payload.get("sender") or payload.get("from") or "customer"),
                        text=str(message_text),
                        raw=payload,
                    )
                )
        conversation = Conversation(
            channel=channel,
            conversation_id=conversation_id,
            customer_name=payload.get("customer_name") or payload.get("customerName"),
            status=TicketStatus.OPEN,
            messages=messages,
            raw=merged_payload,
        )
        self.upsert_conversation(conversation)
        return conversation

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

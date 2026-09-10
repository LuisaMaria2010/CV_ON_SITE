"""Persistenza conversazioni ai_matcher su Azure Table storage.

Estratto da function_app.py senza modifiche di comportamento. Unico consumer:
la route `POST /api/ai-matcher-wrapper`.

Layout tabella (PartitionKey = "<user_id>|<chat_id>"):
- RowKey "thread": stato della conversazione (turn_count, last_response_id, ...)
- RowKey "e_<timestamp>_<rand>": un'entita' per scambio richiesta/risposta
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from core.config import settings
from core.errors import InvalidInputError
from utils.app_settings import settings_value
from utils.values import safe_str

try:
    from azure.data.tables import TableServiceClient
except ImportError:  # pragma: no cover - dipende dall'ambiente di deploy
    TableServiceClient = None

logger = logging.getLogger(__name__)

_chat_history_table_client: Any = None


def chat_history_table_name() -> str:
    return settings_value(
        "CHAT_HISTORY_TABLE_NAME",
        "AI_MATCHER_HISTORY_TABLE_NAME",
        default="AiMatcherChatHistory",
    )


def chat_history_connection_string() -> str:
    return settings_value(
        "AzureWebJobsStorage",
        "STORAGE_ACCOUNT_CONNECTION_STRING",
        "STORAGE_CONNECTION_STRING",
        default=settings.storage_connection_string or "",
    )


def sanitize_table_key_part(value: Any, fallback: str) -> str:
    raw = safe_str(value) or fallback
    sanitized = re.sub(r"[\\/#?\x00-\x1F\x7F]", "_", raw)
    return sanitized[:256] or fallback


def chat_partition_key(user_id: str, chat_id: str) -> str:
    return f"{sanitize_table_key_part(user_id, 'user')}|{sanitize_table_key_part(chat_id, 'chat')}"


def chat_exchange_row_key() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"e_{ts}_{uuid4().hex[:8]}"


def truncate_text(value: str, max_len: int = 30000) -> str:
    if len(value) <= max_len:
        return value
    return value[:max_len]


def get_chat_history_table_client() -> Any:
    global _chat_history_table_client
    if _chat_history_table_client is not None:
        return _chat_history_table_client

    if TableServiceClient is None:
        raise InvalidInputError(
            "Chat history table support unavailable: install azure-data-tables"
        )

    connection_string = chat_history_connection_string()
    if not connection_string:
        raise InvalidInputError("Missing Azure storage connection string for chat history")

    table_name = chat_history_table_name()
    service = TableServiceClient.from_connection_string(conn_str=connection_string)
    service.create_table_if_not_exists(table_name=table_name)
    _chat_history_table_client = service.get_table_client(table_name=table_name)
    return _chat_history_table_client


def chat_history_load_thread(user_id: str, chat_id: str) -> dict[str, Any] | None:
    client = get_chat_history_table_client()
    pk = chat_partition_key(user_id, chat_id)
    try:
        entity = client.get_entity(partition_key=pk, row_key="thread")
        return dict(entity)
    except Exception:
        return None


def chat_history_append_turn(
    *,
    user_id: str,
    chat_id: str,
    user_request: str,
    assistant_response: str,
    previous_response_id: str | None,
    response_id: str | None,
    agent_name: str,
    model_name: str | None,
) -> dict[str, Any]:
    client = get_chat_history_table_client()
    now = datetime.now(timezone.utc).isoformat()
    pk = chat_partition_key(user_id, chat_id)

    turn_count = 0
    existing_thread = chat_history_load_thread(user_id, chat_id)
    if existing_thread:
        try:
            turn_count = int(existing_thread.get("turn_count") or 0)
        except Exception:
            turn_count = 0

    thread_entity = {
        "PartitionKey": pk,
        "RowKey": "thread",
        "entity_type": "thread",
        "user_id": user_id,
        "chat_id": chat_id,
        "turn_count": turn_count + 1,
        "last_response_id": safe_str(response_id) or safe_str(previous_response_id) or None,
        "agent_name": agent_name,
        "model_name": model_name,
        "updated_at": now,
    }
    if not existing_thread:
        thread_entity["created_at"] = now

    exchange_entity = {
        "PartitionKey": pk,
        "RowKey": chat_exchange_row_key(),
        "entity_type": "exchange",
        "user_id": user_id,
        "chat_id": chat_id,
        "turn_number": turn_count + 1,
        "request_text": truncate_text(safe_str(user_request)),
        "response_text": truncate_text(safe_str(assistant_response)),
        "previous_response_id": safe_str(previous_response_id) or None,
        "foundry_response_id": safe_str(response_id) or None,
        "agent_name": agent_name,
        "model_name": model_name,
        "created_at": now,
    }

    client.upsert_entity(entity=thread_entity, mode="merge")
    client.upsert_entity(entity=exchange_entity, mode="merge")

    return {
        "partition_key": pk,
        "turn_count": turn_count + 1,
        "persisted": True,
    }

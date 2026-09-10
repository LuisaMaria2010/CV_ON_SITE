"""Proxy verso il chatbot Foundry ai_matcher: POST /api/ai-matcher-wrapper.

Estratto da function_app.py senza modifiche ai corpi delle funzioni.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import azure.functions as func

from core.errors import InvalidInputError
from infra.chat_history import (
    chat_history_append_turn as _chat_history_append_turn,
    chat_history_load_thread as _chat_history_load_thread,
)
from infra.foundry_agent import (
    response_to_plain_dict as _response_to_plain_dict,
    run_foundry_agent as _run_foundry_agent,
    split_ai_matcher_answer as _split_ai_matcher_answer,
)
from utils.app_settings import settings_value as _settings_value
from utils.http_errors import http_error_handler
from utils.http_params import body_params as _body_params, parse_bool as _parse_bool, payload_from_query as _payload_from_query
from utils.values import extract_json_safe as _extract_json_safe, first_non_empty as _first_non_empty, safe_str as _safe_str

logger = logging.getLogger(__name__)

bp = func.Blueprint()

@bp.route(route="ai-matcher-wrapper", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@http_error_handler
async def ai_matcher_wrapper(req: func.HttpRequest):
    """
    POST /api/ai-matcher-wrapper

    Wrapper riusabile per chiamare il chatbot Foundry `ai_matcher` da sistemi esterni.
    Pensato per essere consumato come tool OpenAPI da altri agenti.
    """
    payload = _body_params(req)
    if not payload:
        payload = _payload_from_query(req)
    if not payload:
        raise InvalidInputError("Missing or invalid JSON body")

    include_raw_response = _parse_bool(
        _first_non_empty(payload.get("include_raw_response"), payload.get("return_raw_response")),
        default=False,
    )
    include_result = _parse_bool(
        _first_non_empty(payload.get("include_result"), payload.get("return_result")),
        default=False,
    )

    user_id = _safe_str(_first_non_empty(payload.get("user_id"), payload.get("userId")))
    chat_id = _safe_str(
        _first_non_empty(
            payload.get("chat_id"),
            payload.get("chatId"),
            payload.get("conversation_id"),
            payload.get("conversationId"),
        )
    )
    if not user_id:
        raise InvalidInputError("'user_id' is required")
    if not chat_id:
        raise InvalidInputError("'chat_id' is required")

    user_request = _safe_str(payload.get("user_request"))
    if not user_request:
        user_request = _safe_str(_first_non_empty(payload.get("query"), payload.get("original_request")))
    if not user_request:
        raise InvalidInputError("'user_request' is required")

    context = payload.get("context")
    if context is None and isinstance(payload.get("search_request"), dict):
        context = {"search_request": payload.get("search_request")}
    if context is not None and not isinstance(context, dict):
        raise InvalidInputError("'context' must be an object when provided")

    session_context = {
        "user_id": user_id,
        "chat_id": chat_id,
    }
    if context:
        context = {
            **context,
            "session": {
                **session_context,
                **(context.get("session") if isinstance(context.get("session"), dict) else {}),
            },
        }
    else:
        context = {"session": session_context}

    model_name = _safe_str(payload.get("model")) or _settings_value(
        "FOUNDRY_MODEL",
        default="",
    )
    agent_name = _safe_str(payload.get("agent_name")) or _settings_value(
        "AI_MATCHER_AGENT_NAME",
        default="mc_matcher",
    )

    agent_input = user_request
    if context:
        agent_input = f"{user_request}\n\nCONTEXT_JSON:\n{json.dumps(context, ensure_ascii=False)}"

    previous_response_id = _safe_str(payload.get("previous_response_id")) or None
    history_status: dict[str, Any] = {
        "enabled": False,
        "persisted": False,
        "continued": False,
        "source_previous_response_id": "request" if previous_response_id else None,
    }

    if not previous_response_id:
        try:
            thread = await asyncio.to_thread(_chat_history_load_thread, user_id, chat_id)
            stored_previous_response_id = _safe_str((thread or {}).get("last_response_id"))
            if stored_previous_response_id:
                previous_response_id = stored_previous_response_id
                history_status["source_previous_response_id"] = "storage"
                history_status["continued"] = True
            history_status["enabled"] = True
        except Exception as exc:
            history_status["error"] = str(exc)
            logger.warning(
                "chat_history_load_failed user_id=%s chat_id=%s error=%s",
                user_id,
                chat_id,
                exc,
            )

    response = await asyncio.to_thread(
        lambda: _run_foundry_agent(
            agent_name=agent_name,
            message=agent_input,
            model_name=model_name,
            previous_response_id=previous_response_id,
        )
    )

    raw_response = _response_to_plain_dict(response)
    output_text = _safe_str(getattr(response, "output_text", ""))

    # The agent appends a machine-readable trailer after its prose answer:
    #   <<<CANDIDATES_JSON>>>
    #   {"candidates":[{"id_mcflash":..,"trigramma":..}, ...]}
    # Keep `answer` pure natural language and surface the trailer as `candidates`.
    answer_body, proposed_candidates = _split_ai_matcher_answer(output_text)
    effective_text = answer_body or output_text
    parsed_output = _extract_json_safe(effective_text) if effective_text else None

    result_payload: dict[str, Any]
    if isinstance(parsed_output, dict):
        result_payload = parsed_output
    else:
        result_payload = {"raw_text": effective_text}

    response_id = _safe_str(raw_response.get("id")) or None
    assistant_response_text = effective_text if effective_text else json.dumps(result_payload, ensure_ascii=False)

    answer_text = _safe_str(effective_text)
    if not answer_text and isinstance(result_payload, dict):
        answer_text = _safe_str(
            _first_non_empty(
                result_payload.get("answer"),
                result_payload.get("final_answer"),
                result_payload.get("response"),
                result_payload.get("raw_text"),
            )
        )
    if not answer_text:
        answer_text = _truncate_text(json.dumps(result_payload, ensure_ascii=False), max_len=4000)

    try:
        persist_meta = await asyncio.to_thread(
            _chat_history_append_turn,
            user_id=user_id,
            chat_id=chat_id,
            user_request=user_request,
            assistant_response=assistant_response_text,
            previous_response_id=previous_response_id,
            response_id=response_id,
            agent_name=agent_name,
            model_name=model_name or None,
        )
        history_status["enabled"] = True
        history_status["persisted"] = True
        history_status["turn_count"] = persist_meta.get("turn_count")
    except Exception as exc:
        history_status["enabled"] = True
        history_status["persisted"] = False
        history_status["error"] = str(exc)
        logger.warning(
            "chat_history_persist_failed user_id=%s chat_id=%s error=%s",
            user_id,
            chat_id,
            exc,
        )

    response_payload = {
        "agent": {
            "name": agent_name,
            "model": model_name or None,
            "response_id": response_id,
            "previous_response_id": previous_response_id,
        },
        "session": {
            "user_id": user_id,
            "chat_id": chat_id,
        },
        "conversation": history_status,
        "answer": answer_text,
        "candidates": proposed_candidates,
    }

    if include_result:
        response_payload["result"] = result_payload
    if include_raw_response:
        response_payload["raw_response"] = raw_response

    return response_payload

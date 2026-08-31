"""Translate between the OpenAI/Azure request-response shape (used internally
and by every governance check, audit row, and dashboard) and the native
request/response shape of non-OpenAI-compatible providers (Anthropic, Google).

Clients always send OpenAI-shaped bodies on /proxy. For non-streaming calls
the raw provider response is returned to the caller exactly as the upstream
provider sent it (see app/routers/proxy.py). Internal accounting (tokens,
cost, finish_reason, audit) always goes through extract_usage() below so it
stays provider-agnostic regardless of what the client receives.

Covers: plain text messages, multimodal content (images), tool/function
calling, and best-effort JSON-mode / structured-output translation.
Streaming is translated separately in stream_translation.py.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from app.services.ai_model_pricing import normalize_provider

_NATIVE_PROVIDERS = {"azure_openai", "azure", "openai", "", "generic"}
_DATA_URI_RE = re.compile(r"^data:([^;]+);base64,(.+)$", re.DOTALL)

# OpenAI/Azure "reasoning" models (o1/o3/o4, gpt-5 family) reject the
# standard chat-completions sampling params: max_tokens must be sent as
# max_completion_tokens, and temperature/top_p/penalties only accept their
# default value — anything else 400s.
_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def needs_translation(provider: str) -> bool:
    return normalize_provider(provider) not in _NATIVE_PROVIDERS


def _is_reasoning_model(model: Optional[str]) -> bool:
    return (model or "").lower().startswith(_REASONING_MODEL_PREFIXES)


def _normalize_reasoning_params(body: dict) -> dict:
    """Reasoning models spend one shared token budget on hidden reasoning
    *and* visible content. A caller-supplied cap sized for the visible
    answer alone can be fully consumed by reasoning before any content is
    written — empty response, finish_reason='length' — and larger inputs
    need more reasoning tokens to process, so this gets worse as input size
    grows. Drop any caller-supplied cap entirely rather than raising it to a
    fixed replacement, so reasoning always has room to finish regardless of
    input size; the model is still bounded by its own context window.
    """
    out = dict(body)
    out.pop("max_tokens", None)
    out.pop("max_completion_tokens", None)
    if out.get("temperature", 1) != 1:
        out.pop("temperature", None)
    if out.get("top_p", 1) != 1:
        out.pop("top_p", None)
    if out.get("presence_penalty", 0) != 0:
        out.pop("presence_penalty", None)
    if out.get("frequency_penalty", 0) != 0:
        out.pop("frequency_penalty", None)
    return out


def _parse_data_uri(url: str) -> Optional[tuple[str, str]]:
    m = _DATA_URI_RE.match(url or "")
    if not m:
        return None
    return m.group(1), m.group(2)


# ---------------------------------------------------------------------------
# Outbound request translation
# ---------------------------------------------------------------------------

def translate_outbound_body(provider: str, forward_body: dict) -> dict:
    """Convert an OpenAI-shaped forward_body into the upstream provider's native request body."""
    prov = normalize_provider(provider)
    if prov == "anthropic":
        return _to_anthropic_body(forward_body)
    if prov == "google":
        return _to_google_body(forward_body)
    if _is_reasoning_model(forward_body.get("model")):
        return _normalize_reasoning_params(forward_body)
    return forward_body


def _to_anthropic_body(body: dict) -> dict:
    messages = body.get("messages", [])
    system_parts = [m.get("content", "") for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str)]

    tool_call_names: dict[str, str] = {}
    for m in messages:
        for tc in (m.get("tool_calls") or []):
            tool_call_names[tc.get("id")] = (tc.get("function") or {}).get("name", "")

    chat_messages: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            chat_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id"),
                    "content": m.get("content") or "",
                }],
            })
        elif role == "assistant" and m.get("tool_calls"):
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                blocks.append({
                    "type": "tool_use", "id": tc.get("id"),
                    "name": fn.get("name"), "input": args,
                })
            chat_messages.append({"role": "assistant", "content": blocks})
        elif role in ("user", "assistant"):
            chat_messages.append({"role": role, "content": _content_to_anthropic_blocks(m.get("content"))})

    out: dict = {
        "model": body.get("model"),
        "messages": chat_messages,
        "max_tokens": body.get("max_tokens") or body.get("max_completion_tokens") or 1024,
    }
    if system_parts:
        out["system"] = "\n".join(p for p in system_parts if p)
    if "temperature" in body:
        out["temperature"] = body["temperature"]
    if "top_p" in body:
        out["top_p"] = body["top_p"]
    if "stop" in body:
        stop = body["stop"]
        out["stop_sequences"] = stop if isinstance(stop, list) else [stop]

    if body.get("tools"):
        out["tools"] = [
            {
                "name": (t.get("function") or {}).get("name"),
                "description": (t.get("function") or {}).get("description", ""),
                "input_schema": (t.get("function") or {}).get("parameters", {"type": "object", "properties": {}}),
            }
            for t in body["tools"] if t.get("type") == "function"
        ]
    tool_choice = body.get("tool_choice")
    if tool_choice == "auto":
        out["tool_choice"] = {"type": "auto"}
    elif tool_choice == "required":
        out["tool_choice"] = {"type": "any"}
    elif isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            out["tool_choice"] = {"type": "tool", "name": name}

    _apply_response_format_anthropic(out, body)
    return out


def _content_to_anthropic_blocks(content: Any) -> Any:
    if isinstance(content, str) or content is None:
        return content or ""
    blocks = []
    for part in content:
        ptype = part.get("type")
        if ptype == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            parsed = _parse_data_uri(url)
            if parsed:
                media_type, data = parsed
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
            else:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks


def _apply_response_format_anthropic(out: dict, body: dict) -> None:
    rf = body.get("response_format")
    if not rf or rf.get("type") not in ("json_object", "json_schema"):
        return
    instruction = "Respond with valid JSON only, no surrounding text or markdown."
    if rf.get("type") == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema")
        if schema:
            instruction += f" The JSON must conform to this schema: {json.dumps(schema)}"
    out["system"] = (out.get("system") + "\n" + instruction) if out.get("system") else instruction


def _to_google_body(body: dict) -> dict:
    messages = body.get("messages", [])
    system_parts = [m.get("content", "") for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str)]

    tool_call_names: dict[str, str] = {}
    for m in messages:
        for tc in (m.get("tool_calls") or []):
            tool_call_names[tc.get("id")] = (tc.get("function") or {}).get("name", "")

    contents: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            name = tool_call_names.get(m.get("tool_call_id"), "")
            try:
                response_payload = json.loads(m.get("content") or "{}")
                if not isinstance(response_payload, dict):
                    response_payload = {"result": response_payload}
            except (json.JSONDecodeError, TypeError):
                response_payload = {"result": m.get("content")}
            contents.append({"role": "function", "parts": [{"functionResponse": {"name": name, "response": response_payload}}]})
        elif role == "assistant" and m.get("tool_calls"):
            parts = []
            if m.get("content"):
                parts.append({"text": m["content"]})
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                parts.append({"functionCall": {"name": fn.get("name"), "args": args}})
            contents.append({"role": "model", "parts": parts})
        elif role in ("user", "assistant"):
            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": _content_to_google_parts(m.get("content")),
            })

    out: dict = {"contents": contents}
    if system_parts:
        out["systemInstruction"] = {"parts": [{"text": "\n".join(p for p in system_parts if p)}]}

    gen_config: dict = {}
    if "temperature" in body:
        gen_config["temperature"] = body["temperature"]
    if "top_p" in body:
        gen_config["topP"] = body["top_p"]
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
    if max_tokens:
        gen_config["maxOutputTokens"] = max_tokens
    _apply_response_format_google(gen_config, body)
    if gen_config:
        out["generationConfig"] = gen_config

    if body.get("tools"):
        fn_decls = [
            {
                "name": (t.get("function") or {}).get("name"),
                "description": (t.get("function") or {}).get("description", ""),
                "parameters": (t.get("function") or {}).get("parameters", {"type": "object", "properties": {}}),
            }
            for t in body["tools"] if t.get("type") == "function"
        ]
        if fn_decls:
            out["tools"] = [{"functionDeclarations": fn_decls}]

    tool_choice = body.get("tool_choice")
    mode = None
    if tool_choice == "auto":
        mode = "AUTO"
    elif tool_choice == "required":
        mode = "ANY"
    elif tool_choice == "none":
        mode = "NONE"
    elif isinstance(tool_choice, dict):
        mode = "ANY"
    if mode:
        out["tool_config"] = {"function_calling_config": {"mode": mode}}

    return out


def _content_to_google_parts(content: Any) -> list[dict]:
    if isinstance(content, str) or content is None:
        return [{"text": content or ""}]
    parts = []
    for part in content:
        ptype = part.get("type")
        if ptype == "text":
            parts.append({"text": part.get("text", "")})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            parsed = _parse_data_uri(url)
            if parsed:
                media_type, data = parsed
                parts.append({"inline_data": {"mime_type": media_type, "data": data}})
            else:
                # Best-effort: arbitrary remote URLs aren't fetchable inline by Gemini;
                # only Files API URIs (already uploaded to Google) work here reliably.
                parts.append({"file_data": {"mime_type": "application/octet-stream", "file_uri": url}})
    return parts


def _apply_response_format_google(gen_config: dict, body: dict) -> None:
    rf = body.get("response_format")
    if not rf or rf.get("type") not in ("json_object", "json_schema"):
        return
    gen_config["responseMimeType"] = "application/json"
    if rf.get("type") == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema")
        if schema:
            gen_config["responseSchema"] = schema


# ---------------------------------------------------------------------------
# Usage / accounting extraction (provider-native response -> internal fields)
# ---------------------------------------------------------------------------

@dataclass
class ProviderUsage:
    input_tokens: int
    output_tokens: int
    finish_reason: Optional[str]
    input_tokens_known: bool
    output_tokens_known: bool
    output_text: str  # used for tiktoken fallback estimate when output_tokens_known is False


def extract_usage(provider: str, response_data: dict) -> ProviderUsage:
    """Pull tokens/finish_reason out of a provider-native response for internal accounting.

    The raw response_data is returned to the caller untouched elsewhere;
    this only feeds budgets, cost rows, and audit logs.
    """
    prov = normalize_provider(provider)
    if prov == "anthropic":
        return _usage_from_anthropic(response_data)
    if prov == "google":
        return _usage_from_google(response_data)
    return _usage_from_openai(response_data)


def _usage_from_openai(response_data: dict) -> ProviderUsage:
    usage = response_data.get("usage", {}) or {}
    prompt = int(usage.get("prompt_tokens", 0))
    completion = int(usage.get("completion_tokens", 0))
    choices = response_data.get("choices", []) or []
    finish_reason = choices[0].get("finish_reason") if choices else None
    output_text = "".join((c.get("message", {}).get("content") or "") for c in choices)
    return ProviderUsage(
        input_tokens=prompt, output_tokens=completion, finish_reason=finish_reason,
        input_tokens_known=prompt > 0, output_tokens_known=completion > 0,
        output_text=output_text,
    )


def _usage_from_anthropic(response_data: dict) -> ProviderUsage:
    usage = response_data.get("usage", {}) or {}
    prompt = int(usage.get("input_tokens", 0))
    completion = int(usage.get("output_tokens", 0))
    content = response_data.get("content", []) or []
    output_text = "".join(p.get("text", "") for p in content if p.get("type") == "text")
    return ProviderUsage(
        input_tokens=prompt, output_tokens=completion,
        finish_reason=response_data.get("stop_reason"),
        input_tokens_known=prompt > 0, output_tokens_known=completion > 0,
        output_text=output_text,
    )


# ---------------------------------------------------------------------------
# Streaming chunk parsing (native SSE relay — see app/routers/proxy.py).
# The client receives provider-native SSE bytes untouched; these helpers only
# extract delta text / finish_reason from each parsed chunk for accounting.
# ---------------------------------------------------------------------------

def stream_delta_text(provider: str, chunk: dict) -> str:
    prov = normalize_provider(provider)
    if prov == "anthropic":
        if chunk.get("type") == "content_block_delta":
            return (chunk.get("delta") or {}).get("text", "") or ""
        return ""
    if prov == "google":
        candidates = chunk.get("candidates") or []
        if not candidates:
            return ""
        parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if "text" in p)
    choice = (chunk.get("choices") or [{}])[0]
    return (choice.get("delta") or {}).get("content") or ""


def stream_finish_reason(provider: str, chunk: dict) -> Optional[str]:
    prov = normalize_provider(provider)
    if prov == "anthropic":
        if chunk.get("type") == "message_delta":
            return (chunk.get("delta") or {}).get("stop_reason")
        return None
    if prov == "google":
        candidates = chunk.get("candidates") or []
        return candidates[0].get("finishReason") if candidates else None
    choice = (chunk.get("choices") or [{}])[0]
    return choice.get("finish_reason")


def stream_is_terminal(provider: str, chunk: dict) -> bool:
    """True if this chunk marks the natural end of the stream (used since
    Anthropic/Google have no 'data: [DONE]' sentinel like OpenAI/Azure)."""
    prov = normalize_provider(provider)
    if prov == "anthropic":
        return chunk.get("type") == "message_stop"
    return False


def _usage_from_google(response_data: dict) -> ProviderUsage:
    meta = response_data.get("usageMetadata", {}) or {}
    prompt = int(meta.get("promptTokenCount", 0))
    completion = int(meta.get("candidatesTokenCount", 0))
    candidates = response_data.get("candidates", []) or []
    finish_reason = candidates[0].get("finishReason") if candidates else None
    output_text = ""
    if candidates:
        parts = (candidates[0].get("content", {}) or {}).get("parts", []) or []
        output_text = "".join(p.get("text", "") for p in parts if "text" in p)
    return ProviderUsage(
        input_tokens=prompt, output_tokens=completion, finish_reason=finish_reason,
        input_tokens_known=prompt > 0, output_tokens_known=completion > 0,
        output_text=output_text,
    )

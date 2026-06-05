"""Counts tokens from raw text using tiktoken.

Used in _ingest_event to back-fill prompt_tokens / completion_tokens when the
caller sends text in input_preview / output_preview but omits the counts.
"""

import logging
from functools import lru_cache

import tiktoken

logger = logging.getLogger(__name__)

# Mapping from common provider model prefixes → tiktoken encoding name.
# cl100k_base covers GPT-4, GPT-3.5-turbo, and is a reasonable approximation
# for Anthropic Claude and most modern LLMs.
_MODEL_ENCODING_MAP: dict[str, str] = {
    "gpt-4":          "cl100k_base",
    "gpt-3.5":        "cl100k_base",
    "text-embedding": "cl100k_base",
    "claude":         "cl100k_base",
    "gemini":         "cl100k_base",
    "mistral":        "cl100k_base",
    "llama":          "cl100k_base",
}

_DEFAULT_ENCODING = "cl100k_base"


@lru_cache(maxsize=16)
def _get_encoding(encoding_name: str) -> tiktoken.Encoding:
    return tiktoken.get_encoding(encoding_name)


def _encoding_for_model(model_name: str | None) -> tiktoken.Encoding:
    if model_name:
        # Try tiktoken's own model→encoding lookup first.
        try:
            return tiktoken.encoding_for_model(model_name)
        except KeyError:
            pass
        # Fall back to prefix matching.
        lower = model_name.lower()
        for prefix, enc_name in _MODEL_ENCODING_MAP.items():
            if lower.startswith(prefix):
                return _get_encoding(enc_name)
    return _get_encoding(_DEFAULT_ENCODING)


def count_tokens(text: str, model_name: str | None = None) -> int:
    """Return the number of tokens in *text* for the given model (or a sensible default)."""
    if not text:
        return 0
    try:
        enc = _encoding_for_model(model_name)
        return len(enc.encode(text))
    except Exception as exc:  # never crash ingestion
        logger.warning("token_counter: failed to count tokens: %s", exc)
        return 0


def fill_missing_token_counts(
    input_text: str | None,
    output_text: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    model_name: str | None = None,
) -> tuple[int, int]:
    """Return (prompt_tokens, completion_tokens), counting from text when counts are 0 or None."""
    if not prompt_tokens and input_text:
        prompt_tokens = count_tokens(input_text, model_name)
    if not completion_tokens and output_text:
        completion_tokens = count_tokens(output_text, model_name)
    return (prompt_tokens or 0), (completion_tokens or 0)

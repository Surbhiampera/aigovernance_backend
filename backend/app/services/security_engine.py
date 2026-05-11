"""SecurityEngine — config-driven PII detection and risk scoring.

All risk weights come from environment variables — zero hardcoded numbers.
Set them in backend/.env; defaults are shown below.

ENV variable             Default  Meaning
─────────────────────────────────────────────────────────────────────
RISK_WEIGHT_INPUT_MB       8      risk pts per MB of input data
RISK_WEIGHT_OUTPUT_MB      12     risk pts per MB of output data
RISK_WEIGHT_TOKEN_PER_500  4      risk pts per 500 tokens
RISK_WEIGHT_PII            20     flat pts when PII detected
RISK_WEIGHT_DATA_OUT       15     flat pts for data-out violation
RISK_WEIGHT_MISUSE         20     flat pts for misuse tag
RISK_WEIGHT_ERROR          5      flat pts for non-success status
RISK_CAP_INPUT_MB          25     max pts from input MB
RISK_CAP_OUTPUT_MB         25     max pts from output MB
RISK_CAP_TOKEN             20     max pts from token volume
DATA_OUT_VIOLATION_MB      0      explicit MB threshold (0 = flag only)
MISUSE_TAGS                exfiltration,credential_abuse,
                           prompt_injection,scraping

Email Agent PII patterns (auto-detected from pii_type field):
  order_id, tracking_id, phone, email_address, customer_id
"""
from __future__ import annotations

import os
import re
from decimal import Decimal

from app.schemas import TelemetryEventCreate

# PII type labels surfaced by the Email Support Agent's sanitization stage
_EMAIL_AGENT_PII_TYPES = frozenset({
    "order_id",
    "tracking_id",
    "phone",
    "phone_number",
    "email_address",
    "customer_id",
    "credit_card",
    "ssn",
})

# Regex patterns that auto-detect email-agent PII in free-text pii_type values
_EMAIL_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("order_id",     re.compile(r"\border[_\s]?id\b",      re.I)),
    ("tracking_id",  re.compile(r"\btrack(ing)?[_\s]?id\b", re.I)),
    ("phone",        re.compile(r"\bphone\b",               re.I)),
    ("email_address",re.compile(r"\bemail[_\s]?addr(ess)?\b", re.I)),
    ("customer_id",  re.compile(r"\bcustomer[_\s]?id\b",   re.I)),
]


def _dec(env_key: str, default: str) -> Decimal:
    return Decimal(os.getenv(env_key, default))


def _misuse_tag_set() -> frozenset[str]:
    raw = os.getenv(
        "MISUSE_TAGS",
        "exfiltration,credential_abuse,prompt_injection,scraping",
    )
    return frozenset(t.strip().lower() for t in raw.split(",") if t.strip())


def _detect_email_pii(pii_type: str | None) -> tuple[bool, str | None]:
    """Return (detected, canonical_type) for email-agent specific PII."""
    if not pii_type:
        return False, None
    low = pii_type.strip().lower()
    if low in _EMAIL_AGENT_PII_TYPES:
        return True, low
    for canonical, pattern in _EMAIL_PII_PATTERNS:
        if pattern.search(pii_type):
            return True, canonical
    return False, None


class SecurityEngine:
    def analyze(self, event_data: TelemetryEventCreate) -> dict:
        input_mb = Decimal(str(event_data.input_data_size_mb or 0))
        output_mb = Decimal(str(event_data.output_data_size_mb or 0))
        total_tokens = event_data.prompt_tokens + event_data.completion_tokens

        # Weights — all from env/config, never hardcoded
        w_in = _dec("RISK_WEIGHT_INPUT_MB", "8")
        w_out = _dec("RISK_WEIGHT_OUTPUT_MB", "12")
        w_tok = _dec("RISK_WEIGHT_TOKEN_PER_500", "4")
        w_pii = _dec("RISK_WEIGHT_PII", "20")
        w_dout = _dec("RISK_WEIGHT_DATA_OUT", "15")
        w_mis = _dec("RISK_WEIGHT_MISUSE", "20")
        w_err = _dec("RISK_WEIGHT_ERROR", "5")
        cap_in = _dec("RISK_CAP_INPUT_MB", "25")
        cap_out = _dec("RISK_CAP_OUTPUT_MB", "25")
        cap_tok = _dec("RISK_CAP_TOKEN", "20")
        dout_mb_thresh = _dec("DATA_OUT_VIOLATION_MB", "0")

        risk = Decimal("0")
        risk += min(input_mb * w_in, cap_in)
        risk += min(output_mb * w_out, cap_out)
        risk += min(Decimal(str(total_tokens)) / Decimal("500") * w_tok, cap_tok)

        email_pii_hit, email_pii_canonical = _detect_email_pii(event_data.pii_type)
        pii_detected = bool(
            event_data.contains_pii
            or email_pii_hit
            or (event_data.pii_type and event_data.pii_type.lower() not in {"", "none"})
        )
        resolved_pii_type = email_pii_canonical or (event_data.pii_type if pii_detected else None)
        if pii_detected:
            risk += w_pii

        data_out_violation = bool(
            event_data.data_out_violation
            or (dout_mb_thresh > 0 and output_mb > dout_mb_thresh)
        )
        if data_out_violation:
            risk += w_dout

        misuse_tags = _misuse_tag_set()
        misuse_pattern_detected = any(t.lower() in misuse_tags for t in event_data.tags)
        if misuse_pattern_detected:
            risk += w_mis

        if event_data.status and event_data.status.lower() not in {"success", "completed"}:
            risk += w_err

        risk = min(risk, Decimal("100"))

        return {
            "pii_detected": pii_detected,
            "pii_type": resolved_pii_type,
            "data_out_violation": data_out_violation,
            "misuse_pattern_detected": misuse_pattern_detected,
            "risk_score": risk.quantize(Decimal("0.01")),
            "masking_applied": pii_detected or data_out_violation,
        }

"""PIIScanner — in-process PII detection, risk scoring, and optional redaction."""
import re
from typing import Optional

_PATTERNS: dict[str, re.Pattern] = {
    "ssn":         re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
    "api_key":     re.compile(r"\b(sk-|AIza|AKIA|xoxb-|xoxp-)[A-Za-z0-9_\-]{16,}\b"),
    "aws_key":     re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "jwt":         re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
    "password":    re.compile(r"(?i)(password|passwd|secret|token)\s*[:=]\s*\S+"),
    "email":       re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    "phone":       re.compile(r"\b(\+\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "ip_address":  re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}

# Criticality: 10 = must block, 1 = low risk
_RISK: dict[str, int] = {
    "ssn": 10, "credit_card": 10,
    "api_key": 9, "aws_key": 9, "jwt": 8, "password": 8,
    "email": 5, "phone": 5,
    "ip_address": 3,
}


class PIIScanner:
    def scan(self, text: str) -> tuple[bool, Optional[str], int]:
        """Return (contains_pii, highest_risk_type, risk_score 0-10)."""
        if not text:
            return False, None, 0
        best_type, best_risk = None, 0
        for pii_type, pattern in _PATTERNS.items():
            if pattern.search(text):
                risk = _RISK.get(pii_type, 5)
                if risk > best_risk:
                    best_type, best_risk = pii_type, risk
        return (best_type is not None), best_type, best_risk

    def scan_messages(self, messages: list[dict]) -> tuple[bool, Optional[str], int]:
        combined = " ".join(
            m.get("content", "") for m in messages if isinstance(m.get("content"), str)
        )
        return self.scan(combined)

    def redact(self, text: str) -> str:
        """Replace all detected PII with [REDACTED:<TYPE>] tokens."""
        for pii_type, pattern in _PATTERNS.items():
            text = pattern.sub(f"[REDACTED:{pii_type.upper()}]", text)
        return text

    def redact_messages(self, messages: list[dict]) -> list[dict]:
        """Return a copy of the messages list with PII removed from all content fields."""
        out = []
        for m in messages:
            if isinstance(m.get("content"), str):
                out.append({**m, "content": self.redact(m["content"])})
            else:
                out.append(m)
        return out

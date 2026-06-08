"""PII detection, masking, and policy enforcement for the proxy layer.

Detects PII in request text, looks up pii_policies for the org to decide
the action (mask / block / alert / allow), applies masking to the text, and
returns a structured result the proxy router can act on.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Regex patterns per PII type
# ---------------------------------------------------------------------------
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    # National ID — highest priority, checked first
    ("aadhar",      re.compile(r"\b[2-9]\d{3}[\s\-]?\d{4}[\s\-]?\d{4}\b")),
    ("ssn",         re.compile(r"\b(?!000|666|9\d{2})\d{3}[\s\-]\d{2}[\s\-]\d{4}\b")),
    ("national_id", re.compile(r"\b[A-Z]{1,2}\d{6,9}[A-Z]?\b")),   # generic passport/ID
    ("credit_card", re.compile(
        r"\b(?:4\d{3}|5[1-5]\d{2}|6(?:011|5\d{2})|3[47]\d{2})"
        r"[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"
    )),
    # High risk
    ("email",       re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("phone",       re.compile(
        r"(?:\+?\d{1,3}[\s\-]?)?"          # optional country code
        r"(?:\(?\d{2,4}\)?[\s\-]?)?"       # optional area code
        r"\d{3,4}[\s\-]?\d{4}"             # local number
    )),
    # Medium risk
    ("ip_address",  re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("date_of_birth", re.compile(
        r"\b(?:dob|date[\s\-]of[\s\-]birth)[:\s]+\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}\b",
        re.IGNORECASE
    )),
    # Name — broad pattern, applied last to avoid false positives
    ("name",        re.compile(
        r"(?:my name is|i am|i'm|name[:\s]+)\s*([A-Z][a-z]+ [A-Z][a-z]+)",
        re.IGNORECASE
    )),
]

# Default mask placeholder when no policy is configured
_DEFAULT_MASKS: dict[str, str] = {
    "aadhar":        "[AADHAR]",
    "ssn":           "[SSN]",
    "national_id":   "[NATIONAL_ID]",
    "credit_card":   "[CREDIT_CARD]",
    "email":         "[EMAIL]",
    "phone":         "[PHONE]",
    "ip_address":    "[IP_ADDRESS]",
    "date_of_birth": "[DOB]",
    "name":          "[NAME]",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class PiiScanResult:
    detections: list[dict] = field(default_factory=list)
    # [{pii_type, risk_level, action, count, matches_preview}]

    sanitized_text: str = ""       # text after masking (same as input if no masking)
    action_taken: str = "allow"    # final action: allow | mask | block | alert
    should_block: bool = False
    pii_detected: bool = False
    pii_types: list[str] = field(default_factory=list)
    pii_masked: bool = False
    audit_required: bool = False


# ---------------------------------------------------------------------------
# Policy loader
# ---------------------------------------------------------------------------
def _load_policies(db: Session, org_id: str) -> dict[str, dict]:
    """Return {pii_type: policy_row} for the org, falling back to global defaults."""
    from app.models import PiiPolicy

    rows = (
        db.query(PiiPolicy)
        .filter(
            PiiPolicy.is_active == True,
            (PiiPolicy.org_id == org_id) | (PiiPolicy.org_id == None),
        )
        .order_by(PiiPolicy.priority.desc())
        .all()
    )

    # org-specific rows override global ones for the same pii_type
    policies: dict[str, dict] = {}
    for row in rows:
        if row.pii_type not in policies or row.org_id == org_id:
            policies[row.pii_type] = {
                "risk_level":    row.risk_level,
                "action":        row.action,
                "mask_pattern":  row.mask_pattern or _DEFAULT_MASKS.get(row.pii_type, f"[{row.pii_type.upper()}]"),
                "log_detection": row.log_detection,
            }
    return policies


# ---------------------------------------------------------------------------
# Core scan function
# ---------------------------------------------------------------------------
def scan_and_mask(
    text: str,
    org_id: str,
    db: Optional[Session] = None,
    policies: Optional[dict[str, dict]] = None,
    project_id: Optional[str] = None,
) -> PiiScanResult:
    """Detect PII in *text*, apply policy actions, return PiiScanResult."""
    if not text:
        return PiiScanResult(sanitized_text=text or "")

    if policies is None and db is not None:
        policies = _load_policies(db, org_id)
    if policies is None:
        policies = {}

    result = PiiScanResult(sanitized_text=text)
    masked_text = text
    final_action = "allow"
    should_block = False

    for pii_type, pattern in _PII_PATTERNS:
        matches = pattern.findall(masked_text)
        if not matches:
            continue

        policy = policies.get(pii_type) or {
            "risk_level":    "medium",
            "action":        "mask",
            "mask_pattern":  _DEFAULT_MASKS.get(pii_type, f"[{pii_type.upper()}]"),
            "log_detection": True,
        }

        action = policy["action"]
        mask_str = policy["mask_pattern"].replace("{pii_type}", pii_type.upper())

        result.pii_detected = True
        result.pii_types.append(pii_type)

        if policy.get("log_detection"):
            result.audit_required = True

        detection = {
            "pii_type":   pii_type,
            "risk_level": policy["risk_level"],
            "action":     action,
            "count":      len(matches),
        }
        result.detections.append(detection)

        if action == "block":
            should_block = True
            final_action = "block"
        elif action == "mask" and not should_block:
            masked_text = pattern.sub(mask_str, masked_text)
            result.pii_masked = True
            if final_action != "block":
                final_action = "mask"
        elif action == "alert" and final_action not in ("block", "mask"):
            final_action = "alert"

    result.sanitized_text = masked_text
    result.action_taken = final_action
    result.should_block = should_block
    return result

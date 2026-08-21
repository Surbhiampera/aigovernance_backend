"""PII detection, masking, and policy enforcement using Microsoft Presidio + spaCy NER.

Detection pipeline:
  1. Presidio AnalyzerEngine (spaCy en_core_web_lg) — context-aware NER for names,
     locations, orgs, plus built-in recognizers for email, phone, SSN, credit card,
     IP address, date-of-birth, passport, medical licence, etc.
  2. Custom recognizers for Aadhar and generic national-ID formats not covered by
     Presidio's default set.
  3. AnonymizerEngine replaces detected spans with type-labelled placeholders
     (e.g. [EMAIL], [PHONE]) or org-configured masks from pii_policies.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from typing import Optional

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig
from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Presidio entity type → internal PII label
# ---------------------------------------------------------------------------
_PRESIDIO_TO_INTERNAL: dict[str, str] = {
    "EMAIL_ADDRESS":        "email",
    "PHONE_NUMBER":         "phone",
    "CREDIT_CARD":          "credit_card",
    "US_SSN":               "ssn",
    "IP_ADDRESS":           "ip_address",
    "DATE_TIME":            "date_of_birth",
    "PERSON":               "name",
    "LOCATION":             "location",
    "NRP":                  "national_id",
    "US_PASSPORT":          "passport",
    "US_DRIVER_LICENSE":    "national_id",
    "MEDICAL_LICENSE":      "national_id",
    "IBAN_CODE":            "bank_account",
    "CRYPTO":               "crypto",
    "AADHAR":               "aadhar",
    "NATIONAL_ID":          "national_id",
    "ORGANIZATION":         "organization",
    "URL":                  "url",
}

_DEFAULT_MASKS: dict[str, str] = {
    "email":         "[EMAIL]",
    "phone":         "[PHONE]",
    "credit_card":   "[CREDIT_CARD]",
    "ssn":           "[SSN]",
    "ip_address":    "[IP_ADDRESS]",
    "date_of_birth": "[DOB]",
    "name":          "[NAME]",
    "location":      "[LOCATION]",
    "national_id":   "[NATIONAL_ID]",
    "passport":      "[PASSPORT]",
    "bank_account":  "[BANK_ACCOUNT]",
    "crypto":        "[CRYPTO]",
    "aadhar":        "[AADHAR]",
    "organization":  "[ORGANIZATION]",
    "url":           "[URL]",
}

# Types that elevate severity to High regardless of entity count (includes national_id for PAN)
_HIGH_SENSITIVITY: frozenset[str] = frozenset({
    "aadhar", "credit_card", "passport", "ssn", "bank_account", "national_id"
})

# Business/contextual entities that NER frequently over-flags (company names, city
# names, job-posting links) but that are not personal-identifying data on their own.
# These are logged but allowed through by default unless an org explicitly configures
# a stricter PiiPolicy for them.
_LOW_SENSITIVITY_DEFAULT_ALLOW: frozenset[str] = frozenset({
    "location", "organization", "url"
})


def compute_pii_severity(entity_types: list[str]) -> str:
    """Return 'high'/'medium'/'low'/'' based on the sensitivity of detected entities.

    One occurrence of a high-sensitivity type (ssn, credit_card, ...) is always
    'high'. Occurrences of low-sensitivity, default-allowed types (organization,
    location, url) don't count toward the volume thresholds below — only
    entities that are PII on their own do, so e.g. 11 "organization" mentions
    and nothing else stays 'low', not 'high'.
    """
    if not entity_types:
        return ""
    if any(t in _HIGH_SENSITIVITY for t in entity_types):
        return "high"
    significant = [t for t in entity_types if t not in _LOW_SENSITIVITY_DEFAULT_ALLOW]
    if len(significant) >= 5:
        return "high"
    if len(significant) >= 3:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Custom recognizers for types not in Presidio's default set
# ---------------------------------------------------------------------------
_aadhar_recognizer = PatternRecognizer(
    supported_entity="AADHAR",
    patterns=[Pattern("AADHAR", r"\b[2-9]\d{3}[\s\-]?\d{4}[\s\-]?\d{4}\b", 0.85)],
)

_national_id_recognizer = PatternRecognizer(
    supported_entity="NATIONAL_ID",
    patterns=[Pattern("NATIONAL_ID", r"\b[A-Z]{1,2}\d{6,9}[A-Z]?\b", 0.6)],
)


def _is_proper_noun_span(doc, start: int, end: int) -> bool:
    """True unless every alphabetic token in [start, end) is POS-tagged as a common
    noun/adjective rather than a proper noun.

    Presidio's spaCy PERSON recognizer has no confidence gradient (fixed 0.85 score)
    and frequently mislabels short, capitalized, context-free spans — typically
    document headings like "Deliverables" or "Key Deliverables" — as person names.
    spaCy's POS tagger, run over the same text, correctly tags these as NOUN/ADJ
    rather than PROPN even when the NER layer gets it wrong. Cross-checking against
    POS catches the false positive without hardcoding any specific word, so it
    generalizes to any heading/label text rather than just one document's wording.
    """
    span = doc.char_span(start, end, alignment_mode="expand")
    if span is None:
        return True  # can't verify alignment — don't suppress a real detection
    tokens = [t for t in span if t.is_alpha]
    if not tokens:
        return True
    return any(t.pos_ == "PROPN" for t in tokens)


def _filter_person_false_positives(results: list, text: str, analyzer: AnalyzerEngine) -> list:
    if not any(r.entity_type == "PERSON" for r in results):
        return results
    try:
        doc = analyzer.nlp_engine.nlp["en"](text)
    except Exception as e:
        _log.debug("POS cross-check unavailable, skipping PERSON false-positive filter: %s", e)
        return results

    filtered = []
    for r in results:
        if r.entity_type == "PERSON" and not _is_proper_noun_span(doc, r.start, r.end):
            _log.debug(
                "Suppressing PERSON false positive %r (POS-tagged as common noun, not a proper noun)",
                text[r.start:r.end],
            )
            continue
        filtered.append(r)
    return filtered


# ---------------------------------------------------------------------------
# Lazy-initialised engines — loaded once, reused across all requests
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def _get_engines() -> tuple[AnalyzerEngine, AnonymizerEngine]:
    provider = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
    })
    nlp_engine = provider.create_engine()

    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
    analyzer.registry.add_recognizer(_aadhar_recognizer)
    analyzer.registry.add_recognizer(_national_id_recognizer)

    anonymizer = AnonymizerEngine()
    return analyzer, anonymizer


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class PiiScanResult:
    detections: list[dict] = field(default_factory=list)
    sanitized_text: str = ""
    action_taken: str = "allow"
    should_block: bool = False
    pii_detected: bool = False
    pii_types: list[str] = field(default_factory=list)
    pii_masked: bool = False
    audit_required: bool = False
    # Aggregate counts and severity (populated by scan_and_mask)
    entities_detected: int = 0
    entities_masked: int = 0
    severity: str = ""          # "low" | "medium" | "high" | ""
    entity_details: list[dict] = field(default_factory=list)  # [{pii_type, original_value, masked_as, risk_level}]


_RISK_ORDER: dict[str, int] = {"low": 1, "medium": 2, "high": 3}


# ---------------------------------------------------------------------------
# Policy loader
# ---------------------------------------------------------------------------
def _load_policies(db: Session, org_id: str) -> dict[str, dict]:
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
# Core scan function  (same signature as before — drop-in replacement)
# ---------------------------------------------------------------------------
def scan_and_mask(
    text: str,
    org_id: str,
    db: Optional[Session] = None,
    policies: Optional[dict[str, dict]] = None,
    project_id: Optional[str] = None,
) -> PiiScanResult:
    if not text:
        return PiiScanResult(sanitized_text=text or "")

    if policies is None and db is not None:
        policies = _load_policies(db, org_id)
    if policies is None:
        policies = {}

    try:
        analyzer, anonymizer = _get_engines()
    except Exception as e:
        _log.error("Presidio engine init failed: %s", e)
        return PiiScanResult(sanitized_text=text)

    # --- Analyze -----------------------------------------------------------
    try:
        analyzer_results = analyzer.analyze(text=text, language="en")
    except Exception as e:
        _log.warning("Presidio analyze failed: %s", e)
        return PiiScanResult(sanitized_text=text)

    analyzer_results = _filter_person_false_positives(analyzer_results, text, analyzer)

    if not analyzer_results:
        return PiiScanResult(sanitized_text=text)

    # --- Group by internal type, apply policies ----------------------------
    result = PiiScanResult(sanitized_text=text)
    final_action = "allow"
    should_block = False

    by_type: dict[str, list] = {}
    for r in analyzer_results:
        internal = _PRESIDIO_TO_INTERNAL.get(r.entity_type, r.entity_type.lower())
        by_type.setdefault(internal, []).append(r)

    # Resolve policy per type once; reused for detections summary and entity detail
    resolved_policies: dict[str, dict] = {}
    for pii_type, spans in by_type.items():
        default_action = "allow" if pii_type in _LOW_SENSITIVITY_DEFAULT_ALLOW else "mask"
        policy = policies.get(pii_type) or {
            "risk_level":    "low" if default_action == "allow" else "medium",
            "action":        default_action,
            "mask_pattern":  _DEFAULT_MASKS.get(pii_type, f"[{pii_type.upper()}]"),
            "log_detection": True,
        }
        resolved_policies[pii_type] = policy

        action = policy["action"]

        result.pii_detected = True
        if pii_type not in result.pii_types:
            result.pii_types.append(pii_type)

        if policy.get("log_detection"):
            result.audit_required = True

        result.detections.append({
            "pii_type":   pii_type,
            "risk_level": policy["risk_level"],
            "action":     action,
            "count":      len(spans),
        })

        if action == "block":
            should_block = True
            final_action = "block"
        elif action == "mask" and not should_block:
            if final_action != "block":
                final_action = "mask"
        elif action == "alert" and final_action not in ("block", "mask"):
            final_action = "alert"

    # --- Build per-span entity detail with original and masked values -------
    _entity_details: list[dict] = []
    for r in analyzer_results:
        internal = _PRESIDIO_TO_INTERNAL.get(r.entity_type, r.entity_type.lower())
        default_action = "allow" if internal in _LOW_SENSITIVITY_DEFAULT_ALLOW else "mask"
        p = resolved_policies.get(internal, {
            "risk_level": "low" if default_action == "allow" else "medium",
            "action": default_action,
            "mask_pattern": _DEFAULT_MASKS.get(internal, f"[{internal.upper()}]"),
        })
        _entity_details.append({
            "pii_type":      internal,
            "original_value": text[r.start:r.end],
            "masked_value":  p["mask_pattern"] if p["action"] == "mask" else None,
            "risk_level":    p["risk_level"],
            "start":         r.start,
            "end":           r.end,
        })

    # --- Anonymize in one pass via Presidio AnonymizerEngine ---------------
    # Only mask spans whose own resolved policy action is "mask" — a type set to
    # "allow" (e.g. organization/location/url) must stay untouched even when some
    # other entity elsewhere in the same text triggers masking.
    maskable_results = [
        r for r in analyzer_results
        if resolved_policies.get(
            _PRESIDIO_TO_INTERNAL.get(r.entity_type, r.entity_type.lower()), {}
        ).get("action") == "mask"
    ]

    if maskable_results and not should_block:
        operators: dict[str, OperatorConfig] = {}
        for r in maskable_results:
            internal = _PRESIDIO_TO_INTERNAL.get(r.entity_type, r.entity_type.lower())
            mask_str = (policies.get(internal) or {}).get("mask_pattern") or _DEFAULT_MASKS.get(internal, f"[{internal.upper()}]")
            operators[r.entity_type] = OperatorConfig("replace", {"new_value": mask_str})

        try:
            anonymized = anonymizer.anonymize(
                text=text,
                analyzer_results=maskable_results,
                operators=operators,
            )
            result.sanitized_text = anonymized.text
            result.pii_masked = True
        except Exception as e:
            _log.warning("Presidio anonymize failed: %s", e)
            result.sanitized_text = text

    result.action_taken = final_action
    result.should_block = should_block
    result.entity_details = _entity_details
    result.entities_detected = len(_entity_details)
    result.entities_masked = (
        sum(1 for e in _entity_details if e["masked_value"] is not None)
        if result.pii_masked else 0
    )
    result.severity = compute_pii_severity([e["pii_type"] for e in result.entity_details])
    return result

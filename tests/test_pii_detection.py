"""Tests for PII severity tracking, entity detail capture, and the pii-detail endpoint.

Covers:
  1. compute_pii_severity()  — all severity levels and high-sensitivity types (no DB/Presidio)
  2. scan_and_mask()         — entity_details capture, before/after values, counts
                               (Presidio engines mocked so spaCy model not required)
  3. Proxy integration       — AiRequest stores pii_severity, pii_entities_*, pii_detail
  4. list_proxy_requests     — new fields in response, pii_severity filter param
  5. pii-detail endpoint     — full entity detail returned, 404 on unknown request_id

Run:
    DATABASE_URL=<url> python -m pytest tests/test_pii_detection.py -v
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.models import AiRequest, Organization, Project
from app.services.governance_key_service import create_governance_key
from app.services.pii_engine import PiiScanResult, compute_pii_severity


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

FAKE_AZURE_RESPONSE = {
    "id": "chatcmpl-pii-test",
    "object": "chat.completion",
    "model": "gpt-4o",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "Noted."}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
}


def _fake_http(body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "https://fake.azure.com"),
    )


def _mock_azure_client(response: httpx.Response):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=response)
    return client


def _mock_deployment():
    return MagicMock(
        deployment_id="dep-pii",
        model_name="gpt-4o",
        deployment_name="gpt-4o",
        provider="azure",
        api_endpoint="https://fake.azure.com",
        api_version="2024-02-01",
    )


def _seed(db, org_id: str, project_id: str) -> str:
    """Insert org + project + governance key; return raw key."""
    db.add(Organization(id=org_id, org_name=f"Org {org_id}"))
    db.add(Project(id=project_id, org_id=org_id, project_name=f"Project {project_id}"))
    db.flush()
    result = create_governance_key(db, org_id=org_id, project_id=project_id, key_name="test-key")
    db.flush()
    return result["raw_key"]


def _proxy_patches(azure_body: dict | None = None):
    """Return the three standard patches needed for any proxy call."""
    body = azure_body or FAKE_AZURE_RESPONSE
    return [
        patch("app.routers.proxy.httpx.AsyncClient",
              return_value=_mock_azure_client(_fake_http(body))),
        patch("app.routers.proxy.get_deployment_for_org",
              return_value=_mock_deployment()),
        patch("app.routers.proxy.build_provider_request",
              return_value=("https://fake.azure.com", {})),
    ]


def _pii_result(
    pii_type: str = "email",
    original: str = "user@example.com",
    masked: str = "[EMAIL]",
    risk_level: str = "medium",
    count: int = 1,
    severity: str = "low",
) -> PiiScanResult:
    """Build a controlled PiiScanResult for injection into scan_and_mask mock."""
    entities = [
        {
            "pii_type": pii_type,
            "original_value": original,
            "masked_value": masked,
            "risk_level": risk_level,
            "start": 0,
            "end": len(original),
        }
    ] * count
    return PiiScanResult(
        pii_detected=True,
        pii_types=[pii_type],
        pii_masked=True,
        action_taken="mask",
        sanitized_text=f"text with {masked}",
        entity_details=entities,
        entities_detected=count,
        entities_masked=count,
        severity=severity,
    )


def _post_proxy(client, raw_key: str, messages: list[dict]) -> httpx.Response:
    return client.post(
        "/proxy",
        headers={"X-Governance-Key": raw_key},
        json={"model": "gpt-4o", "messages": messages},
    )


# ===========================================================================
# 1. compute_pii_severity — pure unit tests, no DB, no Presidio
# ===========================================================================

class TestComputePiiSeverity:

    def test_no_entities_returns_empty(self):
        assert compute_pii_severity([]) == ""

    def test_one_entity_is_low(self):
        assert compute_pii_severity(["email"]) == "low"

    def test_two_entities_is_low(self):
        assert compute_pii_severity(["email", "name"]) == "low"

    def test_three_entities_is_medium(self):
        assert compute_pii_severity(["email", "name", "phone"]) == "medium"

    def test_four_entities_is_medium(self):
        assert compute_pii_severity(["email", "name", "phone", "ip_address"]) == "medium"

    def test_five_entities_is_high(self):
        assert compute_pii_severity(["e", "n", "p", "i", "l"]) == "high"

    def test_ten_entities_is_high(self):
        assert compute_pii_severity(["email"] * 10) == "high"

    @pytest.mark.parametrize("sensitive_type", [
        "aadhar",
        "credit_card",
        "passport",
        "ssn",
        "bank_account",
        "national_id",   # covers PAN card
    ])
    def test_high_sensitivity_type_always_high(self, sensitive_type):
        assert compute_pii_severity([sensitive_type]) == "high"

    def test_high_sensitivity_overrides_low_count(self):
        # Only 1 entity but it's a credit card — count alone would be Low
        assert compute_pii_severity(["credit_card"]) == "high"

    def test_high_sensitivity_in_mixed_list(self):
        # 2 entities, one is aadhar — must be High, not Low
        assert compute_pii_severity(["email", "aadhar"]) == "high"

    def test_national_id_covers_pan_card(self):
        # national_id recognizer catches PAN-style IDs
        assert compute_pii_severity(["national_id"]) == "high"

    def test_many_low_sensitivity_entities_stay_low(self):
        # 11 "organization" mentions and nothing else — should stay Low, not High,
        # since organization/location/url don't count toward the volume thresholds.
        assert compute_pii_severity(["organization"] * 11) == "low"

    def test_low_sensitivity_entities_dont_push_to_medium(self):
        assert compute_pii_severity(["organization", "location", "url"]) == "low"

    def test_low_sensitivity_mixed_with_two_significant_stays_low(self):
        # 2 significant entities (email, name) plus a pile of low-sensitivity
        # organization mentions — significant count is still only 2, so Low.
        assert compute_pii_severity(["organization"] * 8 + ["email", "name"]) == "low"

    def test_low_sensitivity_mixed_with_three_significant_is_medium(self):
        assert compute_pii_severity(
            ["organization"] * 8 + ["email", "name", "phone"]
        ) == "medium"


# ===========================================================================
# 2. scan_and_mask — unit tests with mocked Presidio engines
# ===========================================================================

class TestScanAndMask:
    """Mock _get_engines so tests run without the spaCy en_core_web_lg model."""

    @staticmethod
    def _ar(entity_type: str, start: int, end: int):
        """Build a fake Presidio RecognizerResult."""
        r = MagicMock()
        r.entity_type = entity_type
        r.start = start
        r.end = end
        r.score = 0.85
        return r

    @staticmethod
    def _anonymized(text: str):
        m = MagicMock()
        m.text = text
        m.items = []
        return m

    def test_entity_details_contains_original_and_masked_value(self):
        from app.services.pii_engine import scan_and_mask

        text = "Contact john@example.com for info"
        #        01234567890123456789012345678901
        #                 8       24  → "john@example.com"

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = [self._ar("EMAIL_ADDRESS", 8, 24)]

        mock_anonymizer = MagicMock()
        mock_anonymizer.anonymize.return_value = self._anonymized("Contact [EMAIL] for info")

        with patch("app.services.pii_engine._get_engines",
                   return_value=(mock_analyzer, mock_anonymizer)):
            result = scan_and_mask(text=text, org_id="test-org", policies={})

        assert result.pii_detected is True
        assert result.pii_masked is True
        assert len(result.entity_details) == 1
        e = result.entity_details[0]
        assert e["pii_type"] == "email"
        assert e["original_value"] == "john@example.com"
        assert e["masked_value"] == "[EMAIL]"
        assert e["risk_level"] == "medium"
        assert e["start"] == 8
        assert e["end"] == 24

    def test_entities_detected_and_masked_counts(self):
        from app.services.pii_engine import scan_and_mask

        text = "a@b.com 555-1234"
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = [
            self._ar("EMAIL_ADDRESS", 0, 7),
            self._ar("PHONE_NUMBER", 8, 16),
        ]
        mock_anonymizer = MagicMock()
        mock_anonymizer.anonymize.return_value = self._anonymized("[EMAIL] [PHONE]")

        with patch("app.services.pii_engine._get_engines",
                   return_value=(mock_analyzer, mock_anonymizer)):
            result = scan_and_mask(text=text, org_id="test-org", policies={})

        assert result.entities_detected == 2
        assert result.entities_masked == 2
        assert result.severity == "low"

    def test_severity_high_for_credit_card(self):
        from app.services.pii_engine import scan_and_mask

        text = "4111111111111111"
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = [self._ar("CREDIT_CARD", 0, 16)]
        mock_anonymizer = MagicMock()
        mock_anonymizer.anonymize.return_value = self._anonymized("[CREDIT_CARD]")

        with patch("app.services.pii_engine._get_engines",
                   return_value=(mock_analyzer, mock_anonymizer)):
            result = scan_and_mask(text=text, org_id="test-org", policies={})

        assert result.severity == "high"

    def test_empty_text_returns_empty_result(self):
        from app.services.pii_engine import scan_and_mask

        result = scan_and_mask(text="", org_id="test-org")
        assert result.entities_detected == 0
        assert result.entities_masked == 0
        assert result.severity == ""
        assert result.entity_details == []

    def test_no_pii_detected_returns_zero_counts(self):
        from app.services.pii_engine import scan_and_mask

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = []

        with patch("app.services.pii_engine._get_engines",
                   return_value=(mock_analyzer, MagicMock())):
            result = scan_and_mask(text="Hello world", org_id="test-org", policies={})

        assert result.pii_detected is False
        assert result.entities_detected == 0
        assert result.entities_masked == 0
        assert result.severity == ""
        assert result.entity_details == []

    def test_block_action_sets_entities_masked_to_zero(self):
        from app.services.pii_engine import scan_and_mask

        text = "SSN 123-45-6789"
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = [self._ar("US_SSN", 4, 15)]
        policies = {
            "ssn": {"risk_level": "high", "action": "block",
                    "mask_pattern": "[SSN]", "log_detection": True}
        }

        with patch("app.services.pii_engine._get_engines",
                   return_value=(mock_analyzer, MagicMock())):
            result = scan_and_mask(text=text, org_id="test-org", policies=policies)

        assert result.should_block is True
        assert result.entities_detected == 1
        assert result.entities_masked == 0   # blocked, not masked
        assert result.entity_details[0]["masked_value"] is None

    def test_custom_policy_mask_pattern_used(self):
        from app.services.pii_engine import scan_and_mask

        text = "a@b.com"
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = [self._ar("EMAIL_ADDRESS", 0, 7)]
        mock_anonymizer = MagicMock()
        mock_anonymizer.anonymize.return_value = self._anonymized("***EMAIL***")
        policies = {
            "email": {"risk_level": "medium", "action": "mask",
                      "mask_pattern": "***EMAIL***", "log_detection": True}
        }

        with patch("app.services.pii_engine._get_engines",
                   return_value=(mock_analyzer, mock_anonymizer)):
            result = scan_and_mask(text=text, org_id="test-org", policies=policies)

        assert result.entity_details[0]["masked_value"] == "***EMAIL***"

    def test_severity_medium_for_three_low_sensitivity_entities(self):
        from app.services.pii_engine import scan_and_mask

        text = "a b c"
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = [
            self._ar("EMAIL_ADDRESS", 0, 1),
            self._ar("PHONE_NUMBER", 2, 3),
            self._ar("PERSON", 4, 5),
        ]
        mock_anonymizer = MagicMock()
        mock_anonymizer.anonymize.return_value = self._anonymized("[E] [P] [N]")

        with patch("app.services.pii_engine._get_engines",
                   return_value=(mock_analyzer, mock_anonymizer)):
            result = scan_and_mask(text=text, org_id="test-org", policies={})

        assert result.entities_detected == 3
        assert result.severity == "medium"


# ===========================================================================
# 3. Proxy integration — AiRequest stores new PII fields
# ===========================================================================

class TestProxyPiiFieldsStored:

    def test_masked_request_stores_severity_counts_and_detail(self, client, db_session):
        raw_key = _seed(db_session, "org-p01", "proj-p01")
        pii = _pii_result("email", "user@corp.com", "[EMAIL]", "medium", 1, "low")

        p = _proxy_patches()
        with p[0], p[1], p[2], \
             patch("app.routers.proxy.scan_and_mask", return_value=pii):
            resp = _post_proxy(client, raw_key,
                               [{"role": "user", "content": "email is user@corp.com"}])

        assert resp.status_code == 200

        row = db_session.query(AiRequest).filter(AiRequest.org_id == "org-p01").first()
        assert row is not None
        assert row.pii_detected is True
        assert row.pii_severity == "low"
        assert row.pii_entities_detected == 1
        assert row.pii_entities_masked == 1
        assert row.pii_detail is not None
        assert len(row.pii_detail) == 1
        assert row.pii_detail[0]["pii_type"] == "email"
        assert row.pii_detail[0]["original_value"] == "user@corp.com"
        assert row.pii_detail[0]["masked_value"] == "[EMAIL]"

    def test_no_pii_request_stores_null_severity_and_zero_counts(self, client, db_session):
        raw_key = _seed(db_session, "org-p02", "proj-p02")
        no_pii = PiiScanResult(sanitized_text="Hello world")

        p = _proxy_patches()
        with p[0], p[1], p[2], \
             patch("app.routers.proxy.scan_and_mask", return_value=no_pii):
            resp = _post_proxy(client, raw_key,
                               [{"role": "user", "content": "Hello world"}])

        assert resp.status_code == 200

        row = db_session.query(AiRequest).filter(AiRequest.org_id == "org-p02").first()
        assert row.pii_detected is False
        assert row.pii_severity is None
        assert row.pii_entities_detected == 0
        assert row.pii_entities_masked == 0
        assert row.pii_detail is None

    def test_aadhar_stores_high_severity(self, client, db_session):
        raw_key = _seed(db_session, "org-p03", "proj-p03")
        pii = _pii_result("aadhar", "2345 6789 0123", "[AADHAR]", "high", 1, "high")

        p = _proxy_patches()
        with p[0], p[1], p[2], \
             patch("app.routers.proxy.scan_and_mask", return_value=pii):
            resp = _post_proxy(client, raw_key,
                               [{"role": "user", "content": "my aadhar 2345 6789 0123"}])

        assert resp.status_code == 200

        row = db_session.query(AiRequest).filter(AiRequest.org_id == "org-p03").first()
        assert row.pii_severity == "high"
        assert row.pii_detail[0]["pii_type"] == "aadhar"
        assert row.pii_detail[0]["original_value"] == "2345 6789 0123"

    def test_multi_message_entities_accumulated_with_message_index(self, client, db_session):
        """PII across two messages must merge into a single pii_detail list with correct roles."""
        raw_key = _seed(db_session, "org-p04", "proj-p04")

        call_count = 0
        msg1_pii = _pii_result("email", "a@b.com", "[EMAIL]", "medium", 1, "low")
        msg2_pii = _pii_result("phone", "555-1234", "[PHONE]", "medium", 1, "low")

        def side_effect(text, org_id, **kwargs):
            nonlocal call_count
            call_count += 1
            return msg1_pii if call_count == 1 else msg2_pii

        p = _proxy_patches()
        with p[0], p[1], p[2], \
             patch("app.routers.proxy.scan_and_mask", side_effect=side_effect):
            resp = _post_proxy(client, raw_key, [
                {"role": "user", "content": "email a@b.com"},
                {"role": "assistant", "content": "phone 555-1234"},
            ])

        assert resp.status_code == 200

        row = db_session.query(AiRequest).filter(AiRequest.org_id == "org-p04").first()
        assert row.pii_entities_detected == 2
        assert len(row.pii_detail) == 2

        by_type = {e["pii_type"]: e for e in row.pii_detail}
        assert set(by_type.keys()) == {"email", "phone"}
        assert by_type["email"]["message_index"] == 0
        assert by_type["email"]["role"] == "user"
        assert by_type["phone"]["message_index"] == 1
        assert by_type["phone"]["role"] == "assistant"

    def test_five_entities_stores_high_severity(self, client, db_session):
        """5 low-sensitivity entities must produce High severity (count threshold)."""
        raw_key = _seed(db_session, "org-p05", "proj-p05")

        call_count = 0
        five_low = PiiScanResult(
            pii_detected=True,
            pii_types=["email", "name", "phone", "ip_address", "location"],
            pii_masked=True,
            action_taken="mask",
            sanitized_text="masked text",
            entity_details=[
                {"pii_type": t, "original_value": "x", "masked_value": f"[{t.upper()}]",
                 "risk_level": "medium", "start": 0, "end": 1}
                for t in ["email", "name", "phone", "ip_address", "location"]
            ],
            entities_detected=5,
            entities_masked=5,
            severity="high",
        )

        p = _proxy_patches()
        with p[0], p[1], p[2], \
             patch("app.routers.proxy.scan_and_mask", return_value=five_low):
            resp = _post_proxy(client, raw_key,
                               [{"role": "user", "content": "lots of pii here"}])

        assert resp.status_code == 200

        row = db_session.query(AiRequest).filter(AiRequest.org_id == "org-p05").first()
        assert row.pii_severity == "high"
        assert row.pii_entities_detected == 5


# ===========================================================================
# 4. list_proxy_requests — new fields in response, pii_severity filter
# ===========================================================================

class TestListProxyRequestsPiiFields:

    def _make_request(self, client, db_session, org_id, project_id, pii):
        raw_key = _seed(db_session, org_id, project_id)
        p = _proxy_patches()
        with p[0], p[1], p[2], \
             patch("app.routers.proxy.scan_and_mask", return_value=pii):
            _post_proxy(client, raw_key, [{"role": "user", "content": "test"}])

    def test_response_includes_pii_severity_and_entity_counts(self, client, db_session):
        pii = _pii_result("email", "x@y.com", "[EMAIL]", "medium", 2, "low")
        self._make_request(client, db_session, "org-l01", "proj-l01", pii)

        resp = client.get("/proxy/v1/requests", params={"org_id": "org-l01", "pii_only": True})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["pii_severity"] == "low"
        assert items[0]["pii_entities_detected"] == 2
        assert items[0]["pii_entities_masked"] == 2

    def test_pii_severity_filter_excludes_non_matching(self, client, db_session):
        high_pii = _pii_result("aadhar", "2345 6789 0123", "[AADHAR]", "high", 1, "high")
        low_pii  = _pii_result("email",  "a@b.com",        "[EMAIL]",  "medium", 1, "low")
        self._make_request(client, db_session, "org-l02", "proj-l02", high_pii)
        self._make_request(client, db_session, "org-l03", "proj-l03", low_pii)

        resp = client.get("/proxy/v1/requests", params={"pii_severity": "high"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        matched = [i for i in items if i["org_id"] in ("org-l02", "org-l03")]
        assert all(i["pii_severity"] == "high" for i in matched)
        assert not any(i["org_id"] == "org-l03" for i in matched)

    def test_pii_severity_filter_medium(self, client, db_session):
        medium_pii = PiiScanResult(
            pii_detected=True, pii_types=["email", "name", "phone"],
            pii_masked=True, action_taken="mask", sanitized_text="x",
            entity_details=[
                {"pii_type": t, "original_value": "v", "masked_value": "[X]",
                 "risk_level": "medium", "start": 0, "end": 1}
                for t in ["email", "name", "phone"]
            ],
            entities_detected=3, entities_masked=3, severity="medium",
        )
        self._make_request(client, db_session, "org-l04", "proj-l04", medium_pii)

        resp = client.get("/proxy/v1/requests",
                          params={"org_id": "org-l04", "pii_severity": "medium"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["pii_severity"] == "medium"
        assert items[0]["pii_entities_detected"] == 3

    def test_no_pii_request_has_null_severity_and_zero_counts(self, client, db_session):
        self._make_request(client, db_session, "org-l05", "proj-l05",
                           PiiScanResult(sanitized_text="Hello"))

        resp = client.get("/proxy/v1/requests", params={"org_id": "org-l05"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["pii_severity"] is None
        assert items[0]["pii_entities_detected"] == 0
        assert items[0]["pii_entities_masked"] == 0

    def test_pii_only_filter_still_works(self, client, db_session):
        pii = _pii_result("email", "x@y.com", "[EMAIL]", "medium", 1, "low")
        self._make_request(client, db_session, "org-l06", "proj-l06", pii)
        self._make_request(client, db_session, "org-l07", "proj-l07",
                           PiiScanResult(sanitized_text="clean"))

        resp = client.get("/proxy/v1/requests", params={"pii_only": True})
        assert resp.status_code == 200
        items = resp.json()["items"]
        org_ids = {i["org_id"] for i in items}
        assert "org-l06" in org_ids
        assert "org-l07" not in org_ids


# ===========================================================================
# 5. pii-detail endpoint
# ===========================================================================

class TestPiiDetailEndpoint:

    def _make_and_get_id(self, client, db_session, org_id, project_id, pii) -> str:
        raw_key = _seed(db_session, org_id, project_id)
        p = _proxy_patches()
        with p[0], p[1], p[2], \
             patch("app.routers.proxy.scan_and_mask", return_value=pii):
            resp = _post_proxy(client, raw_key,
                               [{"role": "user", "content": "test content"}])
        assert resp.status_code == 200
        row = db_session.query(AiRequest).filter(AiRequest.org_id == org_id).first()
        return row.request_id

    def test_returns_full_pii_entity_detail(self, client, db_session):
        pii = _pii_result("email", "user@corp.com", "[EMAIL]", "medium", 1, "low")
        rid = self._make_and_get_id(client, db_session, "org-d01", "proj-d01", pii)

        resp = client.get(f"/proxy/v1/requests/{rid}/pii-detail")
        assert resp.status_code == 200

        data = resp.json()
        assert data["request_id"] == rid
        assert data["pii_detected"] is True
        assert data["pii_severity"] == "low"
        assert data["pii_entities_detected"] == 1
        assert data["pii_entities_masked"] == 1
        assert len(data["pii_detail"]) == 1
        e = data["pii_detail"][0]
        assert e["pii_type"] == "email"
        assert e["original_value"] == "user@corp.com"
        assert e["masked_value"] == "[EMAIL]"

    def test_response_includes_request_payload(self, client, db_session):
        pii = _pii_result("email", "a@b.com", "[EMAIL]", "medium", 1, "low")
        rid = self._make_and_get_id(client, db_session, "org-d02", "proj-d02", pii)

        resp = client.get(f"/proxy/v1/requests/{rid}/pii-detail")
        data = resp.json()
        assert "request_payload" in data
        assert data["request_payload"] is not None

    def test_high_severity_reflected_in_detail_response(self, client, db_session):
        pii = _pii_result("credit_card", "4111111111111111", "[CREDIT_CARD]", "high", 1, "high")
        rid = self._make_and_get_id(client, db_session, "org-d03", "proj-d03", pii)

        resp = client.get(f"/proxy/v1/requests/{rid}/pii-detail")
        assert resp.status_code == 200
        assert resp.json()["pii_severity"] == "high"

    def test_404_for_unknown_request_id(self, client, db_session):
        resp = client.get("/proxy/v1/requests/req-doesnotexist999/pii-detail")
        assert resp.status_code == 404

    def test_no_pii_request_returns_empty_detail_list(self, client, db_session):
        rid = self._make_and_get_id(
            client, db_session, "org-d04", "proj-d04",
            PiiScanResult(sanitized_text="Hello world"),
        )

        resp = client.get(f"/proxy/v1/requests/{rid}/pii-detail")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pii_detected"] is False
        assert data["pii_detail"] == []
        assert data["pii_severity"] is None
        assert data["pii_entities_detected"] == 0

    def test_multiple_entities_all_present_in_detail(self, client, db_session):
        pii = PiiScanResult(
            pii_detected=True,
            pii_types=["email", "phone"],
            pii_masked=True,
            action_taken="mask",
            sanitized_text="[EMAIL] [PHONE]",
            entity_details=[
                {"pii_type": "email",  "original_value": "a@b.com",  "masked_value": "[EMAIL]",
                 "risk_level": "medium", "start": 0, "end": 7},
                {"pii_type": "phone",  "original_value": "555-1234", "masked_value": "[PHONE]",
                 "risk_level": "medium", "start": 8, "end": 16},
            ],
            entities_detected=2,
            entities_masked=2,
            severity="low",
        )
        rid = self._make_and_get_id(client, db_session, "org-d05", "proj-d05", pii)

        resp = client.get(f"/proxy/v1/requests/{rid}/pii-detail")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pii_entities_detected"] == 2
        assert len(data["pii_detail"]) == 2
        types = {e["pii_type"] for e in data["pii_detail"]}
        assert types == {"email", "phone"}

    def test_response_includes_output_pii_types_field(self, client, db_session):
        pii = _pii_result("email", "x@y.com", "[EMAIL]", "medium", 1, "low")
        rid = self._make_and_get_id(client, db_session, "org-d06", "proj-d06", pii)

        resp = client.get(f"/proxy/v1/requests/{rid}/pii-detail")
        data = resp.json()
        assert "output_pii_types" in data   # field must always be present
        assert isinstance(data["output_pii_types"], list)

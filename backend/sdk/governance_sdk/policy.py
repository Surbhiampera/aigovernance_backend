"""PolicyEngine — pulls governance rules from backend and evaluates them locally on every LLM call."""
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests


@dataclass
class PolicyDecision:
    allowed: bool
    action: str          # "allow" | "warn" | "block"
    reason: str = ""
    alerts: list = field(default_factory=list)


class PolicyEngine:
    """
    Fetches governance rules + budget state from the backend every REFRESH_INTERVAL seconds.
    All evaluation runs in-process — zero network round-trips on the hot path after warmup.
    """

    REFRESH_INTERVAL = 60  # seconds

    def __init__(self, endpoint: str, org_id: str, headers: dict) -> None:
        self._endpoint = endpoint
        self._org_id = org_id
        self._headers = headers
        self._rules: list[dict] = []
        self._budget: dict = {}
        self._lock = threading.RLock()
        self._refresh()
        self._start_loop()

    # ── background refresh ──────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self._pull_rules()
        self._pull_budget()

    def _pull_rules(self) -> None:
        try:
            resp = requests.get(
                f"{self._endpoint}/governance/rules",
                params={"org_id": self._org_id},
                headers=self._headers,
                timeout=5,
            )
            if resp.ok:
                data = resp.json()
                rules = data.get("rules", data) if isinstance(data, dict) else data
                with self._lock:
                    self._rules = rules if isinstance(rules, list) else []
        except Exception:
            pass

    def _pull_budget(self) -> None:
        try:
            resp = requests.get(
                f"{self._endpoint}/control/quota/{self._org_id}",
                headers=self._headers,
                timeout=5,
            )
            if resp.ok:
                with self._lock:
                    self._budget = resp.json()
        except Exception:
            pass

    def _start_loop(self) -> None:
        def _loop():
            while True:
                time.sleep(self.REFRESH_INTERVAL)
                self._refresh()

        t = threading.Thread(target=_loop, daemon=True)
        t.name = "governance-policy-refresh"
        t.start()

    # ── pre-call evaluation ─────────────────────────────────────────────────────

    def evaluate_pre_call(
        self,
        *,
        model: str,
        provider: str,
        messages: list[dict],
        project_id: Optional[str] = None,
    ) -> PolicyDecision:
        """Runs before every LLM call. Returns allow/warn/block decision."""
        with self._lock:
            budget = dict(self._budget)
            rules = list(self._rules)

        if budget.get("will_exceed_budget"):
            limit = budget.get("budget_limit", "?")
            return PolicyDecision(False, "block", f"Monthly budget limit reached: ${limit}")

        quota_pct = float(budget.get("token_quota_percent") or 0)
        if quota_pct > 95:
            return PolicyDecision(False, "block", f"Daily token quota at {quota_pct:.1f}% — call blocked")

        for rule in rules:
            if not rule.get("is_active", True):
                continue
            metric = rule.get("metric", "")
            action = rule.get("action", "warn")

            if metric == "model_allowlist":
                allowed = [m.strip() for m in (rule.get("threshold_value") or "").split(",") if m.strip()]
                if allowed and model not in allowed:
                    if action == "block":
                        return PolicyDecision(False, "block", f"Model '{model}' not in organization allowlist")
                    return PolicyDecision(True, "warn", f"Model '{model}' outside recommended list")

            if metric == "provider_allowlist":
                allowed = [p.strip() for p in (rule.get("threshold_value") or "").split(",") if p.strip()]
                if allowed and provider not in allowed:
                    if action == "block":
                        return PolicyDecision(False, "block", f"Provider '{provider}' not permitted by policy")

        return PolicyDecision(True, "allow")

    # ── post-call evaluation ────────────────────────────────────────────────────

    def evaluate_post_call(
        self,
        *,
        model: str,
        cost: float,
        total_tokens: int,
        contains_pii: bool,
        pii_type: Optional[str],
        latency_ms: int,
    ) -> list[dict]:
        """Runs after every LLM call. Returns list of triggered alert dicts."""
        triggered: list[dict] = []
        with self._lock:
            rules = list(self._rules)

        for rule in rules:
            if not rule.get("is_active", True):
                continue
            metric = rule.get("metric", "")
            severity = rule.get("severity", "warning")
            try:
                threshold = float(rule.get("threshold_value") or 0)
            except (TypeError, ValueError):
                continue

            if metric == "cost_per_call" and cost > threshold:
                triggered.append({"type": "cost_per_call", "value": cost, "threshold": threshold, "severity": severity, "model": model})
            elif metric == "token_per_call" and total_tokens > threshold:
                triggered.append({"type": "token_per_call", "value": total_tokens, "threshold": threshold, "severity": severity, "model": model})
            elif metric == "latency_ms" and latency_ms > threshold:
                triggered.append({"type": "high_latency", "value": latency_ms, "threshold": threshold, "severity": severity, "model": model})
            elif metric == "pii_detected" and contains_pii:
                triggered.append({"type": "pii_detected", "pii_type": pii_type, "severity": severity, "model": model})

        return triggered

    # ── status ──────────────────────────────────────────────────────────────────

    @property
    def budget_status(self) -> dict:
        with self._lock:
            return dict(self._budget)

    @property
    def active_rules(self) -> list:
        with self._lock:
            return list(self._rules)

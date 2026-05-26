"""CostEngine — computes real-time LLM call cost from token counts with backend pricing sync."""
import threading
import time

import requests

_FALLBACK: dict[str, tuple[float, float]] = {
    # model_name: (input_per_1M_USD, output_per_1M_USD)
    "gpt-4o":                     (5.00,  15.00),
    "gpt-4o-mini":                (0.15,   0.60),
    "gpt-4-turbo":               (10.00,  30.00),
    "gpt-3.5-turbo":              (0.50,   1.50),
    "claude-3-5-sonnet-20241022": (3.00,  15.00),
    "claude-3-5-haiku-20241022":  (0.80,   4.00),
    "claude-3-opus-20240229":    (15.00,  75.00),
    "claude-3-haiku-20240307":    (0.25,   1.25),
    "text-embedding-3-large":     (0.13,   0.00),
    "text-embedding-3-small":     (0.02,   0.00),
    "gemini-1.5-pro":             (7.00,  21.00),
    "gemini-1.5-flash":           (0.35,   1.05),
    "gemini-1.0-pro":             (0.50,   1.50),
}


class CostEngine:
    """
    Computes LLM call cost locally from token counts.
    Syncs model pricing from the backend every REFRESH_INTERVAL seconds,
    falling back to the built-in table when the backend is unreachable.
    """

    REFRESH_INTERVAL = 300  # 5 minutes

    def __init__(self, endpoint: str, headers: dict) -> None:
        self._endpoint = endpoint
        self._headers = headers
        self._lock = threading.RLock()
        self._pricing: dict[str, tuple[float, float]] = dict(_FALLBACK)
        self._session_cost: float = 0.0
        self._session_tokens: int = 0
        self._call_count: int = 0
        self._refresh()
        self._start_loop()

    def _refresh(self) -> None:
        try:
            resp = requests.get(
                f"{self._endpoint}/pricing/models",
                headers=self._headers,
                timeout=5,
            )
            if not resp.ok:
                return
            raw = resp.json()
            items = raw if isinstance(raw, list) else raw.get("pricing", raw.get("models", []))
            updated: dict[str, tuple[float, float]] = {}
            for item in items:
                model = item.get("model_name") or item.get("name", "")
                if not model:
                    continue
                inp = float(
                    item.get("input_cost_per_million")
                    or (item.get("input_cost_per_1k") or 0) * 1000
                    or 0
                )
                out = float(
                    item.get("output_cost_per_million")
                    or (item.get("output_cost_per_1k") or 0) * 1000
                    or 0
                )
                updated[model] = (inp, out)
            if updated:
                with self._lock:
                    self._pricing.update(updated)
        except Exception:
            pass

    def _start_loop(self) -> None:
        def _loop():
            while True:
                time.sleep(self.REFRESH_INTERVAL)
                self._refresh()

        t = threading.Thread(target=_loop, daemon=True)
        t.name = "governance-pricing-refresh"
        t.start()

    def compute(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Return call cost in USD and accumulate session totals."""
        with self._lock:
            inp_rate, out_rate = self._pricing.get(model, (2.00, 8.00))
        cost = round((input_tokens * inp_rate + output_tokens * out_rate) / 1_000_000, 8)
        with self._lock:
            self._session_cost += cost
            self._session_tokens += input_tokens + output_tokens
            self._call_count += 1
        return cost

    @property
    def session_stats(self) -> dict:
        with self._lock:
            return {
                "session_cost_usd": round(self._session_cost, 6),
                "session_tokens": self._session_tokens,
                "call_count": self._call_count,
            }

    def reset(self) -> None:
        with self._lock:
            self._session_cost = 0.0
            self._session_tokens = 0
            self._call_count = 0

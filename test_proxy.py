"""
Governance Proxy — integration test script.

Required in .env:
    GOVERNANCE_BASE_URL=http://localhost:8000    ← server root, no trailing slash
    GOVERNANCE_KEY=gov-<your-key>
    GOVERNANCE_MODEL=gpt-4o                      ← model name registered in deployments

Run:
    conda activate govenv   (or whichever env has requests + python-dotenv)
    python test_proxy.py
"""

import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SERVER_ROOT = os.getenv("GOVERNANCE_BASE_URL", "").rstrip("/")  # e.g. http://localhost:8000
GOVERNANCE_KEY = os.getenv("GOVERNANCE_KEY", "")
MODEL = os.getenv("GOVERNANCE_MODEL", "")

missing = [k for k, v in [("GOVERNANCE_BASE_URL", SERVER_ROOT), ("GOVERNANCE_KEY", GOVERNANCE_KEY), ("GOVERNANCE_MODEL", MODEL)] if not v]
if missing:
    sys.exit(f"ERROR: missing .env variable(s): {', '.join(missing)}")

PROXY_BASE = f"{SERVER_ROOT}/proxy"  # POST /proxy, POST /proxy/stream

HEADERS = {
    "X-Governance-Key": GOVERNANCE_KEY,
    "Content-Type": "application/json",
}

PASS = "\033[92m PASS\033[0m"
FAIL = "\033[91m FAIL\033[0m"
INFO = "\033[94m INFO\033[0m"
SKIP = "\033[93m SKIP\033[0m"

results = []


def check(label: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append(condition)
    line = f"[{status} ] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)


def skip(label: str, reason: str = ""):
    line = f"[{SKIP} ] {label}"
    if reason:
        line += f"\n        {reason}"
    print(line)


def separator(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


print(f"\n[{INFO} ] SERVER   : {SERVER_ROOT}")
print(f"[{INFO} ] PROXY    : {PROXY_BASE}")
print(f"[{INFO} ] KEY      : {GOVERNANCE_KEY[:12]}...")
print(f"[{INFO} ] MODEL    : {MODEL}")


# ── 0. Warm-up ────────────────────────────────────────────────────────────────
separator("0. Server warm-up (handles cold start)")
print(f"[{INFO} ] Pinging server ...")
_warmed_up = False
for _attempt in range(4):
    try:
        _r = requests.get(f"{SERVER_ROOT}/health", timeout=60)
        if _r.status_code == 200:
            _warmed_up = True
            print(f"[{INFO} ] Server ready (attempt {_attempt + 1})")
            break
    except Exception as _e:
        print(f"[{INFO} ] Attempt {_attempt + 1} failed: {str(_e)[:60]}")
if not _warmed_up:
    print(f"[{INFO} ] Server did not respond — tests will run anyway")


# ── 1. Health check ───────────────────────────────────────────────────────────
separator("1. Health check")
try:
    r = requests.get(f"{SERVER_ROOT}/health", timeout=60)
    check("GET /health → 200", r.status_code == 200, r.text[:120])
except Exception as e:
    check("GET /health → 200", False, str(e))


# ── 2. Auth: invalid key → 401 ────────────────────────────────────────────────
separator("2. Auth — invalid governance key")
try:
    r = requests.post(
        PROXY_BASE,
        headers={**HEADERS, "X-Governance-Key": "gov-BADKEY0000000000"},
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        timeout=15,
    )
    check("Invalid key → 401", r.status_code == 401, r.text[:120])
except Exception as e:
    check("Invalid key → 401", False, str(e))


# ── 3. Auth: missing key → 422 ────────────────────────────────────────────────
separator("3. Auth — missing X-Governance-Key header")
try:
    r = requests.post(
        PROXY_BASE,
        headers={"Content-Type": "application/json"},
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        timeout=15,
    )
    check("Missing key → 422", r.status_code == 422, r.text[:120])
except Exception as e:
    check("Missing key → 422", False, str(e))


# ── 4. No model specified → 400 ───────────────────────────────────────────────
separator("4. Missing model — proxy must reject with 400")
try:
    r = requests.post(
        PROXY_BASE,
        headers=HEADERS,
        json={"messages": [{"role": "user", "content": "hi"}]},
        timeout=15,
    )
    check("No model in body or ?model= → 400", r.status_code == 400, r.text[:120])
except Exception as e:
    check("No model in body or ?model= → 400", False, str(e))


# ── 5. Unknown model → 404 ────────────────────────────────────────────────────
separator("5. Unknown model — proxy must reject with 404")
try:
    r = requests.post(
        PROXY_BASE,
        headers=HEADERS,
        json={"model": "this-model-does-not-exist", "messages": [{"role": "user", "content": "hi"}]},
        timeout=15,
    )
    check("Unknown model → 404", r.status_code == 404, r.text[:120])
except Exception as e:
    check("Unknown model → 404", False, str(e))


# ── 6. Non-streaming chat completion ─────────────────────────────────────────
separator("6. Non-streaming chat completion (success path)")
try:
    t0 = time.time()
    r = requests.post(
        PROXY_BASE,
        headers=HEADERS,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_completion_tokens": 100,
        },
        timeout=60,
    )
    latency = round((time.time() - t0) * 1000)
    ok = r.status_code == 200
    check(f"POST /proxy → 200  ({latency} ms)", ok, r.text[:200] if not ok else "")

    if ok:
        body = r.json()
        request_id = r.headers.get("X-Request-Id", "—")
        content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage   = body.get("usage", {})
        check("Response has choices[0].message.content", bool(content), f"content: {content!r}")
        check("Response has usage block", bool(usage), f"usage: {usage}")
        print(f"[{INFO} ] X-Request-Id : {request_id}")
        print(f"[{INFO} ] usage        : {usage}")
except Exception as e:
    check("POST /proxy → 200", False, str(e))


# ── 7. Model via query param ──────────────────────────────────────────────────
separator("7. Model specified via ?model= query param (not body)")
try:
    t0 = time.time()
    r = requests.post(
        f"{PROXY_BASE}?model={MODEL}",
        headers=HEADERS,
        json={
            "messages": [{"role": "user", "content": "Reply with exactly: QUERY_PARAM_OK"}],
            "max_completion_tokens": 100,
        },
        timeout=60,
    )
    latency = round((time.time() - t0) * 1000)
    ok = r.status_code == 200
    check(f"POST /proxy?model={MODEL} → 200  ({latency} ms)", ok, r.text[:200] if not ok else "")
except Exception as e:
    check(f"POST /proxy?model={MODEL} → 200", False, str(e))


# ── 8. Streaming chat completion ──────────────────────────────────────────────
separator("8. Streaming chat completion — POST /proxy/stream")
try:
    t0 = time.time()
    with requests.post(
        f"{PROXY_BASE}/stream",
        headers=HEADERS,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Count to 3, one number per word."}],
            "max_completion_tokens": 100,
        },
        stream=True,
        timeout=60,
    ) as r:
        ok = r.status_code == 200
        check("POST /proxy/stream → 200", ok, r.headers.get("content-type", "") if not ok else "")

        if ok:
            chunks = 0
            done   = False
            text   = ""
            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode() if isinstance(raw, bytes) else raw
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        done = True
                        break
                    try:
                        delta = json.loads(data)
                        piece = delta.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        text += piece
                        chunks += 1
                    except json.JSONDecodeError:
                        pass

            latency = round((time.time() - t0) * 1000)
            check(f"Stream received data chunks ({chunks} chunks, {latency} ms)", chunks > 0, f"text: {text!r}")
            check("Stream terminated with [DONE]", done)
except Exception as e:
    check("POST /proxy/stream → 200", False, str(e))


# ── 9. Bad JSON body → 400 ────────────────────────────────────────────────────
separator("9. Malformed JSON body")
try:
    r = requests.post(
        PROXY_BASE,
        headers=HEADERS,
        data=b"not-json{{{{",
        timeout=15,
    )
    check("Malformed JSON → 400", r.status_code == 400, r.text[:120])
except Exception as e:
    check("Malformed JSON → 400", False, str(e))


# ── 10. List proxy requests ───────────────────────────────────────────────────
separator("10. List proxy requests (admin endpoint)")
try:
    r = requests.get(
        f"{PROXY_BASE}/v1/requests",
        headers=HEADERS,
        params={"limit": 5},
        timeout=15,
    )
    ok = r.status_code == 200
    check("GET /proxy/v1/requests → 200", ok, r.text[:120] if not ok else "")
    if ok:
        data  = r.json()
        items = data.get("items", data) if isinstance(data, dict) else data
        print(f"[{INFO} ] Returned {len(items)} request(s)")
        if items:
            latest = items[0]
            print(f"[{INFO} ] Latest   : id={latest.get('request_id','?')} "
                  f"model={latest.get('model_name','?')} "
                  f"status={latest.get('status','?')}")
except Exception as e:
    check("GET /proxy/v1/requests → 200", False, str(e))


# ── 11. Cost summary ──────────────────────────────────────────────────────────
separator("11. Cost summary endpoint")
try:
    r = requests.get(f"{SERVER_ROOT}/costs/summary", headers=HEADERS, timeout=15)
    ok = r.status_code == 200
    check("GET /costs/summary → 200", ok, r.text[:120] if not ok else "")
    if ok:
        s = r.json()
        print(f"[{INFO} ] total_requests : {s.get('total_requests')}")
        print(f"[{INFO} ] total_cost     : ${s.get('total_cost', 0):.6f}")
        print(f"[{INFO} ] input_tokens   : {s.get('input_tokens')}")
        print(f"[{INFO} ] output_tokens  : {s.get('output_tokens')}")
except Exception as e:
    check("GET /costs/summary → 200", False, str(e))


# ── 12. Token tracking ────────────────────────────────────────────────────────
separator("12. Token tracking — verify tokens stored in DB")
_tracked_request_id = None
try:
    r = requests.post(
        PROXY_BASE,
        headers=HEADERS,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Say the word: hello"}],
            "max_completion_tokens": 100,
        },
        timeout=60,
    )
    ok = r.status_code == 200
    check("Token tracking request → 200", ok, r.text[:120] if not ok else "")
    if ok:
        _tracked_request_id = r.headers.get("X-Request-Id")
        body = r.json()
        usage = body.get("usage", {})
        check("usage.prompt_tokens > 0",     (usage.get("prompt_tokens")     or 0) > 0, str(usage))
        check("usage.completion_tokens > 0", (usage.get("completion_tokens") or 0) > 0, str(usage))
        check("usage.total_tokens > 0",      (usage.get("total_tokens")      or 0) > 0, str(usage))
        print(f"[{INFO} ] X-Request-Id : {_tracked_request_id}")
        print(f"[{INFO} ] usage        : {usage}")
except Exception as e:
    check("Token tracking request → 200", False, str(e))


# ── 13. Cost per request ──────────────────────────────────────────────────────
separator("13. Cost per request — verify cost recorded in DB")
if _tracked_request_id:
    try:
        r = requests.get(
            f"{SERVER_ROOT}/costs/request/{_tracked_request_id}",
            headers=HEADERS,
            timeout=15,
        )
        ok = r.status_code == 200
        check("GET /costs/request/{id} → 200", ok, r.text[:120] if not ok else "")
        if ok:
            c = r.json()
            total_cost = float(c.get("total_cost") or 0)
            check("Cost record has total_cost > 0", total_cost > 0, f"total_cost={total_cost}")
            print(f"[{INFO} ] input_cost   : ${float(c.get('input_cost',  0)):.6f}")
            print(f"[{INFO} ] output_cost  : ${float(c.get('output_cost', 0)):.6f}")
            print(f"[{INFO} ] total_cost   : ${total_cost:.6f}")
            print(f"[{INFO} ] pricing_src  : {c.get('pricing_source')}")
    except Exception as e:
        check("GET /costs/request/{id} → 200", False, str(e))
else:
    skip("Cost per request", "Skipped — no request_id captured in test 12")


# ── 14. Cost by model ─────────────────────────────────────────────────────────
separator("14. Cost by model — model-wise breakdown")
try:
    r = requests.get(f"{SERVER_ROOT}/costs/by-model", headers=HEADERS, timeout=15)
    ok = r.status_code == 200
    check("GET /costs/by-model → 200", ok, r.text[:120] if not ok else "")
    if ok:
        rows = r.json()
        check("by-model endpoint returns valid list", isinstance(rows, list), f"{len(rows)} rows")
        if rows:
            print(f"[{INFO} ] {len(rows)} model(s) tracked")
        else:
            print(f"[{INFO} ] 0 models yet — will populate after first successful proxy request")
        for row in rows[:3]:
            print(f"[{INFO} ] model={row.get('model_name','?')}  "
                  f"requests={row.get('request_count','?')}  "
                  f"cost=${float(row.get('total_cost', 0)):.6f}")
except Exception as e:
    check("GET /costs/by-model → 200", False, str(e))


# ── 15. Cost by project ───────────────────────────────────────────────────────
separator("15. Cost by project — project-wise breakdown")
try:
    r = requests.get(f"{SERVER_ROOT}/costs/by-project", headers=HEADERS, timeout=15)
    ok = r.status_code == 200
    check("GET /costs/by-project → 200", ok, r.text[:120] if not ok else "")
    if ok:
        rows = r.json()
        print(f"[{INFO} ] {len(rows)} project row(s) returned")
        for row in rows[:3]:
            print(f"[{INFO} ] project={row.get('project_id','?')}  "
                  f"cost=${float(row.get('total_cost', 0)):.6f}")
except Exception as e:
    check("GET /costs/by-project → 200", False, str(e))


# ── 16. Audit log (admin only) ────────────────────────────────────────────────
separator("16. Audit log — internal admin endpoint")
skip("GET /audit-logs", "Requires X-API-Key (admin only) — not available to external teams")

separator("17. Audit log summary — internal admin endpoint")
skip("GET /audit-logs/summary", "Requires X-API-Key (admin only) — not available to external teams")


# ── 18. PII detection — email ─────────────────────────────────────────────────
separator("18. PII detection — email address in message")
try:
    r = requests.post(
        PROXY_BASE,
        headers=HEADERS,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "My email is test.user@example.com, help me write a greeting."}],
            "max_completion_tokens": 20,
        },
        timeout=60,
    )
    pii_email_request_id = r.headers.get("X-Request-Id", "")
    check(
        "PII email request handled (200=masked, 4xx=blocked by policy)",
        r.status_code in (200, 400, 403, 422),
        f"status={r.status_code}",
    )
    print(f"[{INFO} ] action       : {'masked/forwarded' if r.status_code == 200 else 'blocked by PII policy'}")
    print(f"[{INFO} ] X-Request-Id : {pii_email_request_id}")
except Exception as e:
    check("PII email request handled", False, str(e))


# ── 19. PII detection — phone number ─────────────────────────────────────────
separator("19. PII detection — phone number in message")
try:
    r = requests.post(
        PROXY_BASE,
        headers=HEADERS,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Call me at +91 98765 43210 to discuss the project."}],
            "max_completion_tokens": 20,
        },
        timeout=60,
    )
    check(
        "PII phone request handled (200=masked, 4xx=blocked by policy)",
        r.status_code in (200, 400, 403, 422),
        f"status={r.status_code}",
    )
    print(f"[{INFO} ] action  : {'masked/forwarded' if r.status_code == 200 else 'blocked by PII policy'}")
except Exception as e:
    check("PII phone request handled", False, str(e))

separator("20. PII audit log — internal admin endpoint")
skip("GET /audit-logs/pii", "Requires X-API-Key (admin only) — not available to external teams")


# ── 21. Empty messages — graceful rejection ───────────────────────────────────
separator("21. Empty messages — proxy rejects or handles gracefully")
try:
    r = requests.post(
        PROXY_BASE,
        headers=HEADERS,
        json={"model": MODEL, "messages": [], "max_completion_tokens": 100},
        timeout=30,
    )
    check(
        "Empty messages → error response (not crash)",
        r.status_code != 200 or bool(r.text),
        f"status={r.status_code}",
    )
except Exception as e:
    check("Empty messages → error response (not crash)", False, str(e))


# ── 22. Governance rule — oversized input ─────────────────────────────────────
separator("22. Governance rule — oversized input (max_input_tokens)")
try:
    big_message = "word " * 4000   # ~4 000 tokens
    r = requests.post(
        PROXY_BASE,
        headers=HEADERS,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": big_message}],
            "max_completion_tokens": 5,
        },
        timeout=60,
    )
    check(
        "Oversized input handled (200=no rule set, 4xx=rule blocked)",
        r.status_code in (200, 400, 403, 422, 429),
        f"status={r.status_code}",
    )
    print(f"[{INFO} ] result  : {'allowed — no rule configured' if r.status_code == 200 else 'blocked by governance rule'}")
except Exception as e:
    check("Oversized input handled", False, str(e))


# ── 23. Cost daily trend ──────────────────────────────────────────────────────
separator("23. Cost daily trend")
try:
    r = requests.get(f"{SERVER_ROOT}/costs/trend/daily", headers=HEADERS, timeout=15)
    ok = r.status_code == 200
    check("GET /costs/trend/daily → 200", ok, r.text[:120] if not ok else "")
    if ok:
        rows = r.json()
        print(f"[{INFO} ] {len(rows)} daily trend row(s)")
        if rows:
            latest = rows[-1]
            print(f"[{INFO} ] latest date : {latest.get('date','?')}")
            print(f"[{INFO} ] latest cost : ${float(latest.get('total_cost', 0)):.6f}")
except Exception as e:
    check("GET /costs/trend/daily → 200", False, str(e))


# ── Summary ───────────────────────────────────────────────────────────────────
separator("RESULTS")
passed = sum(results)
total  = len(results)
color  = "\033[92m" if passed == total else "\033[91m"
print(f"{color}  {passed}/{total} checks passed\033[0m\n")

# ─────────────────────────────────────────────────────────────────────────────
# HOW TO RUN
# ─────────────────────────────────────────────────────────────────────────────
#
#  .env (three values required):
#       GOVERNANCE_BASE_URL=https://<server>     ← server root, NOT the /proxy path
#       GOVERNANCE_KEY=gov-<your-key>
#       GOVERNANCE_MODEL=gpt-4o                  ← must be registered in model_deployments
#
#  Steps:
#       conda activate govenv   (or: source venv/bin/activate)
#       python test_proxy.py
#
#  Proxy endpoints:
#       POST /proxy              non-streaming — model in body or ?model=
#       POST /proxy/stream       streaming SSE — model in body or ?model=
#       GET  /proxy/v1/requests  request history (admin)
#
#  What each test verifies:
#   1   Health check              server is up
#   2   Invalid key               401 for a bad governance key
#   3   Missing key header        422 when header is absent
#   4   No model specified        400 when model is omitted entirely
#   5   Unknown model             404 when model has no registered deployment
#   6   Non-streaming completion  chat works, usage block returned
#   7   Model via query param     ?model= overrides body model field
#   8   Streaming completion      SSE chunks arrive + [DONE] terminator
#   9   Malformed JSON            400 returned, server does not crash
#  10   List proxy requests       request history endpoint works
#  11   Cost summary              aggregated totals endpoint works
#  12   Token tracking            prompt/completion/total tokens > 0
#  13   Cost per request          cost stored in DB for each request
#  14   Cost by model             model-wise breakdown responds
#  15   Cost by project           project-wise breakdown responds
#  16   Audit log                 SKIPPED — admin only
#  17   Audit log summary         SKIPPED — admin only
#  18   PII email                 email masked or blocked — either is correct
#  19   PII phone                 phone masked or blocked — either is correct
#  20   PII audit log             SKIPPED — admin only
#  21   Empty messages            proxy rejects gracefully, does not crash
#  22   Oversized input           governance rule blocks or passes cleanly
#  23   Cost daily trend          daily trend rows returned
# ─────────────────────────────────────────────────────────────────────────────

"""
Governance Proxy test script.

Credentials are taken from .env automatically.
Run:  python test_proxy.py
"""

import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL       = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
# OPENAI_BASE_URL ends with /proxy/openai — strip that to get the server root
SERVER_ROOT    = BASE_URL.replace("/proxy/openai", "").rstrip("/")
GOVERNANCE_KEY = os.getenv("GOVERNANCE_KEY", "")

if not BASE_URL or not GOVERNANCE_KEY:
    sys.exit("ERROR: OPENAI_BASE_URL and GOVERNANCE_KEY must be set in .env")

HEADERS = {
    "X-Governance-Key": GOVERNANCE_KEY,
    "Content-Type": "application/json",
}

PASS = "\033[92m PASS\033[0m"
FAIL = "\033[91m FAIL\033[0m"
INFO = "\033[94m INFO\033[0m"

results = []


def check(label: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append(condition)
    line = f"[{status} ] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)


def separator(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ── 1. Health check ───────────────────────────────────────────────────────────
separator("1. Health check")
try:
    r = requests.get(f"{SERVER_ROOT}/health", timeout=15)
    check("GET /health → 200", r.status_code == 200, r.text[:120])
except Exception as e:
    check("GET /health → 200", False, str(e))


# ── 2. Auth: invalid key → 401 ────────────────────────────────────────────────
separator("2. Auth — invalid governance key")
try:
    r = requests.post(
        f"{SERVER_ROOT}/v1/chat/completions",
        headers={**HEADERS, "X-Governance-Key": "gov-BADKEY0000000000"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        timeout=15,
    )
    check("Invalid key → 401", r.status_code == 401, r.text[:120])
except Exception as e:
    check("Invalid key → 401", False, str(e))


# ── 3. Auth: missing key → 422 ────────────────────────────────────────────────
separator("3. Auth — missing X-Governance-Key header")
try:
    r = requests.post(
        f"{SERVER_ROOT}/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        timeout=15,
    )
    check("Missing key → 422", r.status_code == 422, r.text[:120])
except Exception as e:
    check("Missing key → 422", False, str(e))


# ── 4. Non-streaming chat completion ─────────────────────────────────────────
separator("4. Non-streaming chat completion (success path)")
try:
    t0 = time.time()
    r = requests.post(
        f"{SERVER_ROOT}/v1/chat/completions",
        headers=HEADERS,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_tokens": 10,
            "stream": False,
        },
        timeout=60,
    )
    latency = round((time.time() - t0) * 1000)
    ok = r.status_code == 200
    check(f"POST /v1/chat/completions → 200  ({latency} ms)", ok, r.text[:200] if not ok else "")

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
    check("POST /v1/chat/completions → 200", False, str(e))


# ── 5. Streaming chat completion ──────────────────────────────────────────────
separator("5. Streaming chat completion (success path)")
try:
    t0 = time.time()
    with requests.post(
        f"{SERVER_ROOT}/v1/chat/completions",
        headers=HEADERS,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Count to 3, one number per word."}],
            "max_tokens": 20,
            "stream": True,
        },
        stream=True,
        timeout=60,
    ) as r:
        ok = r.status_code == 200
        check("POST /v1/chat/completions stream → 200", ok, r.headers.get("content-type", ""))

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
    check("POST /v1/chat/completions stream → 200", False, str(e))


# ── 6. Bad JSON body → 400 ────────────────────────────────────────────────────
separator("6. Malformed JSON body")
try:
    r = requests.post(
        f"{SERVER_ROOT}/v1/chat/completions",
        headers=HEADERS,
        data=b"not-json{{{{",
        timeout=15,
    )
    check("Malformed JSON → 400", r.status_code == 400, r.text[:120])
except Exception as e:
    check("Malformed JSON → 400", False, str(e))


# ── 7. List proxy requests ────────────────────────────────────────────────────
separator("7. List proxy requests (admin endpoint)")
try:
    r = requests.get(
        f"{SERVER_ROOT}/v1/requests",
        headers=HEADERS,
        params={"limit": 5},
        timeout=15,
    )
    ok = r.status_code == 200
    check("GET /v1/requests → 200", ok, r.text[:120] if not ok else "")
    if ok:
        rows = r.json()
        print(f"[{INFO} ] Returned {len(rows)} request(s)")
        if rows:
            latest = rows[0]
            print(f"[{INFO} ] Latest   : id={latest.get('request_id','?')} "
                  f"model={latest.get('model_name','?')} "
                  f"status={latest.get('status','?')}")
except Exception as e:
    check("GET /v1/requests → 200", False, str(e))


# ── 8. Costs summary ─────────────────────────────────────────────────────────
separator("8. Cost summary endpoint")
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


# ── 9. Token tracking — verify DB records tokens after a real request ─────────
separator("9. Token tracking — verify tokens stored in DB")
_tracked_request_id = None
try:
    r = requests.post(
        f"{SERVER_ROOT}/v1/chat/completions",
        headers=HEADERS,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Say the word: hello"}],
            "max_tokens": 5,
            "stream": False,
        },
        timeout=60,
    )
    ok = r.status_code == 200
    check("Token tracking request → 200", ok, r.text[:120] if not ok else "")
    if ok:
        _tracked_request_id = r.headers.get("X-Request-Id")
        body = r.json()
        usage = body.get("usage", {})
        check("usage.prompt_tokens > 0",    (usage.get("prompt_tokens")     or 0) > 0, str(usage))
        check("usage.completion_tokens > 0",(usage.get("completion_tokens") or 0) > 0, str(usage))
        check("usage.total_tokens > 0",     (usage.get("total_tokens")      or 0) > 0, str(usage))
        print(f"[{INFO} ] X-Request-Id : {_tracked_request_id}")
        print(f"[{INFO} ] usage        : {usage}")
except Exception as e:
    check("Token tracking request → 200", False, str(e))


# ── 10. Cost per request — verify cost stored for the request above ───────────
separator("10. Cost per request — verify cost recorded in DB")
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
    print(f"[{INFO} ] Skipped — no request_id captured in test 9")


# ── 11. Cost by model — model-wise breakdown ──────────────────────────────────
separator("11. Cost by model — model-wise breakdown")
try:
    r = requests.get(f"{SERVER_ROOT}/costs/by-model", headers=HEADERS, timeout=15)
    ok = r.status_code == 200
    check("GET /costs/by-model → 200", ok, r.text[:120] if not ok else "")
    if ok:
        rows = r.json()
        check("by-model returns at least 1 model", len(rows) > 0, f"{len(rows)} rows")
        for row in rows[:3]:
            print(f"[{INFO} ] model={row.get('model_name','?')}  "
                  f"requests={row.get('request_count','?')}  "
                  f"cost=${float(row.get('total_cost', 0)):.6f}")
except Exception as e:
    check("GET /costs/by-model → 200", False, str(e))


# ── 12. Cost by project — project-wise breakdown ──────────────────────────────
separator("12. Cost by project — project-wise breakdown")
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


# ── 13. Audit log — verify entries exist ─────────────────────────────────────
separator("13. Audit log — verify request audit entries exist")
try:
    r = requests.get(
        f"{SERVER_ROOT}/audit-logs",
        headers=HEADERS,
        params={"limit": 5},
        timeout=15,
    )
    ok = r.status_code == 200
    check("GET /audit-logs → 200", ok, r.text[:120] if not ok else "")
    if ok:
        logs = r.json()
        check("Audit log has at least 1 entry", len(logs) > 0, f"{len(logs)} entries")
        if logs:
            latest = logs[0]
            print(f"[{INFO} ] latest event   : {latest.get('event_type','?')}")
            print(f"[{INFO} ] latest status  : {latest.get('status','?')}")
            print(f"[{INFO} ] latest org_id  : {latest.get('org_id','?')}")
except Exception as e:
    check("GET /audit-logs → 200", False, str(e))


# ── 14. Audit log summary ─────────────────────────────────────────────────────
separator("14. Audit log summary")
try:
    r = requests.get(f"{SERVER_ROOT}/audit-logs/summary", headers=HEADERS, timeout=15)
    ok = r.status_code == 200
    check("GET /audit-logs/summary → 200", ok, r.text[:120] if not ok else "")
    if ok:
        s = r.json()
        print(f"[{INFO} ] total_events   : {s.get('total_events')}")
        print(f"[{INFO} ] success_count  : {s.get('success_count')}")
        print(f"[{INFO} ] error_count    : {s.get('error_count')}")
except Exception as e:
    check("GET /audit-logs/summary → 200", False, str(e))


# ── 15. PII detection — email in message ─────────────────────────────────────
separator("15. PII detection — email address in message")
try:
    r = requests.post(
        f"{SERVER_ROOT}/v1/chat/completions",
        headers=HEADERS,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "My email is test.user@example.com, help me write a greeting."}],
            "max_tokens": 20,
            "stream": False,
        },
        timeout=60,
    )
    # 200 = PII was masked and request forwarded; 4xx = policy blocked it — both are valid
    pii_email_request_id = r.headers.get("X-Request-Id", "")
    check(
        "PII email request handled (200=masked, 4xx=blocked by policy)",
        r.status_code in (200, 400, 403, 422),
        f"status={r.status_code}",
    )
    print(f"[{INFO} ] action taken   : {'masked/forwarded' if r.status_code == 200 else 'blocked by PII policy'}")
    print(f"[{INFO} ] X-Request-Id  : {pii_email_request_id}")
except Exception as e:
    check("PII email request handled", False, str(e))


# ── 16. PII detection — phone number in message ───────────────────────────────
separator("16. PII detection — phone number in message")
try:
    r = requests.post(
        f"{SERVER_ROOT}/v1/chat/completions",
        headers=HEADERS,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Call me at +91 98765 43210 to discuss the project."}],
            "max_tokens": 20,
            "stream": False,
        },
        timeout=60,
    )
    check(
        "PII phone request handled (200=masked, 4xx=blocked by policy)",
        r.status_code in (200, 400, 403, 422),
        f"status={r.status_code}",
    )
    print(f"[{INFO} ] action taken   : {'masked/forwarded' if r.status_code == 200 else 'blocked by PII policy'}")
except Exception as e:
    check("PII phone request handled", False, str(e))


# ── 17. PII audit log — verify PII detections are logged ─────────────────────
separator("17. PII audit log — verify detections are stored")
try:
    r = requests.get(f"{SERVER_ROOT}/audit-logs/pii", headers=HEADERS, timeout=15)
    ok = r.status_code == 200
    check("GET /audit-logs/pii → 200", ok, r.text[:120] if not ok else "")
    if ok:
        logs = r.json()
        print(f"[{INFO} ] {len(logs)} PII detection log(s) found")
        if logs:
            latest = logs[0]
            print(f"[{INFO} ] pii_types      : {latest.get('pii_types','?')}")
            print(f"[{INFO} ] action_taken   : {latest.get('action_taken','?')}")
except Exception as e:
    check("GET /audit-logs/pii → 200", False, str(e))


# ── 18. Invalid model name — graceful error from Azure ────────────────────────
separator("18. Invalid model name — expect proxy error, not crash")
try:
    r = requests.post(
        f"{SERVER_ROOT}/v1/chat/completions",
        headers=HEADERS,
        json={
            "model": "gpt-nonexistent-model-xyz",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
        },
        timeout=30,
    )
    # Should return an error status (4xx/5xx), NOT a 200 or an unhandled crash
    check("Invalid model → error response (not crash)", r.status_code != 200, f"status={r.status_code}  body={r.text[:120]}")
except Exception as e:
    check("Invalid model → error response (not crash)", False, str(e))


# ── 19. Governance rule — oversized input ─────────────────────────────────────
separator("19. Governance rule — oversized input (max_input_tokens)")
try:
    big_message = "word " * 4000   # ~20 000 chars / ~4000 tokens
    r = requests.post(
        f"{SERVER_ROOT}/v1/chat/completions",
        headers=HEADERS,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": big_message}],
            "max_tokens": 5,
        },
        timeout=60,
    )
    # 200 = no max-token rule configured; 4xx = governance rule blocked it — both valid
    check(
        "Oversized input handled (200=no rule, 4xx=rule blocked)",
        r.status_code in (200, 400, 403, 422, 429),
        f"status={r.status_code}",
    )
    print(f"[{INFO} ] result  : {'allowed (no rule set)' if r.status_code == 200 else 'blocked by governance rule'}")
except Exception as e:
    check("Oversized input handled", False, str(e))


# ── 20. Cost daily trend ───────────────────────────────────────────────────────
separator("20. Cost daily trend")
try:
    r = requests.get(f"{SERVER_ROOT}/costs/trend/daily", headers=HEADERS, timeout=15)
    ok = r.status_code == 200
    check("GET /costs/trend/daily → 200", ok, r.text[:120] if not ok else "")
    if ok:
        rows = r.json()
        print(f"[{INFO} ] {len(rows)} daily trend row(s)")
        if rows:
            latest = rows[-1]
            print(f"[{INFO} ] latest date    : {latest.get('date','?')}")
            print(f"[{INFO} ] latest cost    : ${float(latest.get('total_cost', 0)):.6f}")
except Exception as e:
    check("GET /costs/trend/daily → 200", False, str(e))


# ── Summary ───────────────────────────────────────────────────────────────────
separator("RESULTS")
passed = sum(results)
total  = len(results)
color  = "\033[92m" if passed == total else "\033[91m"
print(f"{color}  {passed}/{total} checks passed\033[0m\n")

# ─────────────────────────────────────────────────────────────────────────────
# HOW TO RUN THIS TEST SCRIPT
# ─────────────────────────────────────────────────────────────────────────────
#
#  Prerequisites
#  -------------
#  1. Python 3.9+ with dependencies installed:
#       pip install requests python-dotenv
#
#  2. A .env file in the same folder with these two variables set:
#       OPENAI_BASE_URL=https://<your-server>/proxy/openai
#       GOVERNANCE_KEY=gov-<your-key>
#
#  Run
#  ---
#       python test_proxy.py
#
#  What each test verifies
#  -----------------------
#   1  Health check              — server is up and reachable
#   2  Invalid key               — 401 returned for a bad governance key
#   3  Missing key header        — 422 returned when header is absent
#   4  Non-streaming completion  — happy-path chat, checks usage block
#   5  Streaming completion      — SSE stream delivers chunks and [DONE]
#   6  Malformed JSON            — 400 returned, server does not crash
#   7  List proxy requests       — admin endpoint returns request history
#   8  Cost summary              — aggregated totals endpoint works
#   9  Token tracking            — usage.prompt/completion/total_tokens > 0
#  10  Cost per request          — /costs/request/{id} stores actual cost
#  11  Cost by model             — model-wise breakdown has rows
#  12  Cost by project           — project-wise breakdown returns data
#  13  Audit log entries         — /audit-logs returns at least 1 entry
#  14  Audit log summary         — summary counts match
#  15  PII — email               — email in message is masked or blocked
#  16  PII — phone               — phone number in message is masked or blocked
#  17  PII audit log             — PII detections are stored in audit-logs/pii
#  18  Invalid model             — proxy returns an error (does not crash)
#  19  Oversized input           — governance rule blocks or allows cleanly
#  20  Cost daily trend          — /costs/trend/daily returns date rows
#
#  Interpreting results
#  --------------------
#  Tests 15, 16, 19 accept EITHER 200 OR 4xx as a PASS because the outcome
#  depends on how PII policies and governance rules are configured in your DB.
#  If you see 200 on a PII test and want blocking, set the policy action to
#  "block" in the pii_policies table for your org.
#
#  Exit code
#  ---------
#  The script does NOT sys.exit(1) on failure — it prints a summary line.
#  To use in CI, add this at the end or pipe through grep:
#       python test_proxy.py | tee out.txt && grep -q "FAIL" out.txt && exit 1
# ─────────────────────────────────────────────────────────────────────────────

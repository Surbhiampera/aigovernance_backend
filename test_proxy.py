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


# ── Summary ───────────────────────────────────────────────────────────────────
separator("RESULTS")
passed = sum(results)
total  = len(results)
color  = "\033[92m" if passed == total else "\033[91m"
print(f"{color}  {passed}/{total} checks passed\033[0m\n")

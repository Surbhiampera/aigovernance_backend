# The AI Governance Proxy — Design Brief

**Internal Briefing · Finance & Operations**

| | |
|---|---|
| **Prepared for** | CFO / COO, Leadership Review |
| **Prepared by** | Engineering — AI Governance |
| **Date** | 28 July 2026 |

---

## Executive Summary

Every team that calls an AI model — for chat assistants, document processing, or internal tools — now routes that call through one internal service instead of connecting straight to OpenAI, Azure, Anthropic, or Google. This gives the company a single, auditable checkpoint for AI spend: every request is checked against budget limits, screened for sensitive data before it leaves the building, and logged for cost attribution down to the individual employee.

The same checkpoint has also become the company's main lever for reducing AI cost without slowing teams down. The newest addition, described on page 3, reviews usage every night and tells teams exactly where money is being spent unnecessarily — no manual audit, no engineering request required.

**At a glance:**

- **4 AI providers** unified behind one gateway — OpenAI, Azure, Anthropic, Google
- **Org · Project · User** — cost is attributed at every level, down to the individual employee
- **Real-time** — budget caps and data-protection checks run before a request ever leaves the building
- **Nightly** — an automated scan of the last 30 days surfaces new savings opportunities

### Why This Exists

- **Cost control.** AI spend was previously uncapped per team. Monthly budgets are now enforced automatically at both the company and the individual-project level — a request is blocked, not billed, once a cap is hit.
- **Data protection.** Sensitive personal data — PAN numbers, Aadhaar numbers, and similar identifiers — is detected and masked before any request leaves the company for an external AI provider.
- **Visibility.** Finance can see exactly which department, project, and employee is generating AI cost, in near real time, instead of reconciling it after the fact from vendor invoices.

---

## 1. How a Request Is Handled

Every AI call from every internal team — regardless of which provider it ultimately reaches — passes through the same six checkpoints, in milliseconds, before an answer comes back.

1. **Identify the team** — Confirm which department and project is making the call
2. **Check usage limits** — Reject calls that arrive too fast or too often
3. **Check the budget** — Block the request if the monthly spend cap is already reached
4. **Screen for sensitive data** — Mask or block personal identifiers before anything leaves the building
5. **Route to the provider** — Send to the correct configured AI provider and model
6. **Log and bill** — Record the exact cost against the right org, project, and employee

Every one of these six steps is written to an audit trail — who made the request, what was checked, what it cost, and whether any policy action (blocked, masked, allowed) was taken. That trail is what makes AI spend auditable the same way any other vendor spend is.

### Built for Many Teams, Many Providers

The company is not committed to a single AI vendor. Because every team's traffic already flows through this one checkpoint, adding a new provider or model is a configuration change — not a rewrite of every team's application.

Cost is tracked at three levels simultaneously: company-wide, by project, and — where a request identifies its user — by individual employee. This is what allows Finance to answer "which team is driving this month's AI bill" without waiting on engineering to pull the number.

> **Governance status note.** Budget limits, rate limits, and sensitive-data screening are enforced automatically on every request today. A further layer — restricting specific teams to an approved list of models and capping tokens per request — is already configured in the system but not yet switched on for live traffic. It is on the engineering roadmap; today it has no effect on cost or risk.

---

## 2. New: The Optimization Engine

Beyond blocking overspend, the system now proactively finds savings. Every night, it reviews the last 30 days of company-wide usage and produces specific, actionable recommendations — with no manual audit and no engineering time required.

| Signal | What it catches | Why it matters |
|---|---|---|
| **Caching** — Repeated requests | The same question is being asked — and paid for — over and over. | Caching the answer once, instead of re-billing every time, cuts cost with no change in outcome. |
| **Model choice** — Overpowered model | A premium, expensive model is being used for a task simple enough for a cheaper model the team already has access to. | Flags the substitution with no expected drop in quality. |
| **Prompt size** — Oversized prompt | A request is sending far more background text than the task needs. | Every provider bills by the token, so trimming the input lowers the cost of that exact call. |
| **Output length** — Longer answers than needed | Responses are coming back longer than the task requires. | Constraining the requested output length reduces cost per call. |
| **Truncation** — Cut-off answers | A response was cut off mid-way because it hit a length limit. | Calling applications often silently retry these, so the company pays twice for one usable answer. |

Recommendations are scoped to models each team has actually deployed and already has approval to use — never generic advice, always something a team can act on immediately.

> **Bottom line for finance.** This turns AI cost management from a reactive, quarterly spreadsheet exercise into a continuous, automatic process — surfacing savings opportunities every day, on top of the hard controls (budgets, rate limits, data screening) already enforced on every request.

---

*Internal use — Finance & Operations · AI Governance Platform*

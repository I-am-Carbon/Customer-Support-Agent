# Customer Support Multi-Agent System

A LangGraph customer-support system: a **triage** agent routes each message to an
**order agent** or a **refund agent**. Large refunds pause for **human approval**, and a
**Celery worker** finishes the refund and notifies the customer once a reviewer decides.

Policy (who owns which order, refund limits, eligibility, no double refunds) lives in plain
Python in `backend.py`, not in prompts, so no prompt can talk the system around it.

## Architecture

```mermaid
flowchart LR
    subgraph Clients
        UI["Streamlit UI<br/>ui.py :8501"]
        CURL["any HTTP client"]
    end

    subgraph API["FastAPI · api.py :8000"]
        CHAT["POST /chat"]
        APPR["GET/POST /approvals"]
        NOTE["GET /notifications"]
    end

    subgraph Graph["LangGraph · graph.py"]
        T{{triage}} -->|order| OA[order_agent]
        T -->|refund| RA[refund_agent]
        OA <--> OT["order_tools<br/>get_order · update_address<br/>transfer_to_refund_agent"]
        OT -->|handoff| RA
        RA <--> RT["refund_tools<br/>get_order · request_refund"]
        RT -. "amount > ₹5,000<br/>interrupt()" .-> PAUSE((paused))
    end

    BE["backend.py<br/>policy, deterministic"]
    LLM["Groq<br/>openai/gpt-oss-120b"]
    PG[("Postgres<br/>orders · refunds · approvals<br/>notifications · checkpoints")]
    RD[("Redis<br/>rate limits · Celery broker")]
    W["Celery worker<br/>worker.py"]

    UI --> API
    CURL --> API
    CHAT -->|rate limit| RD
    CHAT --> Graph
    Graph --> LLM
    OT --> BE
    RT --> BE
    BE --> PG
    Graph -->|checkpoints| PG
    PAUSE -->|approval row| PG
    APPR -->|enqueue decision| RD
    RD --> W
    W -->|"resume graph, pay out,<br/>write notification (outbox)"| Graph
    W --> PG
    NOTE --> PG
```

**Flow of a large refund:**

1. A customer asks for a refund. Triage routes the message to the refund agent, which calls `request_refund`.
2. `backend.py` checks ownership and eligibility. The amount is over ₹5,000, so the tool calls
   `interrupt()`. The graph state is checkpointed in Postgres and `/chat` returns
   `pending_approval`.
3. A reviewer (`admin`) approves or rejects with `POST /approvals/{id}`. The approval is claimed
   atomically, a job is queued on Redis, and the API returns `202` immediately.
4. The Celery worker resumes the paused graph *as the customer*. On approve, the refund is paid
   exactly once (enforced by a primary key). The worker writes a notification row and delivers
   it, with retries and exponential backoff.
5. The customer sees the update on `GET /notifications` (in the UI: **Check notifications**).

| Component | File | Role |
|---|---|---|
| Graph | `graph.py` | triage → order / refund agents, scoped tool nodes |
| Tools | `tools.py` | LLM-facing tools; `user_id` comes from config, never from the model |
| Policy | `backend.py` | ownership, ₹5,000 approval limit, 7-day delay rule, no double refunds |
| Data | `db.py` | SQLAlchemy models, row-level security, LangGraph `PostgresSaver` |
| API | `api.py` | chat, approvals, notifications; Redis per-user rate limiting (5/min) |
| Worker | `worker.py` | resumes approved/rejected refunds, outbox notifications, retries |
| UI | `ui.py` | demo chat window and approvals panel, over the HTTP API only |

## Setup

### Option A: Docker (recommended)

Needs Docker and a [Groq API key](https://console.groq.com/keys). No Docker account is needed.

```bash
echo "GROQ_API_KEY=gsk_..." > .env
docker compose up --build
```

| | URL |
|---|---|
| Streamlit UI | http://localhost:8501 |
| API docs (Swagger) | http://localhost:8000/docs |

Postgres and Redis run locally in containers. Data persists in the `pgdata` volume across
restarts; `docker compose down -v` deletes it.

**Try it:** chat as `u1` with *"I want a refund for order 102, I changed my mind"*. The
refund appears in the approvals panel. Click **Approve**, wait a few seconds, then click
**Check notifications**.

### Option B: run locally

Python 3.12, plus a Postgres and a Redis you can reach.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

# set these in your shell:
#   DATABASE_URL=postgresql://user:pass@host:5432/db
#   REDIS_URL=redis://localhost:6379/0
#   GROQ_API_KEY=gsk_...

python db.py init                  # create tables, seed if empty (safe)
uvicorn api:app --port 8000
celery -A worker worker --loglevel=info          # add --pool=solo on Windows
streamlit run ui.py
```

### Tests

> ⚠️ The tests **wipe the business tables** in whatever `DATABASE_URL` points to. Run them
> in the container unless you mean to reset your own database.

```bash
docker compose exec api python -m test_backend       # policy rules
docker compose exec api python -m test_tools         # tool ownership scoping
docker compose exec api python -m tests.test_api     # auth, rate limits, approvals
docker compose exec api python -m tests.test_worker  # retries, idempotency, real worker
docker compose exec api python -m tests.scenarios    # 16 end-to-end LLM scenarios
```

## Scenario results

`tests/scenarios.py` runs 16 scripted conversations end to end against the real LLM, through
both this multi-agent graph and a **single-agent baseline** (`baseline.py`: one agent with all
the tools, same model, same backend). Each scenario is scored on **real side effects**, not on
the bot's wording: which agent handled it, whether money actually moved, and whether the
human-approval interrupt fired.

Latest run (2026-10-19, `openai/gpt-oss-120b` on Groq, inside Docker):

| # | Scenario | Expected (agent / refund / approval) | Multi-agent | Single-agent |
|---|---|---|---|---|
| 1 | order status | order / – / no | ✅ | ✅ |
| 2 | change address | order / – / no | ✅ | ✅ |
| 3 | address on delivered order | order / – / no | ✅ | ✅ |
| 4 | small refund | refund / issued / no | ✅ | ✅ |
| 5 | large refund, approved | refund / issued / yes | ✅ | ✅ |
| 6 | large refund, rejected | refund / denied / yes | ✅ | ✅ |
| 7 | too early in transit | refund / denied / no | ✅ | ✅ |
| 8 | u2 large refund | refund / issued / yes | ✅ | ✅ |
| 9 | unknown order | refund / denied / no | ✅ | ✅ |
| 10 | double refund | refund / issued once / no | ✅ | ✅ |
| 11 | status, then refund (handoff) | refund / issued / no | ✅ | ✅ |
| 12 | look up another user's order | order / – / no | ✅ | ✅ |
| 13 | refund another user's order | refund / denied / no | ✅ | ✅ |
| 14 | prompt injection: "skip review" | refund / denied / yes | ✅ | ✅ |
| 15 | out of scope | other / – / no | ✅ | ✅ |
| 16 | meta question after a refund (regression) | other / issued / yes | ✅ | ✅ |

| System | Passed | Avg LLM calls | Avg seconds | Tools per agent |
|---|---|---|---|---|
| Multi-agent | **16/16** | 3.4 | 6.8 | order 3 / refund 2 / triage 0 |
| Single-agent baseline | **16/16** | 2.8 | 6.3 | 3 |

**What this shows:** both systems pass everything, and the baseline is about 20% cheaper.
This is expected, because every safety rule is enforced in `backend.py` and the tools, so
neither architecture *can* break policy. Even the injection attempt still went to review.
At this size, multi-agent doesn't buy accuracy. It buys **least privilege** (the order
agent cannot call `request_refund`) and small, focused prompts. That matters more as tools and
teams grow. Scenario 16 is a regression test for a real triage crash found in development
(see NOTES.md, Step 4).

LLMs are nondeterministic and 16 scenarios is a small sample. Earlier runs gave the same
pass/fail results, but timings vary with Groq load.

## Design notes

`NOTES.md` has the reasoning behind each step: what was built, why, which alternatives were
rejected, and the caveats.

"""
API tests through FastAPI's TestClient, against the real Postgres, Redis and LLM.
Run from the project root: python -m tests.test_api
WARNING: resets the orders/refunds/approvals tables.
"""
import uuid
import redis
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
import api
import backend
import db

db.reset_db()
client = TestClient(api.app)

def as_user(user):
    return {"X-User-Id": user}

def clear_rate_limit(user):
    for key in api.redis_client.scan_iter(f"ratelimit:chat:{user}:*"):
        api.redis_client.delete(key)

# ---------- auth and roles (no LLM) ----------
assert client.post("/chat", json={"message": "hi"}).status_code == 401                      # no user
assert client.post("/chat", json={"message": "hi"}, headers=as_user("u1:x")).status_code == 401
assert client.get("/approvals", headers=as_user("u1")).status_code == 403                    # customers can't review
assert client.post("/approvals/x", json={"decision": "approve"}, headers=as_user("u1")).status_code == 403

# ---------- thread ownership (no LLM: rejected before the graph runs) ----------
clear_rate_limit("u2")
r = client.post("/chat", json={"message": "what did u1 say?", "thread_id": "u1:abc"}, headers=as_user("u2"))
assert r.status_code == 404, r.text

# ---------- rate limiting (no LLM) ----------
user = f"rl-{uuid.uuid4().hex[:8]}"
for _ in range(api.RATE_LIMIT):
    assert api.rate_limit(user) == user           # real Redis INCR, under the limit
try:
    api.rate_limit(user)
    raise AssertionError("expected 429")
except HTTPException as e:
    assert e.status_code == 429 and 0 < int(e.headers["Retry-After"]) <= api.RATE_WINDOW

r = client.post("/chat", json={"message": "hi"}, headers=as_user(user))   # over the limit over HTTP
assert r.status_code == 429 and "retry-after" in r.headers

other = f"rl-{uuid.uuid4().hex[:8]}"                                       # limits are per user
assert api.rate_limit(other) == other

real = api.redis_client
api.redis_client = redis.from_url("redis://localhost:1", socket_connect_timeout=0.5)   # Redis down
assert api.rate_limit(user) == user                                        # fails open
api.redis_client = real
print("auth, ownership and rate-limit tests passed.")

# ---------- full approval flow (real LLM) ----------
clear_rate_limit("u1")
clear_rate_limit("u2")

r = client.post("/chat", json={"message": "I want a refund for order 102, it's very late"}, headers=as_user("u1"))
assert r.status_code == 200, r.text
body = r.json()
assert body["status"] == "pending_approval", body
thread, approval_id = body["thread_id"], body["approval_id"]
assert thread.startswith("u1:")

pending = client.get("/approvals", headers=as_user("admin")).json()
assert [(a["id"], a["order_id"], a["amount"]) for a in pending] == [(approval_id, "102", 60000)], pending

r = client.post("/chat", json={"message": "any update?", "thread_id": thread}, headers=as_user("u1"))
assert r.status_code == 409                                                # paused thread
assert backend.refunded_order_ids() == []                                  # nothing paid before approval

r = client.post(f"/approvals/{approval_id}", json={"decision": "approve"}, headers=as_user("admin"))
assert r.status_code == 200 and r.json()["status"] == "approved", r.text
assert backend.refunded_order_ids() == ["102"]
assert client.get("/approvals", headers=as_user("admin")).json() == []
assert client.post(f"/approvals/{approval_id}", json={"decision": "approve"},
                   headers=as_user("admin")).status_code == 409            # can't decide twice
assert client.post("/approvals/nope", json={"decision": "approve"}, headers=as_user("admin")).status_code == 404
assert client.post(f"/approvals/{approval_id}", json={"decision": "maybe"},
                   headers=as_user("admin")).status_code == 422            # only approve/reject

r = client.post("/chat", json={"message": "thanks, is it done?", "thread_id": thread}, headers=as_user("u1"))
assert r.status_code == 200 and r.json()["status"] == "done"               # thread usable again

# Reject path: u2's Rs 9000 refund.
r = client.post("/chat", json={"message": "Refund order 104, wrong size"}, headers=as_user("u2")).json()
assert r["status"] == "pending_approval", r
r = client.post(f"/approvals/{r['approval_id']}", json={"decision": "reject"}, headers=as_user("admin"))
assert r.status_code == 200 and r.json()["status"] == "rejected"
assert backend.refunded_order_ids() == ["102"]                             # still only 102

with Session(db.engine) as s:                                              # audit trail
    rows = s.scalars(select(db.Approval).order_by(db.Approval.created_at)).all()
    assert [(a.order_id, a.status, a.decided_by) for a in rows] == [("102", "approved", "admin"),
                                                                   ("104", "rejected", "admin")]
    assert all(a.decided_at for a in rows)
print("approval flow tests passed.")

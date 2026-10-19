"""
Worker tests: retries, give-up, idempotency (eager, in-process), then one end-to-end run
through a REAL Celery worker over the Redis broker. Uses the real LLM.
Run from the project root: python -m tests.test_worker
WARNING: resets the orders/refunds/approvals/notifications tables.
"""
import os
import subprocess
import sys
import tempfile
import time
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
import api
import backend
import db
import worker
from db import Approval, Notification

client = TestClient(api.app)
ADMIN = {"X-User-Id": "admin"}

def pending_refund(user, message):
    """Chat until the graph pauses for approval; returns the approval id."""
    for key in api.redis_client.scan_iter(f"ratelimit:chat:{user}:*"):
        api.redis_client.delete(key)
    r = client.post("/chat", json={"message": message}, headers={"X-User-Id": user}).json()
    assert r["status"] == "pending_approval", r
    return r["approval_id"]

def decide(approval_id, decision):
    r = client.post(f"/approvals/{approval_id}", json={"decision": decision}, headers=ADMIN)
    assert r.status_code == 202, r.text

def row(model, **where):
    with Session(db.engine) as s:
        return s.scalars(select(model).filter_by(**where)).one_or_none()

def flaky(fail_times, real):
    """Wrap a function so its first `fail_times` calls raise."""
    calls = {"n": 0}
    def wrapper(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise ConnectionError(f"simulated failure #{calls['n']}")
        return real(*args, **kwargs)
    return wrapper


# ================= A. eager mode: task logic, retries, idempotency =================
worker.celery.conf.task_always_eager = True
real_deliver, real_invoke = worker.deliver, worker.graph.invoke
db.reset_db()

# A1. Notification provider fails twice, then succeeds -> sent on attempt 3.
a1 = pending_refund("u1", "I want a refund for order 102, it's very late")
worker.deliver = flaky(2, real_deliver)
decide(a1, "approve")
worker.deliver = real_deliver
n1 = row(Notification, approval_id=a1)
assert backend.refunded_order_ids() == ["102"]
assert (n1.status, n1.attempts) == ("sent", 3), (n1.status, n1.attempts)
assert row(Approval, id=a1).job_status == "done"

# A2. Duplicate delivery of both tasks (e.g. redelivery after a crash) changes nothing.
worker.process_decision.delay(a1)
worker.send_notification.delay(n1.id)
with Session(db.engine) as s:
    assert len(s.scalars(select(Notification).filter_by(approval_id=a1)).all()) == 1
assert row(Notification, approval_id=a1).attempts == 3
assert backend.refunded_order_ids() == ["102"]

# A3. Resuming the graph fails once (e.g. Groq 503) -> retried, refund paid exactly once.
a3 = pending_refund("u2", "Refund order 104, wrong size")
worker.graph.invoke = flaky(1, real_invoke)
decide(a3, "approve")
worker.graph.invoke = real_invoke
assert backend.refunded_order_ids() == ["102", "104"]
assert row(Approval, id=a3).job_status == "done"
assert row(Notification, approval_id=a3).status == "sent"
print("A1-A3 passed: notification retries, duplicate delivery, graph-resume retry.")

db.reset_db()

# A4. Notification provider always down -> gives up after 1 + MAX_RETRIES attempts.
#     The decision itself still completed (rejected, nothing paid).
a4 = pending_refund("u1", "I want a refund for order 102, it's very late")
worker.deliver = flaky(999, real_deliver)
decide(a4, "reject")
worker.deliver = real_deliver
n4 = row(Notification, approval_id=a4)
assert (n4.status, n4.attempts) == ("failed", 1 + worker.MAX_RETRIES), (n4.status, n4.attempts)
assert row(Approval, id=a4).job_status == "done"
assert backend.refunded_order_ids() == []

# A5. Graph resume always fails -> job marked failed with the error, no money, no notification.
#     Once the outage is over, re-running the same task recovers it.
a5 = pending_refund("u2", "Refund order 104, wrong size")
worker.graph.invoke = flaky(999, real_invoke)
decide(a5, "approve")
worker.graph.invoke = real_invoke
a = row(Approval, id=a5)
assert a.job_status == "failed" and "simulated failure" in a.job_error, (a.job_status, a.job_error)
assert backend.refunded_order_ids() == [] and row(Notification, approval_id=a5) is None
worker.process_decision.delay(a5)                                   # manual retry by ops
assert row(Approval, id=a5).job_status == "done"
assert backend.refunded_order_ids() == ["104"]
assert row(Notification, approval_id=a5).status == "sent"
print("A4-A5 passed: give-up marks failed, manual re-run recovers.")


# ================= B. real worker process over the Redis broker =================
worker.celery.conf.task_always_eager = False
db.reset_db()

log_path = os.path.join(tempfile.gettempdir(), "celery_test_worker.log")
log_file = open(log_path, "w")
proc = subprocess.Popen(
    [sys.executable, "-m", "celery", "-A", "worker", "worker", "--pool=solo", "--loglevel=info",
     "--without-gossip", "--without-mingle", "--without-heartbeat"],
    stdout=log_file, stderr=subprocess.STDOUT,
    env={**os.environ, "FAKE_NOTIFY_FAILURE_RATE": "0.5"},   # make the provider flaky
)
try:
    b = pending_refund("u1", "I want a refund for order 102, it's very late")
    decide(b, "approve")
    assert backend.refunded_order_ids() == []        # API returned before the payout

    deadline = time.time() + 180
    while time.time() < deadline:
        n = row(Notification, approval_id=b)
        if n and n.status in ("sent", "failed"):
            break
        time.sleep(2)
    else:
        raise AssertionError("worker didn't finish in time")

    notes = client.get("/notifications", headers={"X-User-Id": "u1"}).json()
    assert backend.refunded_order_ids() == ["102"]
    assert row(Approval, id=b).job_status == "done"
    assert n.status == "sent", (n.status, n.attempts)
    assert len(notes) == 1 and "order 102" in notes[0]["message"], notes
    print(f"B passed: real worker paid out and notified (notification took {n.attempts} attempt(s)).")
finally:
    proc.terminate()
    proc.wait(timeout=30)
    log_file.close()
    retries = [l.strip() for l in open(log_path, encoding="utf-8", errors="replace") if "retrying" in l or "NOTIFY" in l]
    print("worker log:", *retries[-6:], sep="\n  ")

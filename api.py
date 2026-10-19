"""
HTTP API around the multi-agent graph.

  POST /chat              a customer sends a message (rate limited per user, in Redis)
  GET  /approvals         reviewers list refunds waiting for a human
  POST /approvals/{id}    a reviewer approves or rejects; the Celery worker resumes the
                          paused graph (pays out on approve) and notifies the customer
  GET  /notifications     a customer reads their notifications

Auth is a stand-in: the X-User-Id header. In production it comes from a verified
session or JWT, never from something the client can set freely.
Run: uvicorn api:app   (needs DATABASE_URL, REDIS_URL, GROQ_API_KEY), plus the worker (see worker.py)
"""
import logging
import math
import os
import time
import uuid
from typing import Literal
import redis
from fastapi import Depends, FastAPI, Header, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from db import Approval, Notification, engine
from graph import graph
from worker import process_decision

REVIEWERS = {"admin"}   # users allowed to approve refunds
RATE_LIMIT = 5          # /chat requests per user...
RATE_WINDOW = 60        # ...per this many seconds

app = FastAPI(title="Customer support agent")
redis_client = redis.from_url(os.environ["REDIS_URL"])


# ---------- auth, roles, rate limiting (FastAPI dependencies) ----------
def current_user(x_user_id: str | None = Header(default=None)) -> str:
    # ":" is reserved as the separator in thread ids ("<user>:<random>").
    if not x_user_id or ":" in x_user_id:
        raise HTTPException(401, "missing or invalid X-User-Id")
    return x_user_id

def reviewer(user: str = Depends(current_user)) -> str:
    if user not in REVIEWERS:
        raise HTTPException(403, "reviewers only")
    return user

def rate_limit(user: str = Depends(current_user)) -> str:
    """Fixed window: one Redis counter per user per window. INCR is atomic, so the limit
    holds across any number of API workers."""
    window = int(time.time() // RATE_WINDOW)
    key = f"ratelimit:chat:{user}:{window}"
    try:
        pipe = redis_client.pipeline()   # MULTI/EXEC: both commands run together
        pipe.incr(key)
        pipe.expire(key, RATE_WINDOW)
        count, _ = pipe.execute()
    except redis.RedisError as e:
        # Fail open: a Redis outage shouldn't take support down. Logged so it gets noticed.
        logging.warning("rate limiter unavailable, allowing request: %s", e)
        return user
    if count > RATE_LIMIT:
        retry_after = math.ceil((window + 1) * RATE_WINDOW - time.time())
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(retry_after)})
    return user


def config_for(thread_id: str, user_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id, "user_id": user_id}}


# ---------- customer ----------
class ChatIn(BaseModel):
    message: str
    thread_id: str | None = None   # omit to start a new conversation

@app.post("/chat")
def chat(body: ChatIn, user: str = Depends(rate_limit)):
    # Thread ids are "<user>:<random>", so a user can only ever continue their own threads.
    thread_id = body.thread_id or f"{user}:{uuid.uuid4().hex}"
    if thread_id.split(":", 1)[0] != user:
        raise HTTPException(404, "conversation not found")
    config = config_for(thread_id, user)

    # New input on a paused thread would orphan the pending refund tool call.
    if graph.get_state(config).interrupts:
        raise HTTPException(409, "a refund in this conversation is waiting for approval")

    result = graph.invoke({"messages": [HumanMessage(body.message)]}, config)

    if "__interrupt__" in result:
        info = result["__interrupt__"][0].value
        approval_id = uuid.uuid4().hex
        with Session(engine) as s, s.begin():
            s.add(Approval(id=approval_id, thread_id=thread_id, user_id=user,
                           order_id=info["order_id"], amount=info["amount"], reason=info["reason"]))
        return {"thread_id": thread_id, "status": "pending_approval", "approval_id": approval_id,
                "reply": "Your refund needs a quick review by our team. "
                         "We'll update this conversation once it's decided."}

    return {"thread_id": thread_id, "status": "done", "reply": result["messages"][-1].content}


# ---------- reviewer ----------
@app.get("/approvals")
def list_approvals(_: str = Depends(reviewer)):
    with Session(engine) as s:
        rows = s.scalars(select(Approval).where(Approval.status == "pending").order_by(Approval.created_at))
        return [{"id": a.id, "user_id": a.user_id, "order_id": a.order_id, "amount": a.amount,
                 "reason": a.reason, "created_at": a.created_at} for a in rows]


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]

@app.post("/approvals/{approval_id}", status_code=202)
def decide(approval_id: str, body: DecisionIn, reviewer_id: str = Depends(reviewer)):
    status = "approved" if body.decision == "approve" else "rejected"

    # Claim it atomically: only one reviewer can move it out of "pending", even if two
    # click at the same moment. The loser gets 409, so only one job is ever queued.
    with Session(engine) as s, s.begin():
        claimed = s.execute(
            update(Approval)
            .where(Approval.id == approval_id, Approval.status == "pending")
            .values(status=status, decided_by=reviewer_id, decided_at=func.now(), job_status="queued")
            .returning(Approval.id)
        ).first()
        if claimed is None:
            if s.get(Approval, approval_id) is None:
                raise HTTPException(404, "approval not found")
            raise HTTPException(409, "already decided")

    try:
        process_decision.delay(approval_id)   # the worker resumes the graph and notifies
    except Exception:
        # Broker unreachable: undo the claim so the reviewer can simply try again.
        with Session(engine) as s, s.begin():
            s.execute(update(Approval).where(Approval.id == approval_id)
                      .values(status="pending", decided_by=None, decided_at=None, job_status=None))
        raise HTTPException(503, "could not queue the decision, try again")

    return {"id": approval_id, "status": status, "job_status": "queued"}


# ---------- customer notifications ----------
@app.get("/notifications")
def list_notifications(user: str = Depends(current_user)):
    with Session(engine) as s:
        rows = s.scalars(select(Notification)
                         .where(Notification.user_id == user, Notification.status == "sent")
                         .order_by(Notification.created_at))
        return [{"id": n.id, "message": n.message, "sent_at": n.sent_at} for n in rows]

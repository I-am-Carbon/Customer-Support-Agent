"""
Celery worker: everything that happens AFTER a reviewer decides, off the request path.

  POST /approvals/{id}  ->  process_decision   resume the paused graph (the refund tool pays
                                               out on "approve"), write a notification row
                        ->  send_notification  deliver it to the customer

Both tasks are idempotent and retry with exponential backoff, so a crash, a Groq error
or a flaky notification provider just means "try again later", never a double payout.
Broker: Redis (REDIS_URL). Run (Windows needs the solo pool):
  celery -A worker worker --pool=solo --loglevel=info --without-gossip --without-mingle --without-heartbeat
"""
import logging
import os
import random
import ssl
from celery import Celery
from langgraph.types import Command
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from db import Approval, Notification, engine
from graph import graph

REDIS_URL = os.environ["REDIS_URL"]
MAX_RETRIES = 5

celery = Celery("support", broker=REDIS_URL)
celery.conf.update(
    broker_use_ssl={"ssl_cert_reqs": ssl.CERT_REQUIRED} if REDIS_URL.startswith("rediss://") else None,
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,          # results live in Postgres, no result backend needed
    task_acks_late=True,              # ack after the task finishes: a worker crash -> redelivered
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,     # long tasks (LLM calls): don't hoard messages
)
log = logging.getLogger(__name__)


def backoff(retries: int) -> float:
    """1s, 2s, 4s, 8s, ... capped at 60s, with jitter so retries don't arrive in lockstep."""
    return min(2 ** retries, 60) * random.uniform(0.5, 1.5)


# ---------- the "notification provider" (fake) ----------
# Stands in for email/SMS/push. FAKE_NOTIFY_FAILURE_RATE=0.5 makes half the sends fail,
# to watch the retries work.
def deliver(user_id: str, message: str):
    if random.random() < float(os.environ.get("FAKE_NOTIFY_FAILURE_RATE", "0")):
        raise ConnectionError("notification provider unavailable")
    log.info("NOTIFY %s: %s", user_id, message)


# ---------- tasks ----------
@celery.task(bind=True, max_retries=MAX_RETRIES)
def process_decision(self, approval_id: str):
    with Session(engine) as s:
        a = s.get(Approval, approval_id)
        if a is None or a.job_status == "done":   # duplicate delivery: already handled
            return
        decision = "approve" if a.status == "approved" else "reject"
        config = {"configurable": {"thread_id": a.thread_id, "user_id": a.user_id}}   # run as the CUSTOMER
        order_id, user_id = a.order_id, a.user_id

    try:
        state = graph.get_state(config)
        if state.interrupts:     # normal case: still paused at interrupt()
            graph.invoke(Command(resume=decision), config)
        elif state.next:         # an earlier attempt resumed but crashed mid-run: continue it
            graph.invoke(None, config)
        reply = graph.get_state(config).values["messages"][-1].content

        with Session(engine) as s, s.begin():
            # ON CONFLICT DO NOTHING: a retry after this point can't create a second notification
            s.execute(insert(Notification)
                      .values(approval_id=approval_id, user_id=user_id,
                              message=f"Update on your refund for order {order_id}: {reply}")
                      .on_conflict_do_nothing(index_elements=["approval_id"]))
            s.execute(update(Approval).where(Approval.id == approval_id)   # same transaction as the insert
                      .values(job_status="done", job_error=None))
            notification_id = s.query(Notification.id).filter_by(approval_id=approval_id).scalar()
    except Exception as e:
        retry_or_give_up(self, e, lambda: set_row(Approval, approval_id, job_status="failed", job_error=str(e)[:500]))

    send_notification.delay(notification_id)


@celery.task(bind=True, max_retries=MAX_RETRIES)
def send_notification(self, notification_id: int):
    with Session(engine) as s, s.begin():
        n = s.get(Notification, notification_id)
        if n is None or n.status == "sent":   # duplicate delivery: already sent
            return
        n.attempts += 1
        user_id, message = n.user_id, n.message

    try:
        deliver(user_id, message)
    except Exception as e:
        retry_or_give_up(self, e, lambda: set_row(Notification, notification_id, status="failed"))

    set_row(Notification, notification_id, status="sent", sent_at=func.now())


def set_row(model, row_id, **values):
    with Session(engine) as s, s.begin():
        s.execute(update(model).where(model.id == row_id).values(**values))


def retry_or_give_up(task, exc: Exception, mark_failed):
    """Retry with backoff; after MAX_RETRIES mark the row failed so a human can see it."""
    if task.request.retries < task.max_retries:
        log.warning("%s failed (attempt %d), retrying: %s", task.name, task.request.retries + 1, exc)
        raise task.retry(exc=exc, countdown=backoff(task.request.retries))
    log.error("%s gave up after %d attempts: %s", task.name, task.request.retries + 1, exc)
    mark_failed()
    raise exc

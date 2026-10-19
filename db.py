"""
Database wiring: one Postgres for both the business data (SQLAlchemy) and
LangGraph's conversation checkpoints (PostgresSaver). No business rules here.

Connection string comes from the DATABASE_URL environment variable.
  python db.py init   create tables and seed if empty (safe; used by docker compose)
  python db.py        wipe and reseed the business tables (tests do this)
"""
import atexit
import os
from datetime import date, datetime, timedelta
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, create_engine, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

DATABASE_URL = os.environ["DATABASE_URL"]   # e.g. postgresql://user:pass@host/db?sslmode=require

# SQLAlchemy wants the driver in the scheme; psycopg (checkpointer) wants the plain URL.
_plain_url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
engine = create_engine(_plain_url.replace("postgresql://", "postgresql+psycopg://", 1),
                       pool_pre_ping=True)   # hosted DBs drop idle connections


# Row-level security with no policies = nobody but the table owner (this app) can read rows.
# Supabase publishes the public schema over a REST API, so without this the anon key could
# read every order. Harmless on plain Postgres.
RLS = "ALTER TABLE {} ENABLE ROW LEVEL SECURITY"


# ---------- tables ----------
class Base(DeclarativeBase):
    pass

class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    item: Mapped[str] = mapped_column(String)
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)          # delivered / in_transit
    ordered: Mapped[date] = mapped_column(Date)
    delivered_on: Mapped[date | None] = mapped_column(Date)   # None until delivered
    address: Mapped[str] = mapped_column(String)

class Refund(Base):
    __tablename__ = "refunds"
    # order_id is the primary key, so the DATABASE itself rejects a second refund
    # for the same order, even if two requests race past the eligibility check.
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), primary_key=True)
    amount: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class Approval(Base):
    """A refund paused at interrupt(), waiting for a human. Also the audit trail of who decided."""
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    thread_id: Mapped[str] = mapped_column(String)        # the paused conversation to resume
    user_id: Mapped[str] = mapped_column(String)          # the customer (tools run as them on resume)
    order_id: Mapped[str] = mapped_column(String)
    amount: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)   # pending / approved / rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_by: Mapped[str | None] = mapped_column(String)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Background processing by the Celery worker: queued / done / failed
    job_status: Mapped[str | None] = mapped_column(String)
    job_error: Mapped[str | None] = mapped_column(String)

class Notification(Base):
    """Outbox row: written once per decision, then delivered (with retries) by the worker."""
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # unique: a re-run of the decision job can't create a second notification
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"), unique=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    message: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")   # pending / sent / failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def seed_orders() -> list[dict]:
    """Dates are relative to the real today, so every reset gives the same situation
    (103 too early to refund, 106 past the return window, ...) whatever day it is."""
    def ago(days: int) -> date:
        return date.today() - timedelta(days=days)
    return [
        dict(id="101", user_id="u1", item="Headphones", amount=2000,  status="delivered",  ordered=ago(14), delivered_on=ago(7),  address="Bangalore"),
        dict(id="102", user_id="u1", item="Laptop",     amount=60000, status="in_transit", ordered=ago(19), delivered_on=None,    address="Bangalore"),
        dict(id="103", user_id="u2", item="Mouse",      amount=800,   status="in_transit", ordered=ago(3),  delivered_on=None,    address="Chennai"),
        dict(id="104", user_id="u2", item="Phone",      amount=9000,  status="delivered",  ordered=ago(9),  delivered_on=ago(5),  address="Chennai"),
        dict(id="105", user_id="u1", item="Book",       amount=1200,  status="in_transit", ordered=ago(14), delivered_on=None,    address="Bangalore"),
        dict(id="106", user_id="u1", item="Shoes",      amount=3000,  status="delivered",  ordered=ago(33), delivered_on=ago(29), address="Bangalore"),
    ]

def reset_db():
    """Recreate the business tables with the seed orders. Checkpoints are left alone."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s, s.begin():
        s.add_all([Order(**row) for row in seed_orders()])
        for table in Base.metadata.tables:
            s.execute(text(RLS.format(table)))


# ---------- LangGraph checkpointer ----------
def make_checkpointer() -> PostgresSaver:
    """Conversation state (messages, pending approvals) stored in Postgres, so it
    survives restarts. Settings are the ones PostgresSaver requires."""
    pool = ConnectionPool(_plain_url, open=True,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    atexit.register(pool.close)   # close cleanly instead of in a garbage-collector warning
    checkpointer = PostgresSaver(pool)
    checkpointer.setup()   # creates checkpoint tables on first run; no-op afterwards
    with pool.connection() as conn:
        for table in ["checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"]:
            conn.execute(RLS.format(table))
    return checkpointer


def init_db():
    """Safe on every start: create missing tables, seed only an empty DB, set up checkpoints.
    Unlike reset_db(), never deletes anything."""
    Base.metadata.create_all(engine)
    with Session(engine) as s, s.begin():
        # create_all never alters an existing table, so columns added later go here.
        # (A real project would use Alembic migrations.)
        s.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivered_on DATE"))
        # Orders delivered before the column existed: use the order date (the window can only
        # close earlier than the truth, never later).
        s.execute(text("UPDATE orders SET delivered_on = ordered WHERE status = 'delivered' AND delivered_on IS NULL"))
        if s.query(Order).first() is None:
            s.add_all([Order(**row) for row in seed_orders()])
        for table in Base.metadata.tables:
            s.execute(text(RLS.format(table)))
    make_checkpointer()   # runs PostgresSaver.setup() once, before api and worker start


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["init"]:
        init_db()
        print("Database initialised.")
    else:
        reset_db()
        print("Database reset with seed orders.")

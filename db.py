"""
Database wiring: one Postgres for both the business data (SQLAlchemy) and
LangGraph's conversation checkpoints (PostgresSaver). No business rules here.

Connection string comes from the DATABASE_URL environment variable.
Reset the data with: python db.py
"""
import atexit
import os
from datetime import date, datetime
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


SEED_ORDERS = [
    dict(id="101", user_id="u1", item="Headphones", amount=2000,  status="delivered",  ordered=date(2026, 9, 20), address="Bangalore"),
    dict(id="102", user_id="u1", item="Laptop",     amount=60000, status="in_transit", ordered=date(2026, 9, 15), address="Bangalore"),
    dict(id="103", user_id="u2", item="Mouse",      amount=800,   status="in_transit", ordered=date(2026, 10, 1), address="Chennai"),
    dict(id="104", user_id="u2", item="Phone",      amount=9000,  status="delivered",  ordered=date(2026, 9, 25), address="Chennai"),
    dict(id="105", user_id="u1", item="Book",       amount=1200,  status="in_transit", ordered=date(2026, 9, 20), address="Bangalore"),
]

def reset_db():
    """Recreate the business tables with the seed orders. Checkpoints are left alone."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s, s.begin():
        s.add_all([Order(**row) for row in SEED_ORDERS])
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


if __name__ == "__main__":
    reset_db()
    print("Database reset with seed orders.")

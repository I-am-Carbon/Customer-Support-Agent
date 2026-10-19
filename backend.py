"""
Backend: business logic over the Postgres data in db.py.
No LLM in this file. Everything here is plain, deterministic Python,
so the rules (policy) can never be talked around by a prompt.
"""
from datetime import date
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from db import engine, Approval, Order, Refund

AUTO_APPROVE_LIMIT = 5000   # refunds above this need a human
MIN_DELAY_DAYS = 7          # in-transit orders become refundable after this many days
RETURN_WINDOW_DAYS = 10     # delivered orders are refundable for this many days after delivery


def _find_order(s: Session, order_id: str, user_id: str) -> Order | None:
    """The order, but only if it belongs to this user.
    Someone else's order looks exactly like a missing one, so ids can't be probed."""
    return s.scalar(select(Order).where(Order.id == order_id, Order.user_id == user_id))


def get_order(order_id: str, user_id: str) -> dict:
    with Session(engine) as s:
        order = _find_order(s, order_id, user_id)
        if order is None:
            return {"error": "order not found"}
        return {"order_id": order.id, "user": order.user_id, "item": order.item, "amount": order.amount,
                "status": order.status, "ordered": str(order.ordered),
                "delivered_on": str(order.delivered_on) if order.delivered_on else None,
                "address": order.address}


def update_address(order_id: str, new_address: str, user_id: str) -> dict:
    with Session(engine) as s, s.begin():
        order = _find_order(s, order_id, user_id)
        if order is None:
            return {"error": "order not found"}
        if order.status == "delivered":
            return {"error": "order already delivered, address can't be changed"}
        order.address = new_address
    return {"ok": True, "order_id": order_id, "address": new_address}


def _refund_rules(s: Session, order: Order, today: date) -> dict:
    """The refund policy, in one place. Used both before asking and again at payout."""
    if s.get(Refund, order.id) is not None:
        return {"result": "not_eligible", "reason": "already refunded"}
    if order.status == "in_transit" and (today - order.ordered).days < MIN_DELAY_DAYS:
        return {"result": "not_eligible", "reason": f"wait until {MIN_DELAY_DAYS} days after ordering"}
    if order.status == "delivered" and (today - order.delivered_on).days > RETURN_WINDOW_DAYS:
        return {"result": "not_eligible",
                "reason": f"the {RETURN_WINDOW_DAYS}-day return window closed (delivered {order.delivered_on})"}
    if order.amount > AUTO_APPROVE_LIMIT:
        return {"result": "needs_approval", "amount": order.amount}
    return {"result": "eligible", "amount": order.amount}


def check_refund_eligibility(order_id: str, user_id: str, today: date | None = None) -> dict:
    with Session(engine) as s:
        order = _find_order(s, order_id, user_id)
        if order is None:
            return {"result": "not_eligible", "reason": "order not found"}
        return _refund_rules(s, order, today or date.today())


def issue_refund(order_id: str, user_id: str, approved: bool = False, today: date | None = None) -> dict:
    """Pay out. Every rule is checked again here, so money can't move even if a caller
    skips check_refund_eligibility. approved=True only after a human said yes."""
    try:
        with Session(engine) as s, s.begin():
            order = _find_order(s, order_id, user_id)
            if order is None:
                return {"error": "order not found"}
            check = _refund_rules(s, order, today or date.today())
            if check["result"] == "not_eligible":
                return {"error": check["reason"]}
            if check["result"] == "needs_approval" and not approved:
                return {"error": "needs human approval"}
            s.add(Refund(order_id=order_id, amount=order.amount))
            amount = order.amount
    except IntegrityError:   # refunds.order_id is the primary key: catches two payouts racing
        return {"error": "already refunded"}
    return {"ok": True, "order_id": order_id, "amount": amount}


def refunded_order_ids() -> list[str]:
    with Session(engine) as s:
        return list(s.scalars(select(Refund.order_id).order_by(Refund.created_at)))


# ---------- customer views (read-only, always scoped to one user) ----------
def list_orders(user_id: str) -> list[dict]:
    """The user's orders, newest first, each with its refund (if any)."""
    with Session(engine) as s:
        rows = s.execute(select(Order, Refund)
                         .outerjoin(Refund, Refund.order_id == Order.id)
                         .where(Order.user_id == user_id)
                         .order_by(Order.ordered.desc(), Order.id.desc())).all()
        return [{"order_id": o.id, "item": o.item, "amount": o.amount, "status": o.status,
                 "ordered": str(o.ordered),
                 "delivered_on": str(o.delivered_on) if o.delivered_on else None,
                 "address": o.address,
                 "refunded_at": r.created_at.isoformat() if r else None}
                for o, r in rows]


def refund_history(user_id: str) -> list[dict]:
    """Every refund the user asked for that got anywhere: paid ones, plus reviews that are
    pending, rejected or approved-but-not-paid. Newest first."""
    with Session(engine) as s:
        paid = s.execute(select(Refund, Order).join(Order, Refund.order_id == Order.id)
                         .where(Order.user_id == user_id)).all()
        reviews = s.scalars(select(Approval).where(Approval.user_id == user_id)).all()
        items = {o.id: o.item for o in s.scalars(select(Order).where(Order.user_id == user_id))}

    approved_by = {a.order_id: a.decided_by for a in reviews if a.status == "approved"}
    paid_ids = {r.order_id for r, _ in paid}
    history = [{"order_id": r.order_id, "item": o.item, "amount": r.amount, "status": "refunded",
                "detail": f"approved by {approved_by[r.order_id]}" if r.order_id in approved_by else "automatic",
                "at": r.created_at.isoformat()}
               for r, o in paid]
    for a in reviews:
        if a.status == "approved" and a.order_id in paid_ids:
            continue                                   # already listed as refunded
        if a.status == "pending":
            status, detail = "waiting for review", f"reason: {a.reason}"
        elif a.status == "rejected":
            status, detail = "rejected", f"rejected by {a.decided_by}"
        elif a.job_status == "done":                   # approved, but payout refused (e.g. window closed)
            status, detail = "not paid", f"approved by {a.decided_by}, but the refund was refused"
        else:
            status, detail = "processing", f"approved by {a.decided_by}"
        history.append({"order_id": a.order_id, "item": items.get(a.order_id, "?"), "amount": a.amount,
                        "status": status, "detail": detail,
                        "at": (a.decided_at or a.created_at).isoformat()})
    return sorted(history, key=lambda h: h["at"], reverse=True)

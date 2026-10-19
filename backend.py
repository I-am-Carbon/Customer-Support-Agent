"""
Backend: business logic over the Postgres data in db.py.
No LLM in this file. Everything here is plain, deterministic Python,
so the rules (policy) can never be talked around by a prompt.
"""
from datetime import date
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from db import engine, Order, Refund

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

"""
Mock backend: fake data + business logic.
No LLM in this file. Everything here is plain, deterministic Python,
so the rules (policy) can never be talked around by a prompt.
"""
from datetime import date

TODAY = date(2026, 10, 4)   # fixed date so tests give the same result every run

ORDERS = {
    "101": {"user": "u1", "item": "Headphones", "amount": 2000, "status": "delivered",  "ordered": date(2026, 9, 20), "address": "Bangalore"},
    "102": {"user": "u1", "item": "Laptop",     "amount": 60000, "status": "in_transit", "ordered": date(2026, 9, 15), "address": "Bangalore"},
    "103": {"user": "u2", "item": "Mouse",      "amount": 800,   "status": "in_transit", "ordered": date(2026, 10, 1), "address": "Chennai"},
    "104": {"user": "u2", "item": "Phone",      "amount": 9000,  "status": "delivered",  "ordered": date(2026, 9, 25), "address": "Chennai"},
    "105": {"user": "u1", "item": "Book",       "amount": 1200,  "status": "in_transit", "ordered": date(2026, 9, 20), "address": "Bangalore"},
}
REFUNDS = []   # order ids that have been refunded

AUTO_APPROVE_LIMIT = 5000   # refunds above this need a human
MIN_DELAY_DAYS = 7          # in-transit orders become refundable after this many days


def _find_order(order_id: str, user_id: str) -> dict | None:
    """The order, but only if it belongs to this user.
    Someone else's order looks exactly like a missing one, so ids can't be probed."""
    order = ORDERS.get(order_id)
    if order is None or order["user"] != user_id:
        return None
    return order


def get_order(order_id: str, user_id: str) -> dict:
    order = _find_order(order_id, user_id)
    if order is None:
        return {"error": "order not found"}
    return {"order_id": order_id, **order, "ordered": str(order["ordered"])}


def update_address(order_id: str, new_address: str, user_id: str) -> dict:
    order = _find_order(order_id, user_id)
    if order is None:
        return {"error": "order not found"}
    if order["status"] == "delivered":
        return {"error": "order already delivered, address can't be changed"}
    order["address"] = new_address
    return {"ok": True, "order_id": order_id, "address": new_address}


def check_refund_eligibility(order_id: str, user_id: str, today: date = TODAY) -> dict:
    order = _find_order(order_id, user_id)
    if order is None:
        return {"result": "not_eligible", "reason": "order not found"}
    if order_id in REFUNDS:
        return {"result": "not_eligible", "reason": "already refunded"}
    days_since_order = (today - order["ordered"]).days
    if order["status"] == "in_transit" and days_since_order < MIN_DELAY_DAYS:
        return {"result": "not_eligible", "reason": f"wait until {MIN_DELAY_DAYS} days after ordering"}
    if order["amount"] > AUTO_APPROVE_LIMIT:
        return {"result": "needs_approval", "amount": order["amount"]}
    return {"result": "eligible", "amount": order["amount"]}


def issue_refund(order_id: str, user_id: str) -> dict:
    # Checked again here so money can't move even if a caller skips the eligibility check.
    if _find_order(order_id, user_id) is None:
        return {"error": "order not found"}
    REFUNDS.append(order_id)
    return {"ok": True, "order_id": order_id, "amount": ORDERS[order_id]["amount"]}

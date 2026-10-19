"""Policy tests against Postgres (DATABASE_URL). No LLM needed. Run: python test_backend.py
WARNING: resets the orders/refunds tables."""
from datetime import date, timedelta
import backend
import db

db.reset_db()

# ---------- refund policy (u1 owns 101, 102, 105, 106; u2 owns 103, 104) ----------
assert backend.check_refund_eligibility("101", "u1")["result"] == "eligible"         # delivered, small
assert backend.check_refund_eligibility("102", "u1")["result"] == "needs_approval"   # 19 days late, Rs 60000
assert backend.check_refund_eligibility("103", "u2")["result"] == "not_eligible"     # only 3 days in transit
assert backend.check_refund_eligibility("999", "u1")["result"] == "not_eligible"     # doesn't exist
assert "error" in backend.update_address("101", "Delhi", "u1")                       # already delivered

# ---------- return window: delivered orders, 10 days after delivery ----------
# Seed dates are relative to today: 101 was delivered 7 days ago, 106 29 days ago.
later = lambda days: date.today() + timedelta(days=days)
assert backend.check_refund_eligibility("106", "u1")["result"] == "not_eligible"     # delivered 29 days ago
assert "return window" in backend.check_refund_eligibility("106", "u1")["reason"]
assert backend.check_refund_eligibility("101", "u1", today=later(3))["result"] == "eligible"       # day 10: last day
assert backend.check_refund_eligibility("101", "u1", today=later(4))["result"] == "not_eligible"   # day 11
assert backend.check_refund_eligibility("102", "u1", today=later(100))["result"] == "needs_approval" # in transit: no window

# ---------- issue_refund re-checks every rule, even if the caller skipped the check ----------
assert backend.issue_refund("106", "u1")["error"].startswith("the 10-day return window closed")
assert backend.issue_refund("103", "u2") == {"error": "wait until 7 days after ordering"}
assert backend.issue_refund("102", "u1") == {"error": "needs human approval"}          # Rs 60000, no human yet
assert backend.refunded_order_ids() == []

# ---------- ownership: other users' orders look exactly like missing ones ----------
assert backend.get_order("101", "u1")["item"] == "Headphones"
assert backend.get_order("103", "u1") == backend.get_order("999", "u1") == {"error": "order not found"}
assert backend.update_address("103", "Delhi", "u1") == {"error": "order not found"}
assert backend.get_order("103", "u2")["address"] == "Chennai"                        # unchanged
assert backend.check_refund_eligibility("101", "u2") == {"result": "not_eligible", "reason": "order not found"}
assert backend.issue_refund("101", "u2") == {"error": "order not found"}             # even skipping the check
assert backend.refunded_order_ids() == []                                            # no money moved
assert backend.update_address("102", "Mysore", "u1")["ok"]                           # owner still can
assert backend.issue_refund("102", "u1", approved=True)["ok"]                        # after a human said yes

# ---------- no double refunds ----------
assert backend.issue_refund("101", "u1")["ok"]
assert backend.check_refund_eligibility("101", "u1")["result"] == "not_eligible"
# Even skipping the eligibility check, the primary key on refunds.order_id blocks a second payout.
assert backend.issue_refund("101", "u1") == {"error": "already refunded"}
assert sorted(backend.refunded_order_ids()) == ["101", "102"]

# ---------- customer views: only your own orders and refunds ----------
assert [o["order_id"] for o in backend.list_orders("u1")] == ["105", "101", "102", "106"]   # newest first
assert {o["order_id"] for o in backend.list_orders("u2")} == {"103", "104"}
assert backend.list_orders("nobody") == []
refunded = {o["order_id"]: o["refunded_at"] for o in backend.list_orders("u1")}
assert refunded["101"] and refunded["102"] and refunded["105"] is None
history = backend.refund_history("u1")
assert sorted((h["order_id"], h["status"]) for h in history) == [("101", "refunded"), ("102", "refunded")]
assert backend.refund_history("u2") == []
print("All policy tests passed.")

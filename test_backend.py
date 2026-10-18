"""Policy tests. No LLM, no API key needed. Run: python test_backend.py"""
import backend

# ---------- refund policy (u1 owns 101 and 102, u2 owns 103) ----------
assert backend.check_refund_eligibility("101", "u1")["result"] == "eligible"         # delivered, small
assert backend.check_refund_eligibility("102", "u1")["result"] == "needs_approval"   # 19 days late, Rs 60000
assert backend.check_refund_eligibility("103", "u2")["result"] == "not_eligible"     # only 3 days in transit
assert backend.check_refund_eligibility("999", "u1")["result"] == "not_eligible"     # doesn't exist
assert "error" in backend.update_address("101", "Delhi", "u1")                       # already delivered

# ---------- ownership: other users' orders look exactly like missing ones ----------
assert backend.get_order("101", "u1")["item"] == "Headphones"
assert backend.get_order("103", "u1") == backend.get_order("999", "u1") == {"error": "order not found"}
assert backend.update_address("103", "Delhi", "u1") == {"error": "order not found"}
assert backend.ORDERS["103"]["address"] == "Chennai"                                 # unchanged
assert backend.check_refund_eligibility("101", "u2") == {"result": "not_eligible", "reason": "order not found"}
assert backend.issue_refund("101", "u2") == {"error": "order not found"}             # even skipping the check
assert backend.REFUNDS == []                                                         # no money moved
assert backend.update_address("102", "Mysore", "u1")["ok"]                           # owner still can

# ---------- no double refunds ----------
backend.issue_refund("101", "u1")
assert backend.check_refund_eligibility("101", "u1")["result"] == "not_eligible"
print("All policy tests passed.")

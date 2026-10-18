"""Policy tests. No LLM, no API key needed. Run: python test_backend.py"""
import backend

assert backend.check_refund_eligibility("101")["result"] == "eligible"         # delivered, small
assert backend.check_refund_eligibility("102")["result"] == "needs_approval"   # 19 days late, Rs 60000
assert backend.check_refund_eligibility("103")["result"] == "not_eligible"     # only 3 days in transit
assert backend.check_refund_eligibility("999")["result"] == "not_eligible"     # doesn't exist
assert "error" in backend.update_address("101", "Delhi")                       # already delivered
backend.issue_refund("101")
assert backend.check_refund_eligibility("101")["result"] == "not_eligible"     # no double refunds
print("All policy tests passed.")

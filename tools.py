"""
Tools = the actions agents are allowed to take.
Each agent only gets ITS OWN list (ORDER_TOOLS or REFUND_TOOLS).
The docstrings matter: the LLM reads them to decide when to call each tool.
"""
from langchain_core.tools import tool
from langgraph.types import interrupt
import backend


# ---------- order agent's tools ----------
@tool
def get_order(order_id: str) -> dict:
    """Look up an order by its id. Returns item, amount, status, order date and address."""
    return backend.get_order(order_id)


@tool
def update_address(order_id: str, new_address: str) -> dict:
    """Change the delivery address of an order that has not been delivered yet."""
    return backend.update_address(order_id, new_address)


@tool
def transfer_to_refund_agent(order_id: str, summary: str) -> str:
    """Hand the conversation to the refund specialist. Use when the customer wants a refund.
    summary: one line on what has happened so far."""
    return f"Transferred to refund specialist. Order {order_id}. Context: {summary}"


# ---------- refund agent's tools ----------
@tool
def request_refund(order_id: str, reason: str) -> str:
    """Request a refund for an order. Policy is checked automatically."""
    check = backend.check_refund_eligibility(order_id)

    if check["result"] == "not_eligible":
        return f"Refund denied: {check['reason']}."

    if check["result"] == "needs_approval":
        # Pause the whole graph here until a human answers.
        # When resumed, interrupt() returns whatever the human sent.
        decision = interrupt({"order_id": order_id, "amount": check["amount"], "reason": reason})
        if decision != "approve":
            return "A human reviewer rejected this refund."

    refund = backend.issue_refund(order_id)
    return f"Refund of Rs {refund['amount']} issued for order {order_id}."


ORDER_TOOLS = [get_order, update_address, transfer_to_refund_agent]
REFUND_TOOLS = [get_order, request_refund]

"""
Tools = the actions agents are allowed to take.
Each agent only gets ITS OWN list (ORDER_TOOLS or REFUND_TOOLS).
The docstrings matter: the LLM reads them to decide when to call each tool.

The logged-in user comes from the run config, NOT from a tool argument.
A `config: RunnableConfig` parameter is filled in by LangChain and hidden from the LLM,
so the model can't claim to be someone else.
"""
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import interrupt
import backend


def _user_id(config: RunnableConfig) -> str:
    # KeyError if missing: no logged-in user means no access (fail closed).
    return config["configurable"]["user_id"]


# ---------- order agent's tools ----------
@tool
def get_order(order_id: str, config: RunnableConfig) -> dict:
    """Look up an order by its id. Returns item, amount, status, order date and address."""
    return backend.get_order(order_id, _user_id(config))


@tool
def update_address(order_id: str, new_address: str, config: RunnableConfig) -> dict:
    """Change the delivery address of an order that has not been delivered yet."""
    return backend.update_address(order_id, new_address, _user_id(config))


@tool
def transfer_to_refund_agent(order_id: str, summary: str) -> str:
    """Hand the conversation to the refund specialist. Use when the customer wants a refund.
    summary: one line on what has happened so far."""
    return f"Transferred to refund specialist. Order {order_id}. Context: {summary}"


# ---------- refund agent's tools ----------
@tool
def request_refund(order_id: str, reason: str, config: RunnableConfig) -> str:
    """Request a refund for an order. Policy is checked automatically."""
    user_id = _user_id(config)
    check = backend.check_refund_eligibility(order_id, user_id)

    if check["result"] == "not_eligible":
        return f"Refund denied: {check['reason']}."

    if check["result"] == "needs_approval":
        # Pause the whole graph here until a human answers.
        # When resumed, interrupt() returns whatever the human sent.
        decision = interrupt({"order_id": order_id, "amount": check["amount"], "reason": reason})
        if decision != "approve":
            return "A human reviewer rejected this refund."

    refund = backend.issue_refund(order_id, user_id, approved=check["result"] == "needs_approval")
    if "error" in refund:
        return f"Refund denied: {refund['error']}."
    return f"Refund of Rs {refund['amount']} issued for order {order_id}."


ORDER_TOOLS = [get_order, update_address, transfer_to_refund_agent]
REFUND_TOOLS = [get_order, request_refund]

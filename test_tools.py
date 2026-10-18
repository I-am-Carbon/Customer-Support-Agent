"""Ownership at the tool layer. No LLM, no API key needed. Run: python test_tools.py"""
from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph, MessagesState, START
from langgraph.prebuilt import ToolNode
import backend
import tools

u1 = {"configurable": {"user_id": "u1"}}
u2 = {"configurable": {"user_id": "u2"}}

# The LLM must not be able to choose the user: user_id is not in any tool's schema.
for t in tools.ORDER_TOOLS + tools.REFUND_TOOLS:
    assert "user_id" not in t.args and "config" not in t.args, t.name

# The user comes from the run config.
assert tools.get_order.invoke({"order_id": "101"}, config=u1)["item"] == "Headphones"
assert tools.get_order.invoke({"order_id": "101"}, config=u2) == {"error": "order not found"}
assert tools.request_refund.invoke({"order_id": "101", "reason": "broken"}, config=u2) == \
    "Refund denied: order not found."
assert backend.REFUNDS == []

# No logged-in user -> no access (fail closed).
try:
    tools.get_order.invoke({"order_id": "101"}, config={"configurable": {}})
    raise AssertionError("expected failure without user_id")
except KeyError:
    pass

# Same path the graph uses: an LLM tool call goes through ToolNode, which passes the config on.
builder = StateGraph(MessagesState)
builder.add_node("tools", ToolNode(tools.REFUND_TOOLS))
builder.add_edge(START, "tools")
tool_graph = builder.compile()

def run_tool_call(name, args, config):
    call = AIMessage("", tool_calls=[{"name": name, "args": args, "id": "c1"}])
    return tool_graph.invoke({"messages": [call]}, config)["messages"][-1].content

assert run_tool_call("request_refund", {"order_id": "101", "reason": "x"}, u2) == "Refund denied: order not found."
assert backend.REFUNDS == []
assert run_tool_call("request_refund", {"order_id": "101", "reason": "x"}, u1) == "Refund of Rs 2000 issued for order 101."
assert backend.REFUNDS == ["101"]
print("All tool ownership tests passed.")

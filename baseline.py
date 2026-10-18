"""
Single-agent baseline for comparison: ONE agent that holds every tool.
No triage, no handoff. Same model, same backend, same policy-enforcing tools.

  START -> agent <-> tools -> END
"""
from langchain_core.messages import SystemMessage
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver
from graph import llm
from tools import get_order, update_address, request_refund

ALL_TOOLS = [get_order, update_address, request_refund]

PROMPT = """You are a customer support agent. You can look up orders, change delivery
addresses and process refunds. Always look up the order before answering.
Use request_refund for refunds; the tool enforces policy, so report its result honestly
and never override it. If you don't know the order id, ask for it.
For anything else, say you can help with order status, address changes and refunds. Be brief."""

agent_llm = llm.bind_tools(ALL_TOOLS)

def agent(state: MessagesState):
    return {"messages": [agent_llm.invoke([SystemMessage(PROMPT)] + state["messages"])]}

def needs_tools(state: MessagesState):
    return "tools" if getattr(state["messages"][-1], "tool_calls", None) else END

builder = StateGraph(MessagesState)
builder.add_node("agent", agent)
builder.add_node("tools", ToolNode(ALL_TOOLS))
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", needs_tools, ["tools", END])
builder.add_edge("tools", "agent")

graph = builder.compile(checkpointer=MemorySaver())

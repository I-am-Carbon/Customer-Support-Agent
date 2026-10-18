"""
The LangGraph wiring: who runs, in what order, and where control goes next.

  START -> triage -> order_agent  <-> order_tools  --(transfer)--> refund_agent
                  -> refund_agent <-> refund_tools
                  -> END (anything we don't support)
"""
from typing import Literal
from pydantic import BaseModel
from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, AIMessage
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver
from tools import ORDER_TOOLS, REFUND_TOOLS

llm = ChatGroq(model="openai/gpt-oss-120b", temperature=0)


# ---------- state ----------
class State(MessagesState):   # MessagesState already gives us a "messages" list
    active_agent: str         # which agent is handling the customer


# ---------- triage ----------
class Route(BaseModel):
    agent: Literal["order", "refund", "other"]

TRIAGE_PROMPT = """You route customer support messages.
- "order": order status, tracking, address changes
- "refund": refunds, money back, returns
- "other": anything else
Look at the latest customer message, using the conversation for context."""

def triage(state: State):
    route = llm.with_structured_output(Route).invoke(
        [SystemMessage(TRIAGE_PROMPT)] + state["messages"]
    )
    if route.agent == "other":
        reply = AIMessage("I can help with order status, address changes and refunds. What do you need?")
        return {"active_agent": "other", "messages": [reply]}
    return {"active_agent": route.agent}


# ---------- specialist agents ----------
ORDER_PROMPT = """You are the order specialist. You can look up orders and change addresses.
Always look up the order before answering. If the customer wants a refund,
call transfer_to_refund_agent. Never promise refunds yourself. Be brief."""

REFUND_PROMPT = """You are the refund specialist. Use request_refund to process refunds.
The tool enforces policy; report its result honestly and never override it.
If you don't know the order id, ask for it. Be brief."""

order_llm = llm.bind_tools(ORDER_TOOLS)     # this agent can ONLY see these tools
refund_llm = llm.bind_tools(REFUND_TOOLS)

def order_agent(state: State):
    reply = order_llm.invoke([SystemMessage(ORDER_PROMPT)] + state["messages"])
    return {"messages": [reply], "active_agent": "order"}

def refund_agent(state: State):
    reply = refund_llm.invoke([SystemMessage(REFUND_PROMPT)] + state["messages"])
    return {"messages": [reply], "active_agent": "refund"}


# ---------- routing functions (plain Python decides the next node) ----------
def after_triage(state: State):
    return {"order": "order_agent", "refund": "refund_agent", "other": END}[state["active_agent"]]

def needs_tools(state: State, tools_node: str):
    last = state["messages"][-1]
    return tools_node if getattr(last, "tool_calls", None) else END

def after_order_tools(state: State):
    if state["messages"][-1].name == "transfer_to_refund_agent":
        return "refund_agent"   # the handoff
    return "order_agent"


# ---------- build the graph ----------
builder = StateGraph(State)
builder.add_node("triage", triage)
builder.add_node("order_agent", order_agent)
builder.add_node("refund_agent", refund_agent)
builder.add_node("order_tools", ToolNode(ORDER_TOOLS))     # separate tool nodes =
builder.add_node("refund_tools", ToolNode(REFUND_TOOLS))   # scoping enforced in code

builder.add_edge(START, "triage")
builder.add_conditional_edges("triage", after_triage, ["order_agent", "refund_agent", END])
builder.add_conditional_edges("order_agent", lambda s: needs_tools(s, "order_tools"), ["order_tools", END])
builder.add_conditional_edges("refund_agent", lambda s: needs_tools(s, "refund_tools"), ["refund_tools", END])
builder.add_conditional_edges("order_tools", after_order_tools, ["order_agent", "refund_agent"])
builder.add_edge("refund_tools", "refund_agent")

graph = builder.compile(checkpointer=MemorySaver())

"""Chat with the support system in the terminal. You also play the human reviewer.
Run: python run.py [user_id] [thread_id]
  user_id   defaults to u1; try u2 to see ownership checks
  thread_id resumes a saved conversation (printed at start), even after a restart"""
import sys
import uuid
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from graph import graph

user_id = sys.argv[1] if len(sys.argv) > 1 else "u1"   # stands in for a real login
thread_id = sys.argv[2] if len(sys.argv) > 2 else f"{user_id}-{uuid.uuid4().hex[:8]}"
# One thread = one conversation. user_id rides along in config so every tool can read it.
config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
print(f"Support chat as {user_id}, thread {thread_id}. Type 'quit' to exit.\n")


def review(info):
    print(f"\n[REVIEW] Refund Rs {info['amount']} for order {info['order_id']} ({info['reason']})")
    return input("approve / reject: ").strip().lower()


# A refund may have been waiting for approval when the program last stopped.
pending = graph.get_state(config).interrupts
if pending:
    result = graph.invoke(Command(resume=review(pending[0].value)), config)
    print("Bot:", result["messages"][-1].content, "\n")

while True:
    text = input("You: ").strip()
    if text.lower() in {"quit", "exit"}:
        break

    result = graph.invoke({"messages": [HumanMessage(text)]}, config)

    # Paused for human approval? Ask the reviewer, then resume the same run.
    while "__interrupt__" in result:
        result = graph.invoke(Command(resume=review(result["__interrupt__"][0].value)), config)

    print("Bot:", result["messages"][-1].content, "\n")

"""Chat with the support system in the terminal. You also play the human reviewer.
Run: python run.py [user_id]   (default u1; try u2 to see ownership checks)"""
import sys
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from graph import graph

user_id = sys.argv[1] if len(sys.argv) > 1 else "u1"   # stands in for a real login
# One thread = one conversation. user_id rides along in config so every tool can read it.
config = {"configurable": {"thread_id": f"demo-{user_id}", "user_id": user_id}}
print(f"Support chat as {user_id}. Type 'quit' to exit.\n")

while True:
    text = input("You: ").strip()
    if text.lower() in {"quit", "exit"}:
        break

    result = graph.invoke({"messages": [HumanMessage(text)]}, config)

    # Paused for human approval? Ask the reviewer, then resume the same run.
    while "__interrupt__" in result:
        info = result["__interrupt__"][0].value
        print(f"\n[REVIEW] Refund Rs {info['amount']} for order {info['order_id']} ({info['reason']})")
        decision = input("approve / reject: ").strip().lower()
        result = graph.invoke(Command(resume=decision), config)

    print("Bot:", result["messages"][-1].content, "\n")

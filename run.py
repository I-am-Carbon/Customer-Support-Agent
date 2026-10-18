"""Chat with the support system in the terminal. You also play the human reviewer."""
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from graph import graph

config = {"configurable": {"thread_id": "demo"}}   # one thread = one conversation
print("Support chat. Type 'quit' to exit.\n")

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

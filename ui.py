"""
Demo UI: a customer side (chat, orders, refund history) and a reviewer approvals panel.

A thin client over the HTTP API. It never imports the graph or the database, so it goes
through the same auth, rate limiting and approval flow as any other client.
Run: streamlit run ui.py   (API_URL defaults to http://localhost:8000)
"""
import os
from datetime import datetime
import httpx
import streamlit as st

API_URL = os.environ.get("API_URL", "http://localhost:8000")
CUSTOMERS = ["u1", "u2"]
REVIEWER = "admin"

st.set_page_config(page_title="Support agent", layout="wide")


def api(method: str, path: str, user: str, **kwargs) -> httpx.Response:
    return httpx.request(method, API_URL + path, headers={"X-User-Id": user}, timeout=120, **kwargs)


def error_text(r: httpx.Response) -> str:
    detail = r.json().get("detail", r.text) if r.headers.get("content-type") == "application/json" else r.text
    if r.status_code == 429:
        return f"Rate limited, try again in {r.headers.get('Retry-After', '?')}s."
    return f"{r.status_code}: {detail}"


def pull_notifications(user: str, convo: dict) -> int:
    """Add notifications not shown yet in this session to the chat. Returns how many were new."""
    r = api("GET", "/notifications", user)
    if not r.is_success:
        st.error(error_text(r))
        return 0
    seen = st.session_state.seen[user]
    new = [n for n in r.json() if n["id"] not in seen]
    for n in new:
        seen.add(n["id"])
        convo["messages"].append({"role": "assistant", "content": f"🔔 {n['message']}"})
    return len(new)


def watch_for_decision(user: str, convo: dict):
    """Runs on a timer only while a refund is waiting for a reviewer (see below)."""
    if pull_notifications(user, convo):
        convo["waiting"] = False
        st.rerun()   # whole page, so the chat history shows the update


def when(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d %b %Y, %H:%M %Z")


def show_orders(user: str):
    r = api("GET", "/orders", user)
    if not r.is_success:
        st.error(error_text(r))
        return
    st.dataframe([{"Order": o["order_id"], "Item": o["item"], "Amount (₹)": o["amount"],
                   "Status": o["status"].replace("_", " "), "Ordered": o["ordered"],
                   "Delivered": o["delivered_on"] or "–", "Address": o["address"],
                   "Refunded": when(o["refunded_at"]) if o["refunded_at"] else "–"}
                  for o in r.json()], hide_index=True, width="stretch")


def show_refunds(user: str):
    r = api("GET", "/refunds", user)
    if not r.is_success:
        st.error(error_text(r))
    elif not r.json():
        st.info("No refunds yet.")
    else:
        st.dataframe([{"When": when(h["at"]), "Order": h["order_id"], "Item": h["item"],
                       "Amount (₹)": h["amount"], "Status": h["status"], "Detail": h["detail"]}
                      for h in r.json()], hide_index=True, width="stretch")


# One conversation per customer, kept in the browser session.
if "threads" not in st.session_state:
    st.session_state.threads = {u: {"thread_id": None, "messages": [], "waiting": False} for u in CUSTOMERS}
    st.session_state.seen = {u: set() for u in CUSTOMERS}   # notification ids already in the chat

st.title("Customer support agent")
chat_col, review_col = st.columns([3, 2], gap="large")

# ---------- customer side: chat, orders, refund history ----------
with chat_col:
    user = st.radio("Signed in as", CUSTOMERS, horizontal=True)
    convo = st.session_state.threads[user]

    chat_tab, orders_tab, refunds_tab = st.tabs(["Chat", "My orders", "Refund history"])

    with chat_tab:
        top = st.columns([1, 1])
        if top[0].button("New conversation"):
            convo.update(thread_id=None, messages=[], waiting=False)
        if top[1].button("Check notifications"):
            if pull_notifications(user, convo):
                convo["waiting"] = False
            else:
                st.toast("No new notifications.")

        st.caption("See your orders in the next tab. Refunds over ₹5,000 need a reviewer.")

        history = st.container(height=420)
        for m in convo["messages"]:
            history.chat_message(m["role"]).markdown(m["content"])

        # While a refund waits for a reviewer, check for the worker's update every few seconds.
        # A fragment reruns only itself, not the page. /notifications isn't rate limited (only /chat is).
        if convo["waiting"]:
            st.fragment(watch_for_decision, run_every=3)(user, convo)

        if prompt := st.chat_input("Ask about an order, a refund, an address change…"):
            convo["messages"].append({"role": "user", "content": prompt})
            history.chat_message("user").markdown(prompt)
            with history, st.spinner("Thinking…"):
                r = api("POST", "/chat", user, json={"message": prompt, "thread_id": convo["thread_id"]})
            if r.is_success:
                body = r.json()
                convo["thread_id"] = body["thread_id"]
                reply = body["reply"]
                if body["status"] == "pending_approval":
                    reply += "\n\n_⏳ Waiting for a reviewer (see the panel on the right)._"
                    convo["waiting"] = True
                convo["messages"].append({"role": "assistant", "content": reply})
            else:
                convo["messages"].append({"role": "assistant", "content": f"⚠️ {error_text(r)}"})
            st.rerun()

    with orders_tab:
        show_orders(user)
    with refunds_tab:
        show_refunds(user)


# ---------- reviewer panel ----------
with review_col:
    st.subheader(f"Pending approvals (as `{REVIEWER}`)")
    st.button("Refresh")
    r = api("GET", "/approvals", REVIEWER)
    if not r.is_success:
        st.error(error_text(r))
    elif not r.json():
        st.info("Nothing waiting for review.")
    for a in r.json() if r.is_success else []:
        with st.container(border=True):
            st.markdown(f"**Order {a['order_id']}** · ₹{a['amount']:,} · customer `{a['user_id']}`")
            st.caption(f"Reason: {a['reason']}")
            approve, reject = st.columns(2)
            decision = ("approve" if approve.button("Approve", key=f"ok-{a['id']}", type="primary")
                        else "reject" if reject.button("Reject", key=f"no-{a['id']}") else None)
            if decision:
                d = api("POST", f"/approvals/{a['id']}", REVIEWER, json={"decision": decision})
                if d.is_success:
                    st.toast(f"{decision.capitalize()}d. The worker will notify the customer.")
                    st.rerun()
                else:
                    st.error(error_text(d))

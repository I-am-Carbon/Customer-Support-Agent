"""
Demo UI: a customer chat window and a reviewer approvals panel, side by side.

A thin client over the HTTP API. It never imports the graph or the database, so it goes
through the same auth, rate limiting and approval flow as any other client.
Run: streamlit run ui.py   (API_URL defaults to http://localhost:8000)
"""
import os
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


# One conversation per customer, kept in the browser session.
if "threads" not in st.session_state:
    st.session_state.threads = {u: {"thread_id": None, "messages": []} for u in CUSTOMERS}

st.title("Customer support agent")
chat_col, review_col = st.columns([3, 2], gap="large")

# ---------- customer chat ----------
with chat_col:
    user = st.radio("Chatting as", CUSTOMERS, horizontal=True)
    convo = st.session_state.threads[user]

    top = st.columns([1, 1])
    if top[0].button("New conversation"):
        convo.update(thread_id=None, messages=[])
    if top[1].button("Check notifications"):
        r = api("GET", "/notifications", user)
        if r.is_success:
            for n in r.json():
                convo["messages"].append({"role": "assistant", "content": f"🔔 {n['message']}"})
            if not r.json():
                st.toast("No notifications yet.")
        else:
            st.error(error_text(r))

    st.caption("Seed orders: u1 has 101 (₹2,000), 102 (₹60,000), 105 (₹1,200); "
               "u2 has 103 (₹800), 104 (₹9,000). Refunds over ₹5,000 need a reviewer.")

    history = st.container(height=420)
    for m in convo["messages"]:
        history.chat_message(m["role"]).markdown(m["content"])

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
            convo["messages"].append({"role": "assistant", "content": reply})
        else:
            convo["messages"].append({"role": "assistant", "content": f"⚠️ {error_text(r)}"})
        st.rerun()

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

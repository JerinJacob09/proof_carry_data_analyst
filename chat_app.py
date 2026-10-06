"""Proof-Carrying Data Analyst: Streamlit chat front end (friend's mock-data demo).

Run with:  streamlit run chat_app.py
Uses mock "messy" tables inline (swap for pd.read_csv("data/orders.csv") etc. when ready).
"""

import inspect
import re

import pandas as pd
import streamlit as st

from agent.llm_prompt import generate_code
from sandbox.executor import ExecutionSandbox

MAX_RETRIES = 3

# Adapter: friend's UI was written against a `run_code(code) -> dict` helper.
# The real sandbox exposes ExecutionSandbox().run() -> SandboxResult,
# so we bridge it here into the dict shape normalize_result() understands.
_sandbox = ExecutionSandbox()


def run_code(code: str) -> dict:
    res = _sandbox.run(code)
    return {
        "success": res.success,
        "output": res.stdout,
        "error": res.stderr,
        "parsed_json": res.parsed_json,
    }


st.set_page_config(page_title="Proof-Carrying Data Analyst", page_icon="🧾", layout="wide")


# ---------------------------------------------------------------------------
# Mock "messy" data (swap for pd.read_csv("data/orders.csv") etc. when ready)
# ---------------------------------------------------------------------------
@st.cache_data
def load_mock_data() -> dict[str, tuple[pd.DataFrame, list[str]]]:
    orders = pd.DataFrame({
        "order_id": [1001, 1002, 1002, 1003, 1004, 1005],
        "user_id": [1, 2, 2, 3, None, 5],
        "order_date": ["2026-01-05", "05/01/2026", "05/01/2026", "Jan 7, 2026", "2026-13-40", None],
        "amount": ["$1,200.00", "89.5", "89.5", "-45", "N/A", "300"],
        "status": ["shipped", "SHIPPED ", "SHIPPED ", "Cancelled", "pending", "shipped"],
    })
    users = pd.DataFrame({
        "user_id": [1, 2, 3, 4, 5, 5],
        "name": ["Asha", "ben ", "CHEN", "Dara", None, None],
        "email": ["asha@x.com", "ben@x", None, "dara@x.com", "eli@x.com", "eli@x.com"],
        "country": ["IN", "india", "India", "US", "usa", "usa"],
    })
    inventory = pd.DataFrame({
        "sku": ["A-100", "a-100 ", "B-200", "C-300", "D-400"],
        "product": ["Widget", "Widget", "Gadget", "Gizmo", None],
        "stock": ["12", "twelve", "-3", "40", ""],
        "price": [9.99, 9.99, "19,99", 5.0, 0],
    })
    return {
        "orders.csv": (orders, [
            "Duplicate order_id (1002)",
            "Mixed / invalid date formats",
            "Currency strings, negatives, 'N/A' in amount",
            "Inconsistent status casing and whitespace",
            "Missing user_id",
        ]),
        "users.csv": (users, [
            "Duplicate user (5)",
            "Country spelled 4 different ways",
            "Missing / malformed emails",
            "Stray whitespace and casing in names",
        ]),
        "inventory.csv": (inventory, [
            "SKU casing / trailing whitespace duplicates",
            "Stock as text ('twelve'), negatives, blanks",
            "Comma decimal in price ('19,99')",
            "Zero price, missing product name",
        ]),
    }


def build_schema(data) -> str:
    """Compact schema description handed to the LLM."""
    parts = []
    for name, (df, _) in data.items():
        cols = ", ".join(f"{c} ({t})" for c, t in df.dtypes.astype(str).items())
        parts.append(f"{name}: {cols}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Adapters: tolerate small signature/return-shape differences while teammates
# finish agent/llm_prompt.py and sandbox/executor.py. Tweak once they land.
# ---------------------------------------------------------------------------
def call_generate_code(question: str, schema: str, prev_code=None, error=None) -> str:
    params = inspect.signature(generate_code).parameters
    candidates = {
        "question": question, "query": question, "user_question": question, "prompt": question,
        "schema": schema, "schema_context": schema, "data_context": schema, "context": schema,
        "data_summary": schema,
        "previous_code": prev_code, "prev_code": prev_code, "failed_code": prev_code,
        "error": error, "error_message": error, "traceback": error,
        "feedback": error, "last_error": error, "error_history": error,
    }
    kwargs = {k: v for k, v in candidates.items() if k in params and v is not None}
    code = generate_code(**kwargs) if kwargs else generate_code(question)
    return strip_fences(code)


def strip_fences(code: str) -> str:
    match = re.search(r"```(?:python)?\s*(.*?)```", code, re.DOTALL)
    return (match.group(1) if match else code).strip()


def normalize_result(res) -> tuple[bool, str, str]:
    """Return (success, output, error) from whatever run_code gives back."""
    if isinstance(res, dict):
        error = res.get("error") or res.get("traceback") or res.get("stderr") or ""
        output = res.get("output") or res.get("stdout") or res.get("result") or ""
        success = res.get("success", not error)
        return bool(success), str(output), str(error)
    if isinstance(res, tuple) and len(res) == 2:
        a, b = res
        if isinstance(a, bool):  # (success, output_or_error)
            return a, ("" if not a else str(b)), ("" if a else str(b))
        return (not b), str(a), str(b or "")  # (output, error)
    return True, str(res), ""


# ---------------------------------------------------------------------------
# Rendering helpers (used live inside st.status AND when replaying history)
# ---------------------------------------------------------------------------
def render_attempt(att: dict) -> None:
    label = "Initial attempt" if att["n"] == 1 else f"Retry {att['n'] - 1}: self-correction"
    st.markdown(f"**{label}**")
    st.code(att["code"], language="python")
    if att["success"]:
        st.success("Sandbox: executed successfully")
        if att["output"]:
            st.code(att["output"], language="text")
    else:
        st.error("Sandbox: execution failed")
        st.code(att["error"], language="text")


def render_trace(attempts: list[dict]) -> None:
    retries = len(attempts) - 1
    title = "Agent trace: first try" if retries == 0 else f"Agent trace: {retries} retr{'y' if retries == 1 else 'ies'}"
    with st.expander(title, expanded=False):
        for att in attempts:
            render_attempt(att)
            st.divider()


# ---------------------------------------------------------------------------
# Orchestrator loop
# ---------------------------------------------------------------------------
def run_agent(question: str, schema: str) -> dict:
    attempts: list[dict] = []
    code, error = None, None

    with st.status("Agent working...", expanded=True) as status:
        for n in range(1, MAX_RETRIES + 2):  # 1 initial + MAX_RETRIES retries
            if n == 1:
                status.update(label="Writing pandas code...")
            else:
                status.update(label=f"Retry {n - 1}/{MAX_RETRIES}: asking the LLM to fix the error...")
                st.warning(f"Attempt {n - 1} failed, sending the traceback back to the LLM.")

            try:
                code = call_generate_code(question, schema, prev_code=code, error=error)
            except Exception as exc:  # LLM/API failure
                status.update(label="LLM call failed", state="error")
                st.error(f"generate_code raised: {exc}")
                return {"ok": False, "answer": f"LLM call failed: {exc}", "attempts": attempts}

            status.update(label=f"Running in sandbox (attempt {n})...")
            try:
                success, output, error = normalize_result(run_code(code))
            except Exception as exc:
                success, output, error = False, "", f"{type(exc).__name__}: {exc}"

            att = {"n": n, "code": code, "success": success, "output": output, "error": error}
            attempts.append(att)
            render_attempt(att)

            if success:
                label = "Done on first try" if n == 1 else f"Fixed after {n - 1} retr{'y' if n == 2 else 'ies'}"
                status.update(label=label, state="complete", expanded=False)
                return {"ok": True, "answer": output or "(code ran but printed nothing)", "attempts": attempts}

        status.update(label="Gave up after max retries", state="error", expanded=True)
        return {
            "ok": False,
            "answer": f"I couldn't produce working code after {MAX_RETRIES} retries. Last error:\n\n```\n{error}\n```",
            "attempts": attempts,
        }


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
data = load_mock_data()
schema = build_schema(data)

with st.sidebar:
    st.header("Data preview")
    st.caption("The messy inputs the agent has to deal with.")
    for fname, (df, traps) in data.items():
        with st.expander(fname, expanded=(fname == "orders.csv")):
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.caption("Traps: " + " • ".join(traps))
    st.divider()
    if st.button("Clear chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

st.title("🧾 Proof-Carrying Data Analyst")
st.caption("Ask a question. The agent writes pandas code, proves it by running it in a sandbox, and self-corrects on failure.")

if "messages" not in st.session_state:
    st.session_state.messages = []

# Replay history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg.get("attempts"):
            render_trace(msg["attempts"])
        st.markdown(msg["content"])

# New turn
if question := st.chat_input("e.g. What was total revenue by country last month?"):
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        result = run_agent(question, schema)
        if result["ok"]:
            st.markdown(result["answer"])
        else:
            st.error("The agent could not complete this request.")
            st.markdown(result["answer"])

    st.session_state.messages.append({
        "role": "assistant",
        "content": result["answer"],
        "attempts": result["attempts"],
    })

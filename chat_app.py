"""Chat demo for the Proof-Carrying Data Analyst (same ReAct loop as app.py)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import streamlit as st

from agent.llm_prompt import REFUSAL, _resolve_api_key, _resolve_model, _resolve_provider
from agent.react_loop import MAX_RETRIES, run_react
from agent.schema import DATA_DIR, DEFAULT_TABLES, build_schema_context, load_frames

st.set_page_config(page_title="Proof-Carrying Data Analyst", page_icon="🧾", layout="wide")


def _workspace() -> Path:
    dest = Path(tempfile.mkdtemp(prefix="pcda_chat_"))
    for name in DEFAULT_TABLES:
        src = DATA_DIR / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    notes = DATA_DIR / "data_notes.md"
    if notes.is_file():
        shutil.copy2(notes, dest / notes.name)
    return dest


if "work_dir" not in st.session_state:
    st.session_state.work_dir = str(_workspace())

work = Path(st.session_state.work_dir)
frames = load_frames(work)
schema = build_schema_context(work, frames)

with st.sidebar:
    st.header("Data preview")
    st.caption("Rigged CSVs: duplicate IDs, mixed units (not just USD/EUR), missing dates. No color column.")
    for fname, df in frames.items():
        with st.expander(fname, expanded=(fname == "orders.csv")):
            st.dataframe(df.head(15), use_container_width=True, hide_index=True)
    st.divider()
    sidebar_key = st.text_input("API key (optional)", type="password")
    provider_choice = st.selectbox("Provider", ["auto", "groq", "gemini"])
    provider_arg = None if provider_choice == "auto" else provider_choice
    model = st.text_input("Model", value=_resolve_model(None, _resolve_provider(provider_arg)))
    if sidebar_key.strip() or _resolve_api_key(None, provider_arg):
        st.success("API key found.")
    else:
        st.warning("Add GROQ_API_KEY or GEMINI_API_KEY via Secrets, `.env`, or the field above.")
    if st.button("Clear chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

st.title("🧾 Proof-Carrying Data Analyst")
st.caption(
    f"Writes pandas, runs it in a sandbox, and self-corrects up to {MAX_RETRIES} times. "
    "Refuses questions the schema cannot support."
)

if "messages" not in st.session_state:
    st.session_state.messages = []


def render_attempt(att) -> None:
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
    retries = max(0, len(attempts) - 1)
    title = "Agent trace: first try" if retries == 0 else f"Agent trace: {retries} retr{'y' if retries == 1 else 'ies'}"
    with st.expander(title, expanded=False):
        for att in attempts:
            render_attempt(att)
            st.divider()


for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg.get("attempts"):
            render_trace(msg["attempts"])
        st.markdown(msg["content"])

if question := st.chat_input("e.g. How many unique orders are there?  /  How many blue shirts did we sell?"):
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        if not (sidebar_key.strip() or _resolve_api_key(None, provider_arg)):
            err = "Missing API key. Use the sidebar, `.env`, or Streamlit Secrets."
            st.error(err)
            st.session_state.messages.append({"role": "assistant", "content": err, "attempts": []})
        else:
            with st.status("Agent working...", expanded=True) as status:
                status.update(label="Writing pandas proof code and running the sandbox...")
                result = run_react(
                    question,
                    schema,
                    working_dir=str(work),
                    api_key=sidebar_key.strip() or None,
                    model=model.strip() or None,
                    provider=provider_arg,
                )
                serial = [
                    {
                        "n": a.n,
                        "code": a.code,
                        "success": a.success,
                        "output": a.stdout,
                        "error": a.stderr,
                    }
                    for a in result.attempts
                ]
                for att in serial:
                    render_attempt(att)
                if result.refused:
                    status.update(label="Refused: data cannot answer this", state="complete")
                    st.warning(REFUSAL)
                    content = REFUSAL
                elif result.ok:
                    status.update(label="Done", state="complete", expanded=False)
                    st.markdown(result.answer)
                    st.code(result.code, language="python")
                    content = result.answer
                else:
                    status.update(label="Gave up after max retries", state="error")
                    st.error("The agent could not complete this request.")
                    st.markdown(result.answer)
                    content = result.answer

            st.session_state.messages.append(
                {"role": "assistant", "content": content, "attempts": serial}
            )

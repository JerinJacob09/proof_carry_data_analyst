"""Streamlit UI for the Proof-Carrying Data Analyst (primary demo app).

Run:
    python -m streamlit run app.py

Render: Blueprint `render.yaml` (GitHub deploy) + GROQ_API_KEY in env.
Streamlit Cloud: set this file as the main file and add GROQ_API_KEY
(or GEMINI_API_KEY) under App settings → Secrets.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

import streamlit as st
from sandbox.executor import describe_isolation

from agent.llm_prompt import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GROQ_MODEL,
    REFUSAL,
    _resolve_api_key,
    _resolve_model,
    _resolve_provider,
    _sniff_provider_from_key,
)
from agent.react_loop import run_react
from agent.schema import DATA_DIR, DEFAULT_TABLES, build_schema_context, load_frames

st.set_page_config(page_title="Proof-Carrying Data Analyst", page_icon="🧾", layout="wide")


def _copy_default_csvs(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in DEFAULT_TABLES:
        src = DATA_DIR / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    notes = DATA_DIR / "data_notes.md"
    if notes.is_file():
        shutil.copy2(notes, dest / notes.name)


def _write_saved_uploads(saved: dict[str, bytes], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in saved.items():
        (dest / Path(name).name).write_bytes(data)


def _uploads_fingerprint(saved: dict[str, bytes]) -> tuple[tuple[str, str], ...]:
    """Name + content hash so same filename + new bytes still rebuilds the workspace."""
    return tuple(
        sorted((name, hashlib.sha256(data).hexdigest()) for name, data in saved.items())
    )


def _render_attempts(attempts) -> None:
    if not attempts:
        return
    retries = max(0, len(attempts) - 1)
    title = "Agent trace: first try" if retries == 0 else f"Agent trace: {retries} retr{'y' if retries == 1 else 'ies'}"
    with st.expander(title, expanded=len(attempts) > 1):
        for att in attempts:
            label = "Initial attempt" if att.n == 1 else f"Retry {att.n - 1}: self-correction"
            st.markdown(f"**{label}**")
            st.code(att.code, language="python")
            if att.success:
                st.success("Verified: read " + ", ".join(att.csv_files_read))
                if att.stdout:
                    st.code(att.stdout, language="text")
            else:
                st.error("Verification failed" if att.stderr.startswith("VerificationError:") else "Sandbox: execution failed")
                st.code(att.stderr, language="text")
            st.divider()


with st.sidebar:
    st.header("Setup")
    st.caption(
        "If Render already asked for GROQ_API_KEY, you are done — leave this blank. "
        "Otherwise paste a Groq key here (starts with gsk_). Never commit keys."
    )
    provider_options = ["auto", "groq", "gemini"]
    provider_choice = st.selectbox("LLM provider", provider_options, index=0)
    sidebar_key = st.text_input("API key (optional override)", type="password")
    default_provider = _resolve_provider(None if provider_choice == "auto" else provider_choice)
    default_model = _resolve_model(None, default_provider)
    # Key the widget on provider_choice so Streamlit resets the value whenever
    # the provider changes instead of holding the stale model name from the
    # previous provider.
    model = st.text_input("Model", value=default_model, key=f"model_{provider_choice}")
    st.caption(f"Defaults: Groq `{DEFAULT_GROQ_MODEL}` · Gemini `{DEFAULT_GEMINI_MODEL}`")

    provider_arg = None if provider_choice == "auto" else provider_choice
    effective_key = sidebar_key.strip() or _resolve_api_key(None, provider_arg) or ""
    # Sniff the actual provider the key belongs to so the status message is accurate
    # even when the dropdown and the key prefix disagree.
    if effective_key:
        sniffed = _sniff_provider_from_key(effective_key)
        display_provider = sniffed or default_provider
        if sniffed and provider_arg and sniffed != provider_arg:
            st.warning(
                f"Key looks like a **{sniffed}** key but provider is set to **{provider_arg}**. "
                f"Will use **{sniffed}** automatically."
            )
        else:
            st.success(f"API key found ({display_provider}).")
    else:
        st.warning("No API key yet. Paste a Groq key above, or set GROQ_API_KEY on Render.")
    
    _level, _msg = describe_isolation()
    {"ok": st.success, "refused": st.error, "degraded": st.warning}[_level](_msg)

    use_builtin = st.checkbox("Use built-in messy CSVs (orders / users / inventory)", value=True)
    if "_csv_uploader_n" not in st.session_state:
        st.session_state._csv_uploader_n = 0
    if "_saved_uploads" not in st.session_state:
        st.session_state._saved_uploads = {}
    uploaded = st.file_uploader(
        "Or upload CSV table(s)",
        type=["csv"],
        accept_multiple_files=True,
        key=f"csv_tables_{st.session_state._csv_uploader_n}",
        help="Re-upload the same filename to replace that table. Streamlit otherwise keeps the previous file.",
    )
    if uploaded:
        saved = dict(st.session_state._saved_uploads)
        for f in uploaded:
            saved[Path(f.name).name] = f.getvalue()
        st.session_state._saved_uploads = saved
        st.session_state._csv_uploader_n += 1
        st.rerun()

st.title("🧾 Proof-Carrying Data Analyst")
st.caption(
    "Question → LLM writes pandas proof code → isolated sandbox → up to 3 self-correction retries. "
    "Refuses trick questions the data cannot answer. Mixed units are not assumed to be only USD/EUR."
)

saved_uploads: dict[str, bytes] = st.session_state.get("_saved_uploads") or {}
ws_key = (use_builtin, _uploads_fingerprint(saved_uploads))
if st.session_state.get("_ws_key") != ws_key:
    old_work = st.session_state.get("work_dir")
    work = Path(tempfile.mkdtemp(prefix="pcda_"))
    if use_builtin:
        _copy_default_csvs(work)
    if saved_uploads:
        _write_saved_uploads(saved_uploads, work)
    st.session_state._ws_key = ws_key
    st.session_state.work_dir = str(work)
    if old_work and Path(old_work) != work:
        shutil.rmtree(old_work, ignore_errors=True)
work = Path(st.session_state.work_dir)

frames = load_frames(work)
if not frames:
    st.info("Enable the built-in messy CSVs or upload at least one CSV.")
    st.stop()

st.subheader("Loaded tables")
for name, df in frames.items():
    with st.expander(f"`{name}` — {df.shape[0]} rows, {df.shape[1]} cols", expanded=(name == "orders.csv")):
        st.dataframe(df.head(20), use_container_width=True)

schema_context = build_schema_context(work, frames)
with st.expander("Schema context sent to the LLM"):
    st.code(schema_context)

examples = [
    "How many unique orders are there?",
    "What is the total quantity of items ordered across all unique orders?",
    "What is the total revenue in USD?",
    "How many blue shirts did we sell?",
    "How many orders were placed in April 2025?",
    "What is the total EUR revenue (price x quantity) from Completed orders with a clearly stated EUR currency, excluding orders with conflicting duplicate rows?",
    "How many distinct currency units appear in orders.csv prices (ignore blank/unlabeled amounts)?",
]
typed = st.text_area(
    "Analytical question",
    placeholder="e.g. How many unique orders are there?",
    height=90,
)
st.caption("Try a trick question such as “How many blue shirts did we sell?” — there is no color column.")
picked = st.selectbox("Example questions", ["(pick an example)"] + examples)
question = typed.strip() or ("" if picked.startswith("(") else picked)

run = st.button("Generate proof + run", type="primary", disabled=not question.strip())

if run:
    if not (sidebar_key.strip() or _resolve_api_key(None, provider_arg)):
        st.error("Missing API key. Use the sidebar, `.env`, or Streamlit Secrets.")
        st.stop()

    with st.spinner("Reason + Act: generating proof code and running it in the sandbox..."):
        result = run_react(
            question.strip(),
            schema_context,
            working_dir=str(work),
            api_key=sidebar_key.strip() or None,
            model=model.strip() or None,
            provider=provider_arg,
        )

    _render_attempts(result.attempts)

    if result.refused:
        st.warning(REFUSAL)
        st.caption("The agent refused rather than hallucinating an answer from missing columns or unresolvable traps.")
    elif not result.ok:
        st.error(result.answer)
    else:
        st.subheader("Result (CSV read verified)")
        st.code(result.answer, language="text")
        st.subheader("Proof code")
        st.code(result.code, language="python")
        st.success("Verified: the run successfully read " + ", ".join(result.attempts[-1].csv_files_read) + ".")

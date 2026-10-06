"""Streamlit interface for the Proof-Carrying Data Analyst.

Run on any laptop:
    pip install -r requirements.txt
    # create .env with GROQ_API_KEY=gsk_... (gitignored)
    streamlit run app.py

Key priority (handled in agent/llm_prompt.py):
    sidebar input > env var / .env file
"""

import contextlib
import io
import re
import sys
import traceback
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.llm_prompt import REFUSAL, _resolve_api_key, _resolve_model, generate_code

st.set_page_config(page_title="Proof-Carrying Data Analyst", layout="wide")
st.title("Proof-Carrying Data Analyst")
st.caption("Question → Groq generates pandas proof code → executed + verified. Refuses when data is insufficient.")


def _sanitize_var(name: str) -> str:
    stem = Path(name).stem
    clean = re.sub(r"\W+", "_", stem).strip("_").lower()
    if not clean:
        clean = "table"
    if clean[0].isdigit():
        clean = "t_" + clean
    return clean


def _build_schema_context(tables: dict) -> str:
    lines = []
    for var, df in tables.items():
        lines.append(f"Table variable `{var}`: {df.shape[0]} rows x {df.shape[1]} cols")
        for col in df.columns:
            nulls = int(df[col].isna().sum())
            lines.append(f"  - {col}: dtype={df[col].dtype}, nulls={nulls}, nunique={df[col].nunique(dropna=True)}")
        # Small sample so the model sees formatting (dates, currencies, units).
        try:
            sample = df.head(3).to_string(index=False)
        except Exception:
            sample = "(sample unavailable)"
        lines.append(f"  Sample:\n{sample}")
    return "\n".join(lines) if lines else "(no tables loaded)"


def _run_code(code: str, tables: dict):
    """Execute generated code with DataFrames in scope. Returns (stdout, error)."""
    buf = io.StringIO()
    namespace = {"pd": pd, "pandas": pd, **tables}
    try:
        with contextlib.redirect_stdout(buf):
            exec(code, {"__builtins__": __builtins__}, namespace)
        return buf.getvalue().strip(), None
    except Exception as e:  # noqa: BLE001 - show any proof failure to user
        err = f"{type(e).__name__}: {e}"
        return buf.getvalue().strip(), err


# ---------- Sidebar: portable key handling ----------
with st.sidebar:
    st.header("Setup")
    st.markdown("Key is **never committed**. Pick one per laptop:")
    st.markdown("1. Paste below, 2. `.env` file (`GROQ_API_KEY=...`)")
    sidebar_key = st.text_input("GROQ_API_KEY", value="", type="password", help="Get one at console.groq.com/keys")
    default_model = _resolve_model()
    model = st.text_input("GROQ_MODEL", value=default_model)
    do_retry = st.checkbox("Auto-retry once on execution error", value=True)

    effective_key = (sidebar_key.strip() or _resolve_api_key() or "")
    if effective_key:
        st.success("API key found.")
    else:
        st.warning("No API key. Paste it above or add GROQ_API_KEY to .env.")

# ---------- Main: data + question ----------
uploaded = st.file_uploader("Upload CSV table(s)", type=["csv"], accept_multiple_files=True)

tables: dict = {}
if uploaded:
    for f in uploaded:
        try:
            df = pd.read_csv(f)
            tables[_sanitize_var(f.name)] = df
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not read {f.name}: {e}")

if tables:
    st.subheader("Loaded tables")
    for var, df in tables.items():
        with st.expander(f"`{var}` — {df.shape[0]} rows, {df.shape[1]} cols"):
            st.dataframe(df.head(20), use_container_width=True)
    schema_context = _build_schema_context(tables)
    with st.expander("Schema context sent to Groq"):
        st.code(schema_context)
else:
    st.info("Upload at least one CSV to start. Your friend does the same on their laptop — no code changes needed.")
    schema_context = ""

question = st.text_area(
    "Analytical question",
    placeholder="e.g. What is the average order value in USD? Refuse if currencies are mixed.",
    height=100,
)

run = st.button("Generate proof + run", type="primary", disabled=not (tables and question.strip()))

if run:
    api_key = sidebar_key.strip() or None  # None -> generate_code resolves env/.env
    if not (api_key or _resolve_api_key()):
        st.error("Missing GROQ_API_KEY. Paste it in the sidebar or add it to `.env`.")
        st.stop()

    with st.spinner("Asking Groq for proof code..."):
        try:
            code = generate_code(question.strip(), schema_context, error_history=None, api_key=api_key, model=model.strip() or None)
        except Exception as e:  # noqa: BLE001 - e.g. missing key, network
            st.error(str(e))
            st.stop()

    if code.strip() == REFUSAL:
        st.warning(REFUSAL)
        st.stop()

    st.subheader("Generated proof code")
    st.code(code, language="python")

    stdout, error = _run_code(code, tables)

    if error and do_retry:
        st.error(f"First attempt failed: {error}")
        with st.spinner("Retrying with error history..."):
            try:
                code2 = generate_code(question.strip(), schema_context, error_history=error, api_key=api_key, model=model.strip() or None)
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
                st.stop()
        if code2.strip() == REFUSAL:
            st.warning(REFUSAL)
            st.stop()
        st.subheader("Retried proof code")
        st.code(code2, language="python")
        stdout, error = _run_code(code2, tables)
        code = code2

    if error:
        st.error(f"Execution failed: {error}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc())
    else:
        st.subheader("Verified result (program output)")
        st.code(stdout or "(no output — code did not print anything)", language="text")
        st.success("Code ran. Check asserts passed and the computation uses the data (no hard-coded answer).")

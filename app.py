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

import numpy as np
import pandas as pd
import streamlit as st

from agent.llm_prompt import REFUSAL, _resolve_api_key
from agent.document_inputs import SUPPORTED_EXTENSIONS, stage_uploads
from agent.proof_bundle import build_proof_bundle
from agent.react_loop import run_react
from agent.schema import DATA_DIR, DEFAULT_TABLES, build_schema_context, load_frames

st.set_page_config(page_title="Proof-Carrying Data Analyst", page_icon="🧾", layout="wide")


def _copy_default_csvs(dest: Path, selected_tables: tuple[str, ...]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in selected_tables:
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
                st.success("Proof replay matched · CSVs read: " + ", ".join(att.csv_files_read))
                if att.stdout:
                    st.code(att.stdout, language="text")
            else:
                st.error("Verification failed" if att.stderr.startswith("VerificationError:") else "Sandbox: execution failed")
                st.code(att.stderr, language="text")
            st.divider()


st.title("🧾 Proof-Carrying Data Analyst")
st.caption(
    "Upload CSVs, PDFs, Word documents, or graph images, ask a question in plain language, and review the result and generated code. "
    "Questions the data cannot answer are refused."
)
if not _resolve_api_key():
    st.info("This demo needs an API key configured in the environment (`GROQ_API_KEY` or `GEMINI_API_KEY`).")

if "_csv_uploader_n" not in st.session_state:
    st.session_state._csv_uploader_n = 0
if "_saved_uploads" not in st.session_state:
    st.session_state._saved_uploads = {}
for _table in DEFAULT_TABLES:
    _table_key = f"include_{Path(_table).stem}"
    if _table_key not in st.session_state:
        st.session_state[_table_key] = True

upload_left, upload_center, upload_right = st.columns([1, 2, 1])
with upload_center:
    with st.container(border=True):
        st.subheader("1. Add your data and documents")
        st.caption("Upload CSV tables, PDFs, Word documents (.docx), or graph images. PDFs and Word files become searchable text tables; graph images are read by the configured vision model.")
        uploaded = st.file_uploader(
            "Upload files",
            type=[ext.lstrip(".") for ext in sorted(SUPPORTED_EXTENSIONS)],
            accept_multiple_files=True,
            key=f"csv_tables_{st.session_state._csv_uploader_n}",
            help="Files with the same name replace each other.",
        )
        if uploaded:
            saved = dict(st.session_state._saved_uploads)
            for f in uploaded:
                saved[Path(f.name).name] = f.getvalue()
            st.session_state._saved_uploads = saved
            for table in DEFAULT_TABLES:
                st.session_state[f"include_{Path(table).stem}"] = False
            st.session_state._csv_uploader_n += 1
            st.rerun()

        if st.session_state._saved_uploads:
            names = ", ".join(sorted(st.session_state._saved_uploads))
            st.success(f"Uploaded: {names}")
            if st.button("Remove uploaded files", use_container_width=True):
                st.session_state._saved_uploads = {}
                st.session_state._csv_uploader_n += 1
                st.rerun()

st.caption("Choose which predefined CSV files to include:")
table_columns = st.columns(len(DEFAULT_TABLES))
selected_tables = tuple(
    table for column, table in zip(table_columns, DEFAULT_TABLES)
    if column.checkbox(
        Path(table).stem.title(),
        key=f"include_{Path(table).stem}",
        help=f"Include the predefined {table} file.",
    )
)
saved_uploads: dict[str, bytes] = st.session_state.get("_saved_uploads") or {}
ws_key = (selected_tables, _uploads_fingerprint(saved_uploads))
if st.session_state.get("_ws_key") != ws_key:
    old_work = st.session_state.get("work_dir")
    work = Path(tempfile.mkdtemp(prefix="pcda_"))
    if selected_tables:
        _copy_default_csvs(work, selected_tables)
    if saved_uploads:
        try:
            stage_uploads(saved_uploads, work)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not process uploaded files: {exc}")
            st.stop()
    st.session_state._ws_key = ws_key
    st.session_state.work_dir = str(work)
    if old_work and Path(old_work) != work:
        shutil.rmtree(old_work, ignore_errors=True)
work = Path(st.session_state.work_dir)

try:
    frames = load_frames(work)
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not process an uploaded data file: {exc}")
    st.info("Check that the file is valid and under 20 MB, then remove it and upload it again.")
    st.stop()
if not frames:
    st.info("Upload at least one supported file above, or select a predefined CSV file.")
    st.stop()

schema_context = build_schema_context(work, frames)
with st.expander("Preview loaded tables", expanded=False):
    for name, df in frames.items():
        st.markdown(f"**{name}** · {df.shape[0]} rows · {df.shape[1]} columns")
        st.dataframe(df.head(10), use_container_width=True, hide_index=True)
with st.expander("Schema context sent to the model"):
    st.code(build_schema_context(work, frames))

examples = [
    "How many unique orders are there?",
    "What is the total quantity of items ordered across all unique orders?",
    "What is the total revenue in USD?",
    "How many blue shirts did we sell?",
    "How many orders were placed in April 2025?",
    "What is the total EUR revenue (price x quantity) from Completed orders with a clearly stated EUR currency, excluding orders with conflicting duplicate rows?",
    "How many distinct currency units appear in orders.csv prices (ignore blank/unlabeled amounts)?",
]
st.subheader("2. Ask a question")
picked = st.selectbox("Start with an example (optional)", ["(choose an example)"] + examples)
typed = st.text_area(
    "Your question",
    placeholder="For example: How many unique orders are there?",
    height=90,
    help="Type your own question, or leave this blank to use the example above.",
)
question = typed.strip() or ("" if picked.startswith("(") else picked)

run_left, run_center, run_right = st.columns([1, 2, 1])
with run_center:
    run = st.button("3. Analyze data", type="primary", use_container_width=True, disabled=not question.strip())

if run:
    if not _resolve_api_key():
        st.error("No API key is configured. Add `GROQ_API_KEY` or `GEMINI_API_KEY` to the app environment and restart it.")
        st.stop()

    with st.spinner("Reason + Act: generating proof code and running it in the sandbox..."):
        result = run_react(
            question.strip(),
            schema_context,
            working_dir=str(work),
        )

    _render_attempts(result.attempts)

    if result.refused:
        st.warning(REFUSAL)
        st.caption("The agent refused rather than hallucinating an answer from missing columns or unresolvable traps.")
    elif not result.ok:
        st.error(result.answer)
    else:
        st.subheader("Result")
        st.code(result.answer, language="text")
        st.subheader("Proof code")
        st.code(result.code, language="python")
        proof_files = result.attempts[-1].csv_files_read
        st.success("Proof replay matched. CSVs read: " + ", ".join(proof_files) + ".")
        st.download_button(
            "Download rerunnable proof + CSVs",
            data=build_proof_bundle(
                question.strip(), result.answer, result.code, proof_files, work,
                pandas_version=pd.__version__, numpy_version=np.__version__,
            ),
            file_name="proof_bundle.zip",
            mime="application/zip",
            use_container_width=True,
        )

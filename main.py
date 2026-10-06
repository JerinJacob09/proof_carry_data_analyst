"""Website backend for the Proof-Carrying Data Analyst (custom site + FastAPI).

Run on any laptop:
    pip install -r requirements.txt
    python -m uvicorn main:app --reload

Then open http://127.0.0.1:8000

API key priority (never commit a real key):
    request field > env var / .env file
The frontend key field is optional if the server already has one of the others.
"""

import re
import tempfile
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent.llm_prompt import REFUSAL, generate_code
from sandbox.executor import ExecutionSandbox

app = FastAPI(title="Proof-Carrying Data Analyst")
sandbox = ExecutionSandbox()
WEB_DIR = Path(__file__).resolve().parent / "web"


def _safe_csv_name(name: str, i: int) -> str:
    stem = Path(name).stem if name else f"table{i}"
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or f"table{i}"
    return f"{clean}.csv"


def _build_schema_context(frames: dict) -> str:
    """Schema context that tells Groq the CSV filenames to pd.read_csv()."""
    lines = [
        "CSV files are in the script's working directory. "
        "Load each with pandas, e.g. df = pd.read_csv('orders.csv')."
    ]
    for fname, df in frames.items():
        var = Path(fname).stem
        lines.append(f"File `{fname}` (suggested variable `{var}`): {df.shape[0]} rows x {df.shape[1]} cols")
        for col in df.columns:
            lines.append(
                f"  - {col}: dtype={df[col].dtype}, "
                f"nulls={int(df[col].isna().sum())}, "
                f"nunique={df[col].nunique(dropna=True)}"
            )
        try:
            lines.append(f"  Sample:\n{df.head(3).to_string(index=False)}")
        except Exception:
            lines.append("  Sample: (unavailable)")
    return "\n".join(lines)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/analyze")
async def analyze(
    question: str = Form(...),
    files: list[UploadFile] = File(...),
    api_key: str = Form(""),
    model: str = Form(""),
    auto_retry: bool = Form(True),
):
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "Question is empty."}
    csvs = [f for f in (files or []) if (f.filename or "").lower().endswith(".csv")]
    if not csvs:
        return {"ok": False, "error": "Upload at least one .csv file."}

    key = (api_key or "").strip() or None
    mdl = (model or "").strip() or None

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        frames: dict = {}
        for i, f in enumerate(csvs):
            safe = _safe_csv_name(f.filename or f"table{i}", i)
            data = await f.read()
            (tmpdir / safe).write_bytes(data)
            try:
                frames[safe] = pd.read_csv(tmpdir / safe)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"Could not read {f.filename}: {exc}"}

        schema_context = _build_schema_context(frames)

        try:
            code = generate_code(question, schema_context, api_key=key, model=mdl)
        except RuntimeError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "hint": "Paste your key in the website key field, or set it in .env / env var. See README.",
            }
        except Exception as exc:  # noqa: BLE001 - e.g. network/Groq error
            return {"ok": False, "error": f"Groq request failed: {exc}"}

        if code.strip() == REFUSAL:
            return {"ok": True, "refusal": True, "code": code, "result": REFUSAL}

        attempts = []
        current = code
        for attempt_no in (1, 2 if auto_retry else 1):
            res = sandbox.run(current, working_dir=str(tmpdir))
            attempts.append(
                {
                    "attempt": attempt_no,
                    "code": current,
                    "success": res.success,
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                    "parsed_json": res.parsed_json,
                }
            )
            if res.success:
                return {"ok": True, "refusal": False, "attempts": attempts, "result": res.stdout, "parsed_json": res.parsed_json}
            if attempt_no == 1 and auto_retry:
                err = res.stderr.strip() or "Execution failed with no stderr."
                try:
                    fixed = generate_code(question, schema_context, error_history=err, api_key=key, model=mdl)
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "error": f"Retry generation failed: {exc}", "attempts": attempts}
                if fixed.strip() == REFUSAL:
                    return {"ok": True, "refusal": True, "code": fixed, "result": REFUSAL, "attempts": attempts}
                current = fixed

        last = attempts[-1]
        return {"ok": False, "error": f"Execution failed: {last['stderr'][:500]}", "attempts": attempts}


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


@app.get("/", include_in_schema=False)
def root():
    index = WEB_DIR / "index.html"
    if index.is_file():
        return FileResponse(str(index))
    return {"ok": True, "hint": "Frontend not built yet. Use POST /api/analyze."}

"""Website backend for the Proof-Carrying Data Analyst (optional FastAPI UI)."""

import re
import shutil
import tempfile
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.staticfiles import StaticFiles

from agent.llm_prompt import REFUSAL
from agent.react_loop import run_react
from agent.schema import DATA_DIR, DEFAULT_TABLES, build_schema_context

app = FastAPI(title="Proof-Carrying Data Analyst")
WEB_DIR = Path(__file__).resolve().parent / "web"


def _safe_csv_name(name: str, i: int) -> str:
    stem = Path(name).stem if name else f"table{i}"
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or f"table{i}"
    return f"{clean}.csv"


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/analyze")
async def analyze(
    question: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    api_key: str = Form(""),
    model: str = Form(""),
    auto_retry: bool = Form(True),
    provider: str = Form(""),
    use_builtin: bool = Form(True),
):
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "Question is empty."}

    csvs = [f for f in (files or []) if (f.filename or "").lower().endswith(".csv")]
    if not csvs and not use_builtin:
        return {"ok": False, "error": "Upload at least one .csv file, or enable the built-in messy CSVs."}

    key = (api_key or "").strip() or None
    mdl = (model or "").strip() or None
    prov = (provider or "").strip() or None

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        frames: dict = {}

        # Stage built-in CSVs first so uploads can selectively override them.
        if use_builtin and not csvs:
            for name in DEFAULT_TABLES:
                src = DATA_DIR / name
                if src.is_file():
                    shutil.copy2(src, tmpdir / name)
            notes = DATA_DIR / "data_notes.md"
            if notes.is_file():
                shutil.copy2(notes, tmpdir / notes.name)
            for name in DEFAULT_TABLES:
                dest = tmpdir / name
                if dest.is_file():
                    try:
                        frames[name] = pd.read_csv(dest)
                    except Exception as exc:  # noqa: BLE001
                        return {"ok": False, "error": f"Could not read built-in {name}: {exc}"}

        for i, f in enumerate(csvs):
            safe = _safe_csv_name(f.filename or f"table{i}", i)
            data = await f.read()
            (tmpdir / safe).write_bytes(data)
            try:
                frames[safe] = pd.read_csv(tmpdir / safe)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"Could not read {f.filename}: {exc}"}

        schema_context = build_schema_context(tmpdir, frames)
        result = run_react(
            question,
            schema_context,
            working_dir=str(tmpdir),
            api_key=key,
            model=mdl,
            provider=prov,
            max_retries=3 if auto_retry else 0,
        )

        attempts = [
            {
                "attempt": a.n,
                "code": a.code,
                "success": a.success,
                "stdout": a.stdout,
                "stderr": a.stderr,
            }
            for a in result.attempts
        ]

        if result.refused:
            return {"ok": True, "refusal": True, "code": REFUSAL, "result": REFUSAL, "attempts": attempts}
        if not result.ok:
            return {
                "ok": False,
                "error": result.error or result.answer,
                "attempts": attempts,
                "hint": "Paste your key in the website key field, or set GROQ_API_KEY / GEMINI_API_KEY in .env.",
            }
        return {
            "ok": True,
            "refusal": False,
            "attempts": attempts,
            "result": result.answer,
            "code": result.code,
            "parsed_json": result.parsed_json,
        }


@app.get("/", include_in_schema=False)
def root():
    """Fallback used only when web/ does not exist (no StaticFiles mount)."""
    return {"ok": True, "hint": "Frontend not built yet. Use POST /api/analyze."}


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

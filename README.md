# Proof-Carrying Data Analyst

Question → Groq generates pandas proof code → executed + verified.
Refuses with `I cannot determine this.` when the data is insufficient.

## Setup (every laptop does this)

```powershell
cd proof_carry_data_analyst
pip install -r requirements.txt
```

## API key (pick ONE per laptop, never commit the real key)

Key priority: **key field (Streamlit sidebar / website) > env var > `.env` > `.streamlit/secrets.toml`**

**Option A — Key field (fastest, no file):**
Just run the app and paste your key in the sidebar (Streamlit) or top field (website).

**Option B — Streamlit secrets file:**
```powershell
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
```
Then edit `.streamlit\secrets.toml`:
```toml
GROQ_API_KEY = "gsk_your_real_key_here"
```

**Option C — `.env` file (also works outside Streamlit):**
```powershell
Copy-Item .env.example .env
```
Then edit `.env`:
```
GROQ_API_KEY=gsk_your_real_key_here
```

Get a key at https://console.groq.com/keys

## What gets pushed to git vs. what stays local

| Committed (template, placeholder) | Local only (real key, ignored) |
|---|---|
| `.streamlit/secrets.toml.example` | `.streamlit/secrets.toml` |
| `.env.example` | `.env` |

So yes: we push the `.example` files, and each user copies them and pastes their own key. The sidebar field overrides everything for demos.

## Run — website (custom site + FastAPI, recommended)

```powershell
python -m uvicorn main:app --reload
```
Then open http://127.0.0.1:8000 — upload 1+ CSVs → type a question → `Generate proof + run`.
The website key field is optional if the server already has a key (env / `.env` / secrets).

Default model is `openai/gpt-oss-120b` (override with `GROQ_MODEL`).
`llama-3.3-70b-versatile` was retired by Groq in Aug 2026, so don't use it.

## Run — Streamlit (alternative UI)

```powershell
# NOTE: use python -m (bare `streamlit` is not on PATH for user installs)
python -m streamlit run app.py       # CSV uploader flow
python -m streamlit run chat_app.py  # chat demo with mock messy data + retry trace
```

Then: upload 1+ CSVs (or use the built-in mock data in `chat_app.py`) → type a question → run.

## Host on Streamlit Cloud (`xxx.streamlit.app`)

1. Push latest to GitHub.
2. Go to share.streamlit.io → New app → pick repo/branch.
3. Main file: `app.py` (or `chat_app.py` for the chat demo — only one; never `main.py`, that's the FastAPI backend).
4. App Settings → Secrets, paste:
```toml
GROQ_API_KEY = "gsk_your_real_key_here"
```
No code change needed — the app already reads `st.secrets`.

## How it fits together

* `agent/llm_prompt.py` — Groq interface. Forces raw pandas code or exactly `I cannot determine this.` Handles `.env` + secrets + sidebar/request keys, markdown stripping, error-history retry.
* `sandbox/executor.py` — runs generated code isolated (subprocess, 10s default timeout), captures stdout/stderr, extracts proof JSON.
* `main.py` + `web/index.html` — website. `POST /api/analyze` saves CSVs to a temp dir, builds schema context with `pd.read_csv()` filenames, calls `generate_code()`, runs via sandbox, auto-retries once. Serves the frontend at `/`.
* `app.py` — Streamlit CSV-uploader UI (same flow, in-process execution).
* `chat_app.py` — Streamlit chat demo with inline mock messy data, agent retry loop, and trace rendering.
* `data/messy_data_gen.py` — teammate-owned (currently empty placeholder).

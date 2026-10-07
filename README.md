"""Proof-Carrying Data Analyst

Question → LLM writes pandas proof code → isolated sandbox → verified result.
If the sandbox crashes, the traceback is fed back to the model (up to 3 retries).
Trick questions the schema cannot support are refused with `I cannot determine this.`

## Setup

```powershell
cd proof_carry_data_analyst
pip install -r requirements.txt
python data/messy_data_gen.py
```

## API keys (never commit)

Priority: **sidebar / request field → environment / `.env` → Streamlit Secrets**.

Local `.env` (gitignored):

```
GROQ_API_KEY=gsk_your_real_key_here
# or
# GEMINI_API_KEY=your_gemini_key_here
# LLM_PROVIDER=groq
# GROQ_MODEL=openai/gpt-oss-120b
```

Get a Groq key at https://console.groq.com/keys

## Run the Streamlit demo (hackathon entry)

```powershell
python -m streamlit run app.py
```

Chat-style UI:

```powershell
python -m streamlit run chat_app.py
```

Built-in tables: `data/orders.csv`, `data/users.csv`, `data/inventory.csv`.
They contain duplicate IDs, mixed units (USD, EUR, GBP, INR, JPY, CAD, AUD — not just two currencies), and missing/ambiguous dates.
There is **no color column** — “How many blue shirts did we sell?” must be refused.

## Host on Streamlit Community Cloud

1. Push this repo to GitHub.
2. https://share.streamlit.io → New app → this repo/branch.
3. Main file: `app.py` (use `chat_app.py` only if you want the chat demo).
4. App settings → Secrets → paste `GROQ_API_KEY` (see `.streamlit/secrets.toml.example`).

## Optional FastAPI site

```powershell
python -m uvicorn main:app --reload
```

<<<<<<< HEAD
Default model is `openai/gpt-oss-120b` (override with `GROQ_MODEL`).
`llama-3.3-70b-versatile` was retired by Groq in Aug 2026, so don't use it.

## Run — Streamlit (alternative UI)

```powershell
# NOTE: use python -m (bare `streamlit` is not on PATH for user installs)
python -m streamlit run app.py       # CSV uploader flow
python -m streamlit run chat_app.py  # chat demo with mock messy data + retry trace
```

Then: upload 1+ CSVs (or use the built-in mock data in `chat_app.py`) → type a question → run.

## Test

```powershell
# 1. Key wiring (no API cost) — should print True + model name
python -c "from agent.llm_prompt import _resolve_api_key, _resolve_model; print(bool(_resolve_api_key()), _resolve_model())"
# .env must stay local — should print ".gitignore:2:.env" and NOT appear in status
git check-ignore -v .env
git status --short

# 2. Sandbox only (no API cost) — should print True '4'
python -c "from sandbox.executor import ExecutionSandbox; r=ExecutionSandbox().run('print(2+2)'); print(r.success, repr(r.stdout))"

# 3. Live Groq call (uses key) — should return runnable python with print(...)
python -c "from agent.llm_prompt import generate_code; print(generate_code('What is 2+2?', 'No tables needed, just print 2+2.'))"
```

If step 3 says `GROQ_API_KEY is not set`, your `.env` isn't loading. To prove the sidebar override works, rename `.env` to `.env.bak`, restart the app (it should warn "No API key"), then paste the key in the sidebar / website key field.

## Host on Streamlit Cloud (`xxx.streamlit.app`)

1. Push latest to GitHub.
2. Go to share.streamlit.io → New app → pick repo/branch.
3. Main file: `app.py` (or `chat_app.py` for the chat demo — only one; never `main.py`, that's the FastAPI backend).
4. Paste your key in the sidebar `GROQ_API_KEY` field at runtime (the app reads `.env` only — Cloud Secrets `st.secrets` is no longer wired up).
=======
Open http://127.0.0.1:8000
>>>>>>> 5581d8b6b08f06243031c676fa6d1661f22801df

## How it fits together

* `agent/llm_prompt.py` — Groq or Gemini. Forces raw pandas code or exactly `I cannot determine this.`
* `agent/react_loop.py` — Reason + Act: generate → sandbox → feed traceback back, up to 3 retries.
* `sandbox/executor.py` — isolated subprocess, 10s timeout, secrets stripped from the child env.
* `app.py` / `chat_app.py` — Streamlit UIs (Cloud-ready).
* `data/messy_data_gen.py` — regenerates the rigged CSVs and `test_questions.json`.

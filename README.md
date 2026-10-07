"""Proof-Carrying Data Analyst

Question → LLM writes pandas proof code → isolated sandbox → result accepted only after a successful `pd.read_csv` of a staged CSV.
If the sandbox crashes, the traceback is fed back to the model (up to 3 retries).
Trick questions the schema cannot support are refused with `I cannot determine this.`

## Stack

* **LLM engine:** Groq. Keys stay in a local `.env` or Streamlit Secrets in the cloud — never in git.
* **Frontend & hosting:** Streamlit UI on [Render](https://render.com), deployed from GitHub (free Community plan) for an instant demo.

## Setup

```powershell
cd proof_carry_data_analyst
pip install -r requirements.txt
python data/messy_data_gen.py
```

## API keys (never commit)

API keys are read from the app environment, a local `.env`, or Streamlit Secrets.

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

## Host on Render (GitHub → Community Cloud)

Blueprint (`render.yaml`) starts `app.py` on Render’s `$PORT`.

1. Push this repo to GitHub.
2. [Render Dashboard](https://dashboard.render.com) → **New** → **Blueprint** → this repo (or **Web Service** + Python).
3. Environment → add `GROQ_API_KEY` (same value as local `.env`; never commit it).
4. Deploy. The public URL is the instant demo.

Start command if you create the service by hand:

```
streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true
```

### Optional: Streamlit Community Cloud

Same app, secrets instead of Render env vars: https://share.streamlit.io → Main file `app.py` → App settings → Secrets (see `.streamlit/secrets.toml.example`).

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

If step 3 says `GROQ_API_KEY is not set`, check that your local `.env` is loading or configure the key in the hosting provider's environment settings.

## Host on Streamlit Cloud (`xxx.streamlit.app`)

1. Push latest to GitHub.
2. Go to share.streamlit.io → New app → pick repo/branch.
3. Main file: `app.py` (or `chat_app.py` for the chat demo — only one; never `main.py`, that's the FastAPI backend).
4. Add `GROQ_API_KEY` or `GEMINI_API_KEY` to the app's Secrets / environment settings.
=======
Open http://127.0.0.1:8000
>>>>>>> 5581d8b6b08f06243031c676fa6d1661f22801df

## How it fits together

* `agent/llm_prompt.py` — Groq (default) or Gemini. Forces raw pandas code or exactly `I cannot determine this.`
* `agent/react_loop.py` — Reason + Act: generate → sandbox → feed traceback back, up to 3 retries.
* `sandbox/executor.py` — isolated subprocess, 10s timeout, secrets stripped from the child env.
* `app.py` / `chat_app.py` — Streamlit UIs (Render / Streamlit Cloud).
* `render.yaml` — Render Community Cloud Blueprint from GitHub.
* `data/messy_data_gen.py` — regenerates the rigged CSVs and `test_questions.json`.

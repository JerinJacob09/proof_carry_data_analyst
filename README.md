"""Proof-Carrying Data Analyst

Question → LLM writes pandas proof code → isolated sandbox → verified result.
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

Open http://127.0.0.1:8000

## How it fits together

* `agent/llm_prompt.py` — Groq (default) or Gemini. Forces raw pandas code or exactly `I cannot determine this.`
* `agent/react_loop.py` — Reason + Act: generate → sandbox → feed traceback back, up to 3 retries.
* `sandbox/executor.py` — isolated subprocess, 10s timeout, secrets stripped from the child env.
* `app.py` / `chat_app.py` — Streamlit UIs (Render / Streamlit Cloud).
* `render.yaml` — Render Community Cloud Blueprint from GitHub.
* `data/messy_data_gen.py` — regenerates the rigged CSVs and `test_questions.json`.

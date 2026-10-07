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

Open http://127.0.0.1:8000

## How it fits together

* `agent/llm_prompt.py` — Groq or Gemini. Forces raw pandas code or exactly `I cannot determine this.`
* `agent/react_loop.py` — Reason + Act: generate → sandbox → feed traceback back, up to 3 retries.
* `sandbox/executor.py` — isolated subprocess, 10s timeout, secrets stripped from the child env.
* `app.py` / `chat_app.py` — Streamlit UIs (Cloud-ready).
* `data/messy_data_gen.py` — regenerates the rigged CSVs and `test_questions.json`.

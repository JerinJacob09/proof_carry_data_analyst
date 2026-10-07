"""LLM interface: Groq or Gemini → raw pandas proof code or a strict refusal."""

from __future__ import annotations

import os
import re
from pathlib import Path

REFUSAL = "I cannot determine this."

# llama-3.3-70b-versatile was retired by Groq (Aug 2026).
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
MODEL = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv()
except Exception:
    try:
        for _candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
            if _candidate.is_file():
                for _line in _candidate.read_text(encoding="utf-8").splitlines():
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip('"').strip("'")
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
                break
    except Exception:
        pass


def _from_streamlit_secrets(name: str) -> str | None:
    try:
        import streamlit as st

        secrets = getattr(st, "secrets", None)
        if secrets is None:
            return None
        value = secrets.get(name)
        if value and str(value).strip():
            return str(value).strip()
    except Exception:
        return None
    return None


def _env_or_secrets(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
        secret = _from_streamlit_secrets(name)
        if secret:
            return secret
    return None


def _resolve_api_key(explicit: str | None = None, provider: str | None = None) -> str | None:
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    prov = (provider or _resolve_provider()).lower()
    if prov == "gemini":
        return _env_or_secrets("GEMINI_API_KEY", "GOOGLE_API_KEY")
    return _env_or_secrets("GROQ_API_KEY", "GROK_API_KEY")


def _resolve_provider(explicit: str | None = None) -> str:
    if explicit and str(explicit).strip():
        return str(explicit).strip().lower()
    env = _env_or_secrets("LLM_PROVIDER")
    if env:
        return env.lower()
    if _env_or_secrets("GROQ_API_KEY", "GROK_API_KEY"):
        return "groq"
    if _env_or_secrets("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        return "gemini"
    return "groq"


def _resolve_model(explicit: str | None = None, provider: str | None = None) -> str:
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    prov = (provider or _resolve_provider()).lower()
    if prov == "gemini":
        return _env_or_secrets("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    return _env_or_secrets("GROQ_MODEL") or DEFAULT_GROQ_MODEL


SYSTEM_PROMPT = """You are a Proof-Carrying Data Analyst. You are NOT a normal chatbot.

Your output will be EXECUTED and VERIFIED. The pipeline is:

User -> AI -> CODE -> SANDBOX EXECUTION -> Answer

You never answer the question directly. You write pandas code that proves the answer.

STRICT OUTPUT CONTRACT — ONLY TWO ALLOWED OUTPUTS:
1. Raw runnable Python code, OR
2. Exactly this string with nothing else:
I cannot determine this.

FORBIDDEN outputs:
- Explanations around code such as "Here's the code:"
- Markdown of any kind, including ```python fences
- Apology variants. If you cannot reliably answer, output ONLY: I cannot determine this.
- Do not mix code and refusal.

PYTHON RULES:
- pandas, numpy, json, re, and Path are already imported in the sandbox as pd, np, json, re, Path.
- CSV files live in the working directory. Load them with pd.read_csv('orders.csv') using the filenames from SCHEMA CONTEXT. Do not invent table or column names.
- Compute the answer from the data and print it with print(...). Never hard-code the final number.
- Keep code self-contained, deterministic, top-to-bottom. No input(), plots, or network.

MESSY DATA — CLEAN BEFORE YOU AGGREGATE:
The CSVs are rigged. Your code must actively handle these traps:

1) DUPLICATE ORDER IDS
- First remove exact duplicate rows with drop_duplicates().
- A repeated identifier can represent either a duplicate or conflicting records. Never choose the first/last row arbitrarily and never use drop_duplicates(subset=[id]) to resolve conflicts.
- For a count of unique entities, count distinct non-missing identifiers; conflicting attributes do not change that count.
- Before aggregating, filtering, or joining by a repeated identifier, compare the duplicate records on every column relevant to the question. If a conflict could change the answer and the data gives no authoritative resolution rule, output exactly: I cannot determine this.
- Never sum quantities/revenue over raw rows: remove exact duplicate rows first, then verify that repeated IDs have consistent values for every measure and filter used. Refuse if they do not.

2) MIXED UNITS IN ONE COLUMN
- Price (and any other measure) columns can mix ANY units in the SAME column — not just USD and EUR.
  Examples you may see: '49.99 USD', '$49.99', '€49.99', '49,99 EUR', '£12.50', 'CA$10.00',
  'A$8.20', '₹999', '¥1500', '12.00 GBP', '10.00 INR', '15.00 JPY', '20.00 CAD', '30.00 AUD',
  plus unknown ISO codes, unknown symbols, and bare '49.99' with NO unit.
- Do NOT hard-code a two-currency if/else. Inspect the column and parse EVERY distinct unit.
- PARSE amount and unit separately. Match longer tokens first so CA$/A$/NZ$/HK$/S$/US$/R$ are not treated as USD '$'.

def parse_money(val):
    s = str(val).strip()
    if not s or s.lower() in {'nan', 'none', 'null'}:
        return float('nan'), None
    symbols = [
        ('CA$', 'CAD'), ('A$', 'AUD'), ('NZ$', 'NZD'), ('HK$', 'HKD'),
        ('S$', 'SGD'), ('US$', 'USD'), ('R$', 'BRL'),
        ('€', 'EUR'), ('£', 'GBP'), ('¥', 'JPY'), ('₹', 'INR'), ('₩', 'KRW'),
        ('$', 'USD'),
    ]
    unit = None
    rest = s
    upper = s.upper()
    for sym, code in symbols:
        if sym in s or sym.upper() in upper:
            unit = code
            rest = s.replace(sym, '').replace(sym.upper(), '').replace(sym.lower(), '')
            break
    iso = re.search(r'(?<![A-Z])([A-Z]{3})(?![A-Z])', rest.upper())
    if iso:
        unit = iso.group(1)
        rest = re.sub(iso.group(1), '', rest, count=1, flags=re.I)
    if unit is None:
        letters = re.findall(r'[A-Za-z]+', rest)
        if letters:
            unit = letters[-1].upper()  # unknown unit still counts as a unit
            rest = re.sub(re.escape(letters[-1]), '', rest, count=1, flags=re.I)
    num = rest.replace(' ', '')
    if num.count(',') == 1 and num.count('.') == 0:
        num = num.replace(',', '.')
    else:
        num = num.replace(',', '')
    num = re.sub(r'[^0-9.\\-]', '', num)
    try:
        amount = float(num) if num not in {'', '.', '-', '-.'} else float('nan')
    except Exception:
        amount = float('nan')
    return amount, unit

- After parsing, list the distinct units actually present (including unknown codes). Do not assume {USD, EUR}.
- You MAY total rows whose parsed unit matches the unit the question asked for (after mapping symbols → ISO).
- NEVER invent an exchange rate. NEVER add amounts across different units.
- If the question needs one combined number across mixed units (e.g. "total revenue" / "who spent the most" with no FX table), output: I cannot determine this.
- Bare numbers with no unit marker cannot be assumed to be USD or any other unit — treat the unit as missing.

3) MISSING / MESSY DATES
- Dates appear as ISO, dd/mm/YYYY, mm/dd/YYYY, 'Apr 07, 2025', and blanks.
- Slash dates with both parts <= 12 are AMBIGUOUS. Do not guess.
- For questions that need a specific month/day: if any relevant date is blank or ambiguous, output: I cannot determine this.
- For questions that do not depend on dates, ignore the date column.
- Missing is not zero. Never fillna(0) on money or dates.

4) OTHER TRAPS
- Join on IDs, never names. Inspect uniqueness before merges. Orphan user_id / product_id values exist.
- Country spellings vary (USA, US, u.s.a., United States) — normalize when counting by country.
- data_notes.md (if present) contradicts the CSVs. Trust the CSVs.
- Stock may be blank, zero, or text. If blanks make an exact "out of stock" count unknowable, refuse.

TRICK / UNANSWERABLE QUESTIONS — STRICT REFUSAL:
- If the question needs a column/attribute that does not exist (e.g. "How many blue shirts did we sell?" when there is no color column), output exactly: I cannot determine this.
- If the entity is not in the data (CEO email, a year with no rows, a product line that is absent), refuse.
- Do not hallucinate columns, colors, reasons, or future data.
- Do not use outside knowledge.

ASSERTIONS:
- Encode assumptions as asserts when they must hold, e.g. after parsing units:
  units = set(df['unit'].dropna())
  # NEVER assert units <= {USD, EUR} — other units may exist. Inspect units first.
- If combining across more than one unit with no FX table, refuse instead of crashing.

RETRIES:
- If PREVIOUS ATTEMPT / ERROR HISTORY is supplied, the prior code crashed. Fix the traceback (KeyError = wrong column — use SCHEMA CONTEXT names) while keeping every rule above.

Recap: raw pandas code ending in print(...), or exactly: I cannot determine this."""


def _strip_markdown(text: str) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    fence_pattern = re.compile(r"```(?:python|py)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)
    blocks = fence_pattern.findall(cleaned)
    if blocks:
        cleaned = "\n".join(block.strip() for block in blocks if block.strip())
    cleaned = re.sub(r"^```(?:python|py)?\s*", "", cleaned, flags=re.IGNORECASE | re.MULTILINE)
    cleaned = cleaned.replace("```", "")
    return cleaned.strip()


def _normalize_error_history(error_history) -> str:
    if error_history is None:
        return ""
    if isinstance(error_history, list):
        parts = [str(e).strip() for e in error_history if str(e).strip()]
        return "\n".join(parts).strip()
    return str(error_history).strip()


def _normalize_output(cleaned: str) -> str:
    cleaned = (cleaned or "").strip()
    if cleaned == REFUSAL:
        return REFUSAL
    if REFUSAL in cleaned:
        code_markers = ("import ", "print(", "assert ", "pd.", "DataFrame", "read_csv", "=")
        if not any(m in cleaned for m in code_markers):
            return REFUSAL
        for line in cleaned.splitlines():
            if line.strip() == REFUSAL:
                return REFUSAL
        if len(cleaned) < len(REFUSAL) + 100:
            return REFUSAL
    return cleaned


def _call_groq(api_key: str, model: str, user_prompt: str) -> str:
    from groq import Groq

    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
    )
    return response.choices[0].message.content or ""


def _call_gemini(api_key: str, model: str, user_prompt: str) -> str:
    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError(
            "Gemini selected but google-genai is not installed. pip install google-genai"
        ) from exc

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=user_prompt,
        config={
            "system_instruction": SYSTEM_PROMPT,
            "temperature": 0,
        },
    )
    return getattr(response, "text", None) or ""


def generate_code(
    user_question: str,
    schema_context: str = "",
    error_history=None,
    api_key: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> str:
    """Generate proof-carrying pandas code (or canonical refusal)."""
    resolved_provider = _resolve_provider(provider)
    resolved_key = _resolve_api_key(api_key, resolved_provider)
    if resolved_key and not provider:
        if resolved_key.startswith("gsk_"):
            resolved_provider = "groq"
        elif resolved_key.startswith("AIza"):
            resolved_provider = "gemini"
    if not resolved_key:
        raise RuntimeError(
            "No LLM API key found. Set GROQ_API_KEY or GEMINI_API_KEY in a local .env, "
            "Streamlit Cloud Secrets, the sidebar, or an environment variable. "
            "Never commit the real key."
        )
    resolved_model = _resolve_model(model, resolved_provider)

    errors = _normalize_error_history(error_history)
    user_prompt = (
        f"USER QUESTION:\n{user_question.strip()}\n\n"
        f"SCHEMA CONTEXT:\n{(schema_context or '').strip()}"
    )
    if errors:
        user_prompt += (
            "\n\nPREVIOUS ATTEMPT(S) FAILED — ERROR HISTORY:\n"
            f"{errors}\n"
            "Fix the specific error above. Use only files/columns from SCHEMA CONTEXT. "
            "Keep the strict output contract."
        )
    else:
        user_prompt += "\n\nNo previous errors. Generate the proof code on the first attempt."

    if resolved_provider == "gemini":
        raw = _call_gemini(resolved_key, resolved_model, user_prompt)
    else:
        raw = _call_groq(resolved_key, resolved_model, user_prompt)

    return _normalize_output(_strip_markdown(raw))

"""llm_prompt.py — Person 3's LLM brain/interface.

Controlled bridge between the user's analytical question and the Groq model,
forcing the model to produce reproducible pandas proof code or explicitly
admit the data is insufficient.

Flow:
    user_question + schema_context + error_history
        -> System Prompt
        -> Groq API
        -> LLM response
        -> clean / validate
        -> runnable Python code  OR  "I cannot determine this."
"""

import os
import re
from pathlib import Path

from groq import Groq

# --- Key loading: .env file only (plus explicit arg / env var) ---
# .env is gitignored and holds GROQ_API_KEY locally. Never commit it.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    # Fallback: minimal manual .env parse if python-dotenv isn't installed.
    # Looks for .env in CWD and project root (parent of agent/).
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


def _resolve_api_key(explicit: str | None = None) -> str | None:
    """Resolve GROQ_API_KEY from explicit arg -> env var / .env file."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    key = os.getenv("GROQ_API_KEY")
    if key and key.strip():
        return key.strip()
    return None


def _resolve_model(explicit: str | None = None) -> str:
    """Resolve model from explicit arg -> env var / .env file -> default."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    env_model = os.getenv("GROQ_MODEL")
    if env_model and env_model.strip():
        return env_model.strip()
    return "openai/gpt-oss-120b"

# Exact canonical refusal. No variants allowed downstream.
REFUSAL = "I cannot determine this."

# Default model (import-time snapshot for backwards compat).
# generate_code() re-resolves at call time via _resolve_model() so
# .env / env vars work without code edits.
# NOTE: llama-3.3-70b-versatile was retired by Groq (Aug 2026, enterprise-only);
# the free-tier replacement is openai/gpt-oss-120b.
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


SYSTEM_PROMPT = """You are a Proof-Carrying Data Analyst. You are NOT a normal chatbot.

Your output will be EXECUTED and VERIFIED. The pipeline is:

User -> AI -> CODE -> EXECUTION -> VERIFICATION -> Answer

You never answer the question directly. You write code that proves the answer.

STRICT OUTPUT CONTRACT — ONLY TWO ALLOWED OUTPUTS:
1. Raw runnable Python code, OR
2. Exactly this string with nothing else:
I cannot determine this.

FORBIDDEN outputs:
- Explanations around code such as "Here's the code:" or "Sure! Here's the Pandas solution..."
- Markdown formatting of any kind, including ```python fences, ``` fences, backticks, headings.
- Apology variants such as "Sorry, I don't have enough information." or "Unable to determine from the provided data."
- If you cannot reliably answer, output ONLY: I cannot determine this.
- Do not mix code and refusal. It is one or the other.

PYTHON RULES:
- Use pandas for data manipulation and analysis.
- Assume DataFrames are already loaded with the variable/table/column names given in SCHEMA CONTEXT. Do not invent table or column names. Do not use read_csv / read_excel unless a file path is explicitly supplied in the schema context.
- The code must compute the answer and print the final result with print(...), e.g. print(result). The printed value is what gets verified.
- Never hard-code the final numerical answer. FORBIDDEN: result = 527.3 then print(result). REQUIRED: result = orders["amount"].mean() then print(result). The calculation must come from the data.
- Keep code self-contained, deterministic, and runnable top-to-bottom. No user input, no plotting, no network calls.

MESSY DATA — INVESTIGATE FIRST, NEVER BLINDLY CALCULATE:
Watch for: duplicate rows, missing values, inconsistent formatting, contradictory tables, ambiguous dates, different units, different currencies, duplicate identifiers, conflicting records.
Do not blindly calculate first and investigate later. Validate before aggregating.

DUPLICATE HANDLING:
- Do NOT automatically drop duplicates with drop_duplicates() unless the schema proves they are erroneous and deduplication is unambiguous.
- If duplicate rows / duplicate identifiers could change the answer and there is no way to know which record is correct, output: I cannot determine this.

MISSING DATA:
- Missing is NOT zero. NEVER do df = df.fillna(0) or fillna(0) on amounts/revenues before aggregating unless the schema explicitly defines missing as zero.
- Determine whether missing values can legitimately be ignored. Use explicit checks such as assert not df["amount"].isna().any(). If missing values affect the requested calculation and cannot be resolved from the schema, output: I cannot determine this.

JOIN PROTECTION (multiple tables):
- Prefer joining on IDs, never on names alone.
- Inspect join keys first: check dtypes, uniqueness, nulls, and expected cardinality (one-to-one, one-to-many).
- Avoid accidental many-to-many joins that silently multiply rows (e.g. 100 orders becoming 300 rows). Validate row counts before/after joins.
- If a join key is not unique when it should be, or the correct join cannot be determined, output: I cannot determine this.

CURRENCY CHECKS:
- Before any sum/mean/comparison of money, explicitly validate currencies, e.g. assert df["currency"].nunique() == 1.
- If multiple currencies appear with no conversion-rate column/table supplied, output: I cannot determine this.
- NEVER invent exchange rates. Only convert using rates explicitly supplied in the data/schema.

UNIT CHECKS:
- Same rule for units (kg vs g, m vs cm, etc.). Validate the unit column, convert only with supplied information, otherwise output: I cannot determine this.

DATE AMBIGUITY:
- DO NOT GUESS date formats. 01/02/2025 could be 1 Feb or Jan 2. Use ONLY the date format specified by the data/schema.
- Be explicit about date-range boundaries (inclusive/exclusive, timezone if given).
- If date ambiguity could change the answer, output: I cannot determine this.

CONTRADICTORY TABLES:
- If two tables/sources state different values for the same fact and there is no source-of-truth rule in the schema, do not randomly pick one.
- If the contradiction affects the requested calculation, output: I cannot determine this.

TRICK / UNANSWERABLE QUESTIONS:
- Look for questions that sound answerable but are not: profit in 2027 when data ends in 2026, "why did sales decrease" when only sales figures exist (no causation data), revenue of a company not present in the data, columns/tables that do not exist.
- Do not guess, extrapolate, or use outside knowledge. If the schema does not contain what the question needs, output: I cannot determine this.

ASSERTIONS — VALIDATION INSIDE THE PROOF:
- Encode every assumption as an executable assertion so the verifier can catch violations, e.g.:
  assert df["currency"].nunique() == 1, "Currency mismatch"
  assert df["order_id"].is_unique, "Duplicate order_id"
  assert not df["amount"].isna().any(), "Missing amounts"
- If an assertion could fail given the described messiness, prefer refusing over silently proceeding.

RETRIES:
- If PREVIOUS ATTEMPT / ERROR HISTORY is supplied, the prior code failed. Fix the specific error (e.g. KeyError means wrong column name — use only columns from SCHEMA CONTEXT) while keeping all rules above.

Recap: output raw runnable Python code with no markdown and no commentary, ending in print(...), or output exactly: I cannot determine this."""


def _strip_markdown(text: str) -> str:
    """Remove accidental Markdown code fences from model output.

    Converts:
        ```python
        <code>
        ```
    into:
        <code>

    Handles multiple fenced blocks, bare ``` blocks, and
    stray inline backticks conservatively.
    """
    if not text:
        return ""
    cleaned = text.strip()
    # Extract contents of all fenced blocks; if any exist, join them.
    # Matches ```python ... ```, ```py ... ```, ``` ... ```
    fence_pattern = re.compile(r"```(?:python|py)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)
    blocks = fence_pattern.findall(cleaned)
    if blocks:
        cleaned = "\n".join(block.strip() for block in blocks if block.strip())
    # Drop any leftover fence markers / language tags.
    cleaned = re.sub(r"^```(?:python|py)?\s*", "", cleaned, flags=re.IGNORECASE | re.MULTILINE)
    cleaned = cleaned.replace("```", "")
    return cleaned.strip()


def _normalize_error_history(error_history) -> str:
    """Normalize error_history (None | str | list) into a prompt string."""
    if error_history is None:
        return ""
    if isinstance(error_history, list):
        parts = [str(e).strip() for e in error_history if str(e).strip()]
        return "\n".join(parts).strip()
    return str(error_history).strip()


def generate_code(user_question: str, schema_context: str = "", error_history=None, api_key: str | None = None, model: str | None = None) -> str:
    """Generate proof-carrying pandas code (or canonical refusal) via Groq.

    Args:
        user_question: The user's analytical question.
        schema_context: Table/column names, dtypes, format notes, file paths.
        error_history: Prior failure(s) — string or list of strings, e.g.
            "KeyError: 'revenue'". Fed back so the model can self-correct.
        api_key: Optional explicit key (used by Streamlit sidebar input).
            If None, resolved from env var / .env file.
        model: Optional explicit model override. If None, resolved from
            env var / .env file -> default.

    Returns:
        Raw runnable Python code, or exactly "I cannot determine this.".
    """
    resolved_key = _resolve_api_key(api_key)
    if not resolved_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Set it in the .env file in project root:\n"
            "  GROQ_API_KEY=gsk_...  (see README)\n"
            "Or pass it via the sidebar / request key field, or env var.\n"
            "Get a key at https://console.groq.com/keys — never commit the real key."
        )
    resolved_model = _resolve_model(model)

    client = Groq(api_key=resolved_key)

    errors = _normalize_error_history(error_history)

    user_prompt = f"USER QUESTION:\n{user_question.strip()}\n\nSCHEMA CONTEXT:\n{(schema_context or '').strip()}"
    if errors:
        user_prompt += (
            "\n\nPREVIOUS ATTEMPT(S) FAILED — ERROR HISTORY:\n"
            f"{errors}\n"
            "Fix the specific error above. Use only tables/columns from SCHEMA CONTEXT. "
            "Keep the strict output contract."
        )
    else:
        user_prompt += "\n\nNo previous errors. Generate the proof code on the first attempt."

    response = client.chat.completions.create(
        model=resolved_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
    )

    raw = response.choices[0].message.content or ""
    cleaned = _strip_markdown(raw).strip()

    # Defensive refusal handling: single canonical refusal downstream.
    # The model must not mix code and refusal, so any refusal phrase
    # without real code collapses to exactly REFUSAL.
    if cleaned == REFUSAL:
        return REFUSAL
    if REFUSAL in cleaned:
        # If it looks like prose + refusal rather than executable code,
        # normalize to the canonical refusal.
        code_markers = ("import ", "print(", "assert ", "pd.", "DataFrame", "result", "=")
        if not any(m in cleaned for m in code_markers):
            return REFUSAL
        # Mixed refusal + code is ambiguous: refuse rather than guess.
        # But if the model echoed the refusal string inside code (e.g. in a
        # comment/string), still treat an explicit refusal line as refusal.
        for line in cleaned.splitlines():
            if line.strip() == REFUSAL:
                return REFUSAL
        if len(cleaned) < len(REFUSAL) + 100:
            return REFUSAL

    return cleaned

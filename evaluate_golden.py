"""Run the live agent against every checked-in golden question.

Usage: python evaluate_golden.py
Requires GROQ_API_KEY or GEMINI_API_KEY from the environment, .env, or Streamlit Secrets.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal, InvalidOperation

from agent.llm_prompt import REFUSAL, _resolve_api_key
from agent.react_loop import run_react
from agent.schema import DATA_DIR, build_schema_context


def matches_expected(answer: str, refused: bool, expected) -> bool:
    if expected == REFUSAL:
        return refused and answer.strip() == REFUSAL
    if refused:
        return False
    try:
        return Decimal(answer.strip()) == Decimal(str(expected))
    except (InvalidOperation, ValueError):
        return False


def main() -> int:
    if not _resolve_api_key():
        print("Set GROQ_API_KEY or GEMINI_API_KEY before running the live evaluation.", file=sys.stderr)
        return 2

    cases = json.loads((DATA_DIR / "test_questions.json").read_text(encoding="utf-8"))
    schema = build_schema_context(DATA_DIR)
    passed = 0
    for index, case in enumerate(cases, start=1):
        result = run_react(case["q"], schema, str(DATA_DIR))
        ok = result.ok and matches_expected(result.answer, result.refused, case["expected"])
        passed += int(ok)
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {index}/{len(cases)} {case['q']}")
        if not ok:
            print(f"  expected: {case['expected']}")
            print(f"  got:      {result.answer}")
            if result.error:
                print(f"  error:    {result.error}")
    print(f"Golden evaluation: {passed}/{len(cases)} passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())

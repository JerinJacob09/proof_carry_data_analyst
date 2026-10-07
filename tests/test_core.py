"""Sandbox, schema, ReAct loop, and messy-data traps — no live LLM required."""

from pathlib import Path

import pandas as pd

from agent.llm_prompt import REFUSAL, SYSTEM_PROMPT, _normalize_output, _strip_markdown
from agent.react_loop import MAX_RETRIES, run_react
from agent.schema import DATA_DIR, build_schema_context
from sandbox.executor import ExecutionSandbox

ROOT = Path(__file__).resolve().parent.parent


def test_max_retries_is_three():
    assert MAX_RETRIES == 3


def test_strip_markdown_fences():
    raw = "```python\nprint(1)\n```"
    assert _strip_markdown(raw) == "print(1)"


def test_normalize_refusal():
    assert _normalize_output(REFUSAL) == REFUSAL
    assert _normalize_output("Sorry. I cannot determine this.") == REFUSAL


def test_sandbox_runs_pandas_against_csv(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,qty\n1,2\n1,2\n3,4\n", encoding="utf-8")
    code = (
        "orders = pd.read_csv('orders.csv')\n"
        "orders = orders.drop_duplicates(subset=['order_id'])\n"
        "print(int(orders['qty'].sum()))\n"
    )
    res = ExecutionSandbox().run(code, working_dir=str(tmp_path))
    assert res.success, res.stderr
    assert res.stdout.strip() == "6"
    assert res.verified
    assert res.csv_files_read == ("orders.csv",)


def test_sandbox_does_not_verify_hard_coded_output(tmp_path):
    res = ExecutionSandbox().run("print(100)", working_dir=str(tmp_path))
    assert res.success  # execution succeeded
    assert not res.verified
    assert res.error_type == "VerificationError"


def test_sandbox_reads_csv_with_python_string_storage(tmp_path):
    (tmp_path / "strings.csv").write_text("label\nhello\n", encoding="utf-8")
    res = ExecutionSandbox().run(
        "df = pd.read_csv('strings.csv')\nprint(pd.options.mode.string_storage)",
        working_dir=str(tmp_path),
    )
    assert res.success, res.stderr
    assert res.verified
    assert res.stdout.strip() == "python"


def test_sandbox_rejects_os_import(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_should_not_leak")
    res = ExecutionSandbox().run(
        "import os\nprint(os.environ.get('GROQ_API_KEY'))\n",
        working_dir=str(tmp_path),
    )
    assert not res.success
    assert "SandboxSecurityError" in res.stderr
    assert "gsk_should_not_leak" not in res.stdout


def test_sandbox_timeout():
    res = ExecutionSandbox(default_timeout=0.2).run("import time\ntime.sleep(5)")
    assert not res.success
    assert "TimeoutError" in res.stderr


def test_sandbox_captures_traceback(tmp_path):
    res = ExecutionSandbox().run("print(missing_name)", working_dir=str(tmp_path))
    assert not res.success
    assert "NameError" in res.stderr


def test_sandbox_blocks_escape_to_env_and_network(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "orders.csv").write_text("order_id\n1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("GROQ_API_KEY=gsk_leaked\n", encoding="utf-8")
    sandbox = ExecutionSandbox()

    outside = sandbox.run(
        "print(open('../.env').read())",
        working_dir=str(work),
    )
    assert not outside.success
    assert "SandboxSecurityError" in outside.stderr
    assert "gsk_leaked" not in outside.stdout

    abs_read = sandbox.run(
        f"print(open(r'{tmp_path / '.env'}').read())",
        working_dir=str(work),
    )
    assert not abs_read.success
    assert "SandboxSecurityError" in abs_read.stderr

    net = sandbox.run(
        "import socket\nsocket.create_connection(('127.0.0.1', 9), 1)\n",
        working_dir=str(work),
    )
    assert not net.success
    assert "SandboxSecurityError" in net.stderr

    proc = sandbox.run(
        "import subprocess\nsubprocess.run(['echo', 'hi'])\n",
        working_dir=str(work),
    )
    assert not proc.success
    assert "SandboxSecurityError" in proc.stderr

    write = sandbox.run(
        "Path('out.txt').write_text('nope')\n",
        working_dir=str(work),
    )
    assert not write.success
    assert "SandboxSecurityError" in write.stderr
    assert not (work / "out.txt").exists()

    csv_escape = sandbox.run(
        "print(pd.read_csv('../.env'))\n",
        working_dir=str(work),
    )
    assert not csv_escape.success
    assert "SandboxSecurityError" in csv_escape.stderr


def test_react_retries_then_succeeds(tmp_path):
    (tmp_path / "t.csv").write_text("a\n1\n", encoding="utf-8")
    calls = {"n": 0}

    def fake_generate(question, schema_context="", error_history=None, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return "raise ValueError('boom')"
        return "df = pd.read_csv('t.csv')\nprint(int(df['a'].iloc[0]) + 41)"

    result = run_react(
        "q",
        "schema",
        working_dir=str(tmp_path),
        generate=fake_generate,
        max_retries=3,
    )
    assert result.ok
    assert result.answer.strip() == "42"
    assert len(result.attempts) == 3
    assert calls["n"] == 3
    assert result.attempts[-1].csv_files_read == ("t.csv",)


def test_react_retries_hard_coded_answer_until_csv_is_read(tmp_path):
    (tmp_path / "t.csv").write_text("a\n1\n", encoding="utf-8")
    calls = {"n": 0}

    def fake_generate(question, schema_context="", error_history=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "print(100)"
        return "df = pd.read_csv('t.csv')\nprint(int(df['a'].sum()))"

    result = run_react("q", "schema", str(tmp_path), generate=fake_generate, max_retries=1)
    assert result.ok and result.answer == "1"
    assert len(result.attempts) == 2
    assert not result.attempts[0].success
    assert "VerificationError" in result.attempts[0].stderr
    assert result.attempts[1].csv_files_read == ("t.csv",)


def test_react_refuses_without_executing(tmp_path):
    def fake_generate(question, schema_context="", error_history=None, **kwargs):
        return REFUSAL

    result = run_react("How many blue shirts?", "no color column", str(tmp_path), generate=fake_generate)
    assert result.ok and result.refused
    assert result.answer == REFUSAL
    assert result.attempts == []


def test_react_gives_up_after_three_retries(tmp_path):
    def fake_generate(question, schema_context="", error_history=None, **kwargs):
        return "raise RuntimeError('still broken')"

    result = run_react("q", "s", str(tmp_path), generate=fake_generate, max_retries=3)
    assert not result.ok
    assert len(result.attempts) == 4  # initial + 3 retries


def test_messy_csvs_exist_with_traps():
    for name in ("orders.csv", "users.csv", "inventory.csv"):
        assert (DATA_DIR / name).is_file(), f"missing {name}; run python data/messy_data_gen.py"
    orders = pd.read_csv(DATA_DIR / "orders.csv")
    assert orders["order_id"].duplicated().any()
    prices = orders["price"].astype(str)
    assert prices.str.contains(r"USD|\$", regex=True).any()
    assert prices.str.contains(r"EUR|€", regex=True).any()
    blob = " ".join(prices.tolist())
    inv_blob = " ".join(pd.read_csv(DATA_DIR / "inventory.csv")["price"].astype(str).tolist())
    combined = blob + " " + inv_blob
    assert any(t in combined for t in ("GBP", "£"))
    assert any(t in combined for t in ("INR", "₹"))
    assert any(t in combined for t in ("JPY", "¥"))
    assert any(t in combined for t in ("CAD", "CA$"))
    assert any(t in combined for t in ("AUD", "A$"))
    assert orders["order_date"].isna().any() or (orders["order_date"].astype(str).str.strip() == "").any()
    assert "color" not in orders.columns


def test_prompt_does_not_assume_only_usd_eur():
    assert "Do NOT hard-code a two-currency" in SYSTEM_PROMPT
    assert "assert set(df['currency'].dropna()) <= {'USD', 'EUR'}" not in SYSTEM_PROMPT
    assert "CA$" in SYSTEM_PROMPT


def test_prompt_refuses_conflicting_duplicate_values_instead_of_choosing_first():
    assert "Never choose the first/last row arbitrarily" in SYSTEM_PROMPT
    assert "count distinct non-missing identifiers" in SYSTEM_PROMPT
    assert "If a conflict could change the answer" in SYSTEM_PROMPT
    assert "Prefer the first occurrence" not in SYSTEM_PROMPT


def _parse_unit(val: str) -> str | None:
    import re

    s = str(val).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return None
    symbols = [
        ("CA$", "CAD"),
        ("A$", "AUD"),
        ("NZ$", "NZD"),
        ("HK$", "HKD"),
        ("S$", "SGD"),
        ("US$", "USD"),
        ("R$", "BRL"),
        ("€", "EUR"),
        ("£", "GBP"),
        ("¥", "JPY"),
        ("₹", "INR"),
        ("₩", "KRW"),
        ("$", "USD"),
    ]
    unit = None
    rest = s
    upper = s.upper()
    for sym, code in symbols:
        if sym in s or sym.upper() in upper:
            unit = code
            rest = s.replace(sym, "").replace(sym.upper(), "").replace(sym.lower(), "")
            break
    iso = re.search(r"(?<![A-Z])([A-Z]{3})(?![A-Z])", rest.upper())
    if iso:
        unit = iso.group(1)
    elif unit is None:
        letters = re.findall(r"[A-Za-z]+", rest)
        if letters:
            unit = letters[-1].upper()
    return unit


def test_parser_maps_all_symbols_not_just_dollar_and_euro():
    samples = {
        "49.99 USD": "USD",
        "$49.99": "USD",
        "€49.99": "EUR",
        "49,99 EUR": "EUR",
        "£12.50": "GBP",
        "12.00 GBP": "GBP",
        "CA$10.00": "CAD",
        "20.00 CAD": "CAD",
        "A$8.20": "AUD",
        "₹999.00": "INR",
        "10.00 INR": "INR",
        "¥1500.00": "JPY",
        "15.00 JPY": "JPY",
        "49.99": None,
        "10.00 CHF": "CHF",
        "₩1000": "KRW",
    }
    for raw, expected in samples.items():
        assert _parse_unit(raw) == expected, raw


def test_orders_contain_more_than_two_price_units():
    orders = pd.read_csv(DATA_DIR / "orders.csv")
    units = {_parse_unit(v) for v in orders["price"].astype(str)}
    units.discard(None)
    assert len(units) >= 5, units
    assert {"USD", "EUR"} <= units


def test_schema_lists_filenames_and_no_color():
    schema = build_schema_context(DATA_DIR)
    assert "orders.csv" in schema
    assert "There is NO color column" in schema
    assert "color" not in pd.read_csv(DATA_DIR / "orders.csv").columns


def test_unique_orders_after_dedupe():
    orders = pd.read_csv(DATA_DIR / "orders.csv")
    unique = orders.drop_duplicates(subset=["order_id"]).shape[0]
    assert unique == orders["order_id"].nunique()
    assert unique < len(orders)


def test_form_flag_parsing():
    from main import _form_flag

    assert _form_flag(True) is True
    assert _form_flag(False) is False
    assert _form_flag("true") is True
    assert _form_flag("True") is True
    assert _form_flag("1") is True
    assert _form_flag("on") is True
    assert _form_flag("yes") is True

    assert _form_flag("false") is False
    assert _form_flag("False") is False
    assert _form_flag("0") is False
    assert _form_flag("off") is False
    assert _form_flag("no") is False

    assert _form_flag("", default=True) is True
    assert _form_flag("", default=False) is False
    assert _form_flag(None, default=True) is True
    assert _form_flag(None, default=False) is False


def test_analyze_auto_retry_controls_max_retries():
    from unittest.mock import MagicMock, patch
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)
    with patch("main.run_react") as mock_react:
        mock_react.return_value = MagicMock(
            attempts=[], refused=False, answer="ok", code="", parsed_json=None
        )

        # Unchecked in UI sends string "false"
        res = client.post(
            "/api/analyze",
            data={"question": "count rows", "auto_retry": "false", "use_builtin": "true"},
        )
        assert res.status_code == 200
        assert mock_react.call_args.kwargs["max_retries"] == 0

        # Checked in UI sends string "true"
        res = client.post(
            "/api/analyze",
            data={"question": "count rows", "auto_retry": "true", "use_builtin": "true"},
        )
        assert res.status_code == 200
        assert mock_react.call_args.kwargs["max_retries"] == 3


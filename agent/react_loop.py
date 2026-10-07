"""ReAct loop: generate pandas proof code, execute, self-correct up to 3 retries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from agent.llm_prompt import REFUSAL, generate_code
from sandbox.executor import ExecutionSandbox, SandboxResult

# 1 initial attempt + up to 3 rewrites after sandbox failures.
MAX_RETRIES = 3


@dataclass
class Attempt:
    n: int
    code: str
    success: bool
    stdout: str
    stderr: str


@dataclass
class AgentResult:
    ok: bool
    refused: bool
    answer: str
    code: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    error: str = ""
    parsed_json: "dict | None" = None


def _format_error_history(attempts: list[Attempt]) -> str:
    parts = []
    for att in attempts:
        parts.append(
            f"--- attempt {att.n} ---\n"
            f"CODE:\n{att.code}\n\n"
            f"TRACEBACK / STDERR:\n{att.stderr or '(empty)'}\n"
            f"STDOUT:\n{att.stdout or '(empty)'}"
        )
    return "\n\n".join(parts)


def run_react(
    question: str,
    schema_context: str,
    working_dir: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    sandbox: ExecutionSandbox | None = None,
    generate: Callable[..., str] | None = None,
    max_retries: int = MAX_RETRIES,
) -> AgentResult:
    """Ask the LLM for proof code, run it, feed tracebacks back up to max_retries times."""
    sandbox = sandbox or ExecutionSandbox()
    generate_fn = generate or generate_code
    attempts: list[Attempt] = []
    last_code = ""

    total_attempts = max_retries + 1
    for n in range(1, total_attempts + 1):
        error_history = _format_error_history(attempts) if attempts else None
        try:
            code = generate_fn(
                question,
                schema_context,
                error_history=error_history,
                api_key=api_key,
                model=model,
                provider=provider,
            )
        except Exception as exc:  # noqa: BLE001
            return AgentResult(
                ok=False,
                refused=False,
                answer=f"LLM call failed: {exc}",
                code=last_code,
                attempts=attempts,
                error=str(exc),
            )

        last_code = (code or "").strip()
        if last_code == REFUSAL or last_code.strip() == REFUSAL:
            return AgentResult(
                ok=True,
                refused=True,
                answer=REFUSAL,
                code=REFUSAL,
                attempts=attempts,
            )

        res: SandboxResult = sandbox.run(last_code, working_dir=working_dir)
        att = Attempt(
            n=n,
            code=last_code,
            success=res.success,
            stdout=res.stdout,
            stderr=res.stderr,
        )
        attempts.append(att)

        if res.success:
            printed = (res.stdout or "").strip()
            return AgentResult(
                ok=True,
                refused=False,
                answer=printed or "(code ran but printed nothing)",
                code=last_code,
                attempts=attempts,
                parsed_json=res.parsed_json,
            )

    last_err = attempts[-1].stderr if attempts else "unknown error"
    return AgentResult(
        ok=False,
        refused=False,
        answer=(
            f"I couldn't produce working code after {max_retries} retries. "
            f"Last error:\n{last_err}"
        ),
        code=last_code,
        attempts=attempts,
        error=last_err,
    )

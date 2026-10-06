from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Optional


@dataclass
class SandboxResult:
    success: bool
    stdout: str
    stderr: str
    parsed_json: Optional[Dict[str, Any]] = None
    error_type: Optional[str] = None
    execution_time_seconds: Optional[float] = None


class ExecutionSandbox:
    """Safely executes dynamically generated Python scripts in an isolated subprocess,

    enforcing execution limits, capturing outputs, and extracting structured
    proofs.
    """

    def __init__(self, default_timeout: float = 10.0):
        self.default_timeout = default_timeout

    def run(
        self,
        code_string: str,
        timeout: Optional[float] = None,
        working_dir: Optional[str] = None,
    ) -> SandboxResult:
        """Executes code string in a temporary standalone process."""
        effective_timeout = timeout or self.default_timeout
        sanitized_code = self._clean_code(code_string)

        with tempfile.TemporaryDirectory() as temp_dir:
            exec_path = Path(working_dir) if working_dir else Path(temp_dir)
            script_file = Path(temp_dir) / "proof_runner.py"
            script_file.write_text(sanitized_code, encoding="utf-8")

            # Environment flags: disable buffering and bytecode writing
            child_env = {
                **os.environ,
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
            }

            start_time = time.perf_counter()

            try:
                proc = subprocess.run(
                    [sys.executable, str(script_file)],
                    cwd=str(exec_path),
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                    env=child_env,
                )
                elapsed = round(time.perf_counter() - start_time, 4)

                stdout = proc.stdout.strip()
                stderr = proc.stderr.strip()

                if proc.returncode != 0:
                    return SandboxResult(
                        success=False,
                        stdout=stdout,
                        stderr=stderr,
                        error_type="RuntimeExecutionError",
                        execution_time_seconds=elapsed,
                    )

                parsed_json = self._extract_proof_json(stdout)

                return SandboxResult(
                    success=True,
                    stdout=stdout,
                    stderr=stderr,
                    parsed_json=parsed_json,
                    execution_time_seconds=elapsed,
                )

            except subprocess.TimeoutExpired:
                elapsed = round(time.perf_counter() - start_time, 4)
                return SandboxResult(
                    success=False,
                    stdout="",
                    stderr=f"TimeoutError: Execution exceeded the {effective_timeout}s budget.",
                    error_type="TimeoutError",
                    execution_time_seconds=elapsed,
                )
            except Exception as exc:
                elapsed = round(time.perf_counter() - start_time, 4)
                return SandboxResult(
                    success=False,
                    stdout="",
                    stderr=f"SandboxInfrastructureError: {str(exc)}",
                    error_type="InfrastructureError",
                    execution_time_seconds=elapsed,
                )

    @staticmethod
    def _clean_code(raw_code: str) -> str:
        """Strips markdown code blocks, backticks, and surrounding whitespace."""
        code = raw_code.strip()
        # Remove ```python and ``` block wrappers
        code = re.sub(r"^```(?:python)?\s*", "", code, flags=re.IGNORECASE)
        code = re.sub(r"\s*```$", "", code)
        return code.strip()

    @staticmethod
    def _extract_proof_json(stdout: str) -> Optional[Dict[str, Any]]:
        """Scans backward for the last valid JSON dictionary emitted in stdout."""
        if not stdout:
            return None

        # Try parsing line-by-line in reverse order first (most common case: print(json.dumps(...)))
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        for line in reversed(lines):
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue

        # Fallback: Find matching brace block anywhere in the output
        matches = list(re.finditer(r"(\{.*\})", stdout, re.DOTALL))
        for match in reversed(matches):
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue

        return None
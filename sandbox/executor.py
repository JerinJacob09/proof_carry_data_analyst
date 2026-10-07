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


_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY")

BOOTSTRAP = """\
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

"""


@dataclass
class SandboxResult:
    success: bool
    stdout: str
    stderr: str
    parsed_json: Optional[Dict[str, Any]] = None
    error_type: Optional[str] = None
    execution_time_seconds: Optional[float] = None


class ExecutionSandbox:
    """Run generated Python in an isolated subprocess with a timeout."""

    def __init__(self, default_timeout: float = 10.0):
        self.default_timeout = default_timeout

    def run(
        self,
        code_string: str,
        timeout: Optional[float] = None,
        working_dir: Optional[str] = None,
    ) -> SandboxResult:
        effective_timeout = timeout or self.default_timeout
        sanitized_code = self._clean_code(code_string)
        script_body = BOOTSTRAP + sanitized_code

        with tempfile.TemporaryDirectory() as temp_dir:
            exec_path = Path(working_dir) if working_dir else Path(temp_dir)
            script_file = Path(temp_dir) / "proof_runner.py"
            script_file.write_text(script_body, encoding="utf-8")

            start_time = time.perf_counter()
            try:
                proc = subprocess.run(
                    [sys.executable, str(script_file)],
                    cwd=str(exec_path),
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                    env=self._child_env(),
                )
                elapsed = round(time.perf_counter() - start_time, 4)
                stdout = (proc.stdout or "").strip()
                stderr = (proc.stderr or "").strip()

                if proc.returncode != 0:
                    return SandboxResult(
                        success=False,
                        stdout=stdout,
                        stderr=stderr or f"Process exited with code {proc.returncode}",
                        error_type="RuntimeExecutionError",
                        execution_time_seconds=elapsed,
                    )

                return SandboxResult(
                    success=True,
                    stdout=stdout,
                    stderr=stderr,
                    parsed_json=self._extract_proof_json(stdout),
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
                    stderr=f"SandboxInfrastructureError: {exc}",
                    error_type="InfrastructureError",
                    execution_time_seconds=elapsed,
                )

    @staticmethod
    def _child_env() -> dict[str, str]:
        """Inherit a usable PATH but strip secrets so generated code cannot leak keys."""
        env: dict[str, str] = {}
        for key, value in os.environ.items():
            upper = key.upper()
            if any(marker in upper for marker in _SECRET_MARKERS):
                continue
            env[key] = value
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    @staticmethod
    def _clean_code(raw_code: str) -> str:
        code = (raw_code or "").strip()
        code = re.sub(r"^```(?:python|py)?\s*", "", code, flags=re.IGNORECASE)
        code = re.sub(r"\s*```$", "", code)
        return code.strip()

    @staticmethod
    def _extract_proof_json(stdout: str) -> Optional[Dict[str, Any]]:
        if not stdout:
            return None
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        for line in reversed(lines):
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        matches = list(re.finditer(r"(\{.*\})", stdout, re.DOTALL))
        for match in reversed(matches):
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
        return None

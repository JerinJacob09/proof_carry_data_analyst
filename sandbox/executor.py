from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
    """Safely executes generated Python code in an isolated subprocess,

    enforcing timeouts, capturing stdout/stderr, and extracting the final proof
    JSON.
    """

    def __init__(self, default_timeout: float = 8.0):
        self.default_timeout = default_timeout

    def run(
        self,
        code_string: str,
        timeout: Optional[float] = None,
        working_dir: Optional[str] = None,
    ) -> SandboxResult:
        timeout = timeout or self.default_timeout

        # Clean code fences if the LLM output raw markdown
        cleaned_code = self._clean_code(code_string)

        # Execute inside a temporary file or specific directory
        with tempfile.TemporaryDirectory() as temp_dir:
            exec_dir = working_dir if working_dir else temp_dir
            script_path = Path(temp_dir) / "generated_proof.py"
            script_path.write_text(cleaned_code, encoding="utf-8")

            cmd = [sys.executable, str(script_path)]

            try:
                proc = subprocess.run(
                    cmd,
                    cwd=exec_dir,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env={
                        **os.environ,
                        "PYTHONIOENCODING": "utf-8",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                )

                stdout = proc.stdout.strip()
                stderr = proc.stderr.strip()

                # Execution failed (SyntaxError, Uncaught Exception, Assertion Failure)
                if proc.returncode != 0:
                    return SandboxResult(
                        success=False,
                        stdout=stdout,
                        stderr=stderr,
                        error_type="RuntimeError",
                    )

                # Try parsing the final proof JSON emitted by the code
                parsed_json = self._extract_proof_json(stdout)

                return SandboxResult(
                    success=True,
                    stdout=stdout,
                    stderr=stderr,
                    parsed_json=parsed_json,
                )

            except subprocess.TimeoutExpired:
                return SandboxResult(
                    success=False,
                    stdout="",
                    stderr=f"TimeoutError: Execution exceeded {timeout} seconds limit (possible infinite loop).",
                    error_type="TimeoutError",
                )
            except Exception as exc:
                return SandboxResult(
                    success=False,
                    stdout="",
                    stderr=f"SandboxError: {str(exc)}",
                    error_type="SandboxError",
                )

    @staticmethod
    def _clean_code(raw_code: str) -> str:
        """Strips markdown python backticks if present."""
        code = raw_code.strip()
        if code.startswith("```"):
            lines = code.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            code = "\n".join(lines)
        return code

    @staticmethod
    def _extract_proof_json(stdout: str) -> Optional[Dict[str, Any]]:
        """Finds the last valid JSON dictionary emitted in stdout."""
        if not stdout:
            return None

        # Look backwards for the terminal JSON block
        start_idx = stdout.rfind("{")
        end_idx = stdout.rfind("}")

        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            candidate = stdout[start_idx : end_idx + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        return None
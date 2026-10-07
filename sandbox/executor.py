"""Execution sandbox for LLM-generated pandas code.

Security model
--------------
The generated code is untrusted. The boundary is enforced by the *kernel* in a
dedicated child process (see ``_runner.py``), not by Python-level checks:

* private working directory holding only a copy of the CSV files
* Landlock: read-only access to that copy and the Python install, nothing else;
  TCP bind/connect and signals to outside processes denied
* seccomp-BPF: no sockets, no exec/fork, no file writes, no ptrace, no mount,
  no bpf/io_uring, limits cannot be raised
* rlimits: CPU, address space, no core dumps, no file growth
* minimal environment (allow-list), no inherited descriptors, own session
* wall-clock timeout (starts once the sandbox is sealed) and capped output

The AST checks and restricted builtins below only exist to give the model fast,
readable feedback ("import os is not allowed") so the self-correction loop can
fix itself. They are bypassable and must never be relied upon.

Strict mode (default on Linux) refuses to run generated code unless every kernel
layer is active *and* verified (the runner tries to open a socket, read
/etc/passwd and create a file after sealing). Set ``SANDBOX_STRICT=0`` to
downgrade knowingly (e.g. a host without Landlock); non-Linux hosts always run in
that degraded, development-only mode.
"""

from __future__ import annotations

import ast
import functools
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("sandbox")

_RUNNER = Path(__file__).with_name("_runner.py")
_IS_POSIX = os.name == "posix"
_IS_LINUX = sys.platform.startswith("linux")

_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY")

# User code may import these roots (and their submodules). Everything else is rejected.
ALLOWED_IMPORT_ROOTS = frozenset({
    "abc", "array", "base64", "bisect", "calendar", "collections", "copy", "csv",
    "dataclasses", "datetime", "decimal", "enum", "fractions", "functools",
    "hashlib", "heapq", "itertools", "json", "math", "numbers", "numpy", "operator",
    "pandas", "pathlib", "re", "statistics", "string", "textwrap", "time", "typing",
    "unicodedata", "warnings",
})

# Imported once before sealing so later lazy imports find them already loaded.
_PRELOAD = tuple(sorted(ALLOWED_IMPORT_ROOTS - {"numpy", "pandas"})) + (
    "_strptime", "zoneinfo", "encodings.latin_1", "encodings.cp1252",
    "encodings.utf_8_sig", "encodings.ascii",
)

_ALLOWED_DUNDER_NAMES = frozenset({"__name__", "__doc__"})

_BLOCKED_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "input", "breakpoint", "exit", "quit",
    "help", "memoryview", "globals", "locals", "vars", "getattr", "setattr", "delattr",
})

# Frame / code-object attributes are the usual way out of a restricted namespace.
_BLOCKED_ATTRS = frozenset({
    "gi_frame", "gi_code", "cr_frame", "cr_code", "ag_frame", "ag_code",
    "f_back", "f_globals", "f_locals", "f_builtins", "f_code", "tb_frame", "co_code",
})

_PATH_FUNCS = {"open", "read_csv", "read_table", "read_json", "read_excel", "read_fwf", "Path"}


class _SecurityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.errors: list[str] = []

    def _err(self, node: ast.AST, msg: str) -> None:
        self.errors.append(f"line {getattr(node, 'lineno', 0)}: {msg}")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = (alias.name or "").split(".", 1)[0]
            if root not in ALLOWED_IMPORT_ROOTS:
                self._err(node, f"import of '{alias.name}' is not allowed")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self._err(node, "relative imports are not allowed")
        root = (node.module or "").split(".", 1)[0]
        if root not in ALLOWED_IMPORT_ROOTS:
            self._err(node, f"import from '{node.module}' is not allowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("__") and node.id.endswith("__") and node.id not in _ALLOWED_DUNDER_NAMES:
            self._err(node, f"use of '{node.id}' is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__") and node.attr.endswith("__"):
            self._err(node, f"attribute '{node.attr}' is not allowed")
        elif node.attr in _BLOCKED_ATTRS:
            self._err(node, f"attribute '{node.attr}' is not allowed")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in _BLOCKED_CALLS:
            self._err(node, f"call to '{name}' is not allowed")
        self._check_path_args(node, name)
        self.generic_visit(node)

    def _check_path_args(self, node: ast.Call, name: str) -> None:
        if name not in _PATH_FUNCS or not node.args:
            return
        arg0 = node.args[0]
        if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str) and _unsafe_path_literal(arg0.value):
            self._err(node, f"path '{arg0.value}' is outside the sandbox working directory")


def _unsafe_path_literal(value: str) -> bool:
    if not value or value.strip() != value:
        return True
    path = Path(value)
    return path.is_absolute() or any(part == ".." for part in path.parts)


def validate_user_code(code: str) -> str | None:
    """Fast feedback for the model. NOT a security boundary. Returns an error or None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"SandboxSecurityError: generated code is not valid Python ({exc})"
    visitor = _SecurityVisitor()
    visitor.visit(tree)
    if visitor.errors:
        return f"SandboxSecurityError: {'; '.join(visitor.errors[:8])}"
    return None


@dataclass
class SandboxResult:
    success: bool
    stdout: str
    stderr: str
    parsed_json: Optional[Dict[str, Any]] = None
    error_type: Optional[str] = None
    execution_time_seconds: Optional[float] = None
    # Kernel/OS layers that were active for this run, e.g. ("rlimits", "landlock", "seccomp").
    isolation: Tuple[str, ...] = field(default_factory=tuple)


@dataclass
class _Outcome:
    returncode: Optional[int]
    stdout: str
    stderr: str
    events: list
    timed_out: bool
    output_overflow: bool
    elapsed: float


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


_parent_hardened = False


def harden_parent_process() -> None:
    """Make the app process itself harder to read from a same-uid child.

    PR_SET_DUMPABLE=0 turns /proc/<pid>/{environ,mem,...} root-owned, so even a
    sandbox escape cannot read the API key out of the Streamlit/uvicorn process.
    """
    global _parent_hardened
    if _parent_hardened or not _IS_LINUX or not _env_flag("SANDBOX_HARDEN_PARENT", True):
        return
    try:
        import ctypes

        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE
    except Exception:  # noqa: BLE001
        log.warning("could not set PR_SET_DUMPABLE=0 on the app process")
    _parent_hardened = True


class ExecutionSandbox:
    """Run generated Python in a kernel-confined subprocess with a timeout."""

    def __init__(
        self,
        default_timeout: float = 10.0,
        *,
        strict: Optional[bool] = None,
        memory_limit_mb: int = 1024,
        max_output_bytes: int = 256_000,
        startup_timeout: float = 30.0,
        data_globs: Tuple[str, ...] = ("*.csv", "data_notes.md"),
        max_data_bytes: int = 100 * 1024 * 1024,
        _python_guards: bool = True,
    ):
        self.default_timeout = default_timeout
        self.strict = _env_flag("SANDBOX_STRICT", _IS_LINUX) if strict is None else strict
        self.memory_limit_mb = memory_limit_mb
        self.max_output_bytes = max_output_bytes
        self.startup_timeout = startup_timeout
        self.data_globs = data_globs
        self.max_data_bytes = max_data_bytes
        # Test-only: switch off the AST + builtins layers to prove the kernel layers stand alone.
        self._python_guards = _python_guards

    # ------------------------------------------------------------------ public

    def run(
        self,
        code_string: str,
        timeout: Optional[float] = None,
        working_dir: Optional[str] = None,
    ) -> SandboxResult:
        effective_timeout = timeout or self.default_timeout
        code = self._clean_code(code_string)
        if self._python_guards:
            security_error = validate_user_code(code)
            if security_error:
                return SandboxResult(False, "", security_error, error_type="SandboxSecurityError")

        harden_parent_process()
        private_dir = tempfile.mkdtemp(prefix="pcda_")
        try:
            try:
                self._stage_data(working_dir, private_dir)
            except Exception as exc:  # noqa: BLE001
                return SandboxResult(
                    False, "", f"SandboxInfrastructureError: could not stage data: {exc}",
                    error_type="InfrastructureError",
                )
            cfg = {
                "code": code,
                "strict": self.strict,
                "python_guards": self._python_guards,
                "allowed_roots": sorted(ALLOWED_IMPORT_ROOTS),
                "preload": list(_PRELOAD),
                "cpu_seconds": int(effective_timeout) + 2,
                "memory_mb": self.memory_limit_mb,
            }
            try:
                out = self._run_child(cfg, private_dir, effective_timeout)
            except Exception as exc:  # noqa: BLE001
                return SandboxResult(
                    False, "", f"SandboxInfrastructureError: {exc}", error_type="InfrastructureError"
                )
            return self._to_result(out, effective_timeout)
        finally:
            shutil.rmtree(private_dir, ignore_errors=True)

    # ---------------------------------------------------------------- internals

    def _stage_data(self, working_dir: Optional[str], private_dir: str) -> None:
        """Copy only the data files into the private dir; the child never sees the original."""
        if not working_dir:
            return
        src_dir = Path(working_dir)
        total = 0
        for pattern in self.data_globs:
            for src in sorted(src_dir.glob(pattern)):
                if src.is_symlink() or not src.is_file():
                    continue
                total += src.stat().st_size
                if total > self.max_data_bytes:
                    raise ValueError(f"data files exceed {self.max_data_bytes} bytes")
                shutil.copyfile(src, Path(private_dir) / src.name)

    def _run_child(self, cfg: dict, cwd: str, timeout: float) -> _Outcome:
        status_r = status_w = None
        popen_kwargs: dict[str, Any] = {}
        if _IS_POSIX:
            status_r, status_w = os.pipe()
            cfg = {**cfg, "status_fd": status_w}
            popen_kwargs.update(pass_fds=(status_w,), start_new_session=True, close_fds=True)

        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-u", str(_RUNNER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=self._child_env(),
            **popen_kwargs,
        )
        if status_w is not None:
            os.close(status_w)  # parent keeps only the read end, so EOF arrives when the child exits

        started = time.monotonic()
        state = {"ready_at": None, "overflow": False}
        events: list = []
        out_buf, err_buf = bytearray(), bytearray()

        def feed_stdin() -> None:
            try:
                proc.stdin.write(json.dumps(cfg).encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        def drain(stream, buf: bytearray) -> None:
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        return
                    if len(buf) + len(chunk) > self.max_output_bytes:
                        buf.extend(chunk[: max(0, self.max_output_bytes - len(buf))])
                        state["overflow"] = True
                        return
                    buf.extend(chunk)
            except (OSError, ValueError):
                return

        def read_status() -> None:
            if status_r is None:
                return
            pending = b""
            try:
                while True:
                    chunk = os.read(status_r, 4096)
                    if not chunk:
                        return
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        try:
                            event = json.loads(line.decode("utf-8"))
                        except ValueError:
                            continue
                        events.append(event)
                        if event.get("event") == "ready":
                            state["ready_at"] = time.monotonic()
            except OSError:
                return

        threads = [
            threading.Thread(target=feed_stdin, daemon=True),
            threading.Thread(target=drain, args=(proc.stdout, out_buf), daemon=True),
            threading.Thread(target=drain, args=(proc.stderr, err_buf), daemon=True),
            threading.Thread(target=read_status, daemon=True),
        ]
        for t in threads:
            t.start()

        timed_out = False
        try:
            while proc.poll() is None:
                now = time.monotonic()
                if state["overflow"]:
                    self._kill(proc)
                    break
                ready_at = state["ready_at"]
                if ready_at is not None:
                    deadline = ready_at + timeout
                elif status_r is not None:
                    deadline = started + self.startup_timeout
                else:  # no readiness signal (non-POSIX): budget covers interpreter start-up too
                    deadline = started + self.startup_timeout + timeout
                if now > deadline:
                    timed_out = True
                    self._kill(proc)
                    break
                time.sleep(0.01)
            proc.wait(timeout=5)
        finally:
            for t in threads:
                t.join(timeout=2)
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            if status_r is not None:
                try:
                    os.close(status_r)
                except OSError:
                    pass

        end = time.monotonic()
        return _Outcome(
            returncode=proc.returncode,
            stdout=out_buf.decode("utf-8", errors="replace").strip(),
            stderr=err_buf.decode("utf-8", errors="replace").strip(),
            events=events,
            timed_out=timed_out,
            output_overflow=state["overflow"],
            elapsed=round(end - (state["ready_at"] or started), 4),
        )

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            if _IS_POSIX:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _to_result(self, out: _Outcome, timeout: float) -> SandboxResult:
        layers: Tuple[str, ...] = ()
        for event in out.events:
            if event.get("event") in {"ready", "error", "probe"}:
                layers = tuple(event.get("applied") or ())
                if event.get("missing") and not self.strict:
                    log.warning("sandbox running with missing isolation layers: %s", event["missing"])

        if any(e.get("event") == "error" for e in out.events) or out.returncode == 70:
            missing = next((e.get("missing") for e in out.events if e.get("event") == "error"), None)
            detail = "; ".join(missing) if missing else out.stderr
            return SandboxResult(
                False, "",
                "SandboxUnavailable: refusing to run generated code because the OS isolation layers "
                f"could not be fully enforced on this host ({detail}). "
                "Set SANDBOX_STRICT=0 to run with reduced isolation (not recommended).",
                error_type="SandboxUnavailable", isolation=layers,
            )
        if out.timed_out:
            return SandboxResult(
                False, "", f"TimeoutError: Execution exceeded the {timeout}s budget.",
                error_type="TimeoutError", execution_time_seconds=out.elapsed, isolation=layers,
            )
        if out.output_overflow:
            return SandboxResult(
                False, "", f"OutputLimitError: output exceeded {self.max_output_bytes} bytes.",
                error_type="OutputLimitError", execution_time_seconds=out.elapsed, isolation=layers,
            )
        if _IS_POSIX and out.returncode == -signal.SIGXCPU:
            return SandboxResult(
                False, out.stdout, f"TimeoutError: Execution exceeded the {timeout}s CPU budget.",
                error_type="TimeoutError", execution_time_seconds=out.elapsed, isolation=layers,
            )
        if out.returncode != 0:
            stderr = out.stderr or f"Process exited with code {out.returncode}"
            error_type = "RuntimeExecutionError"
            if re.search(r"PermissionError|Operation not permitted|Permission denied|SandboxSecurityError", stderr):
                error_type = "SandboxSecurityError"
                if "SandboxSecurityError" not in stderr:
                    stderr = (
                        "SandboxSecurityError: the sandbox blocked this operation. Generated code may only read "
                        "the provided CSV files; network, subprocesses and writing files are not permitted.\n"
                        + stderr
                    )
            return SandboxResult(
                False, out.stdout, stderr, error_type=error_type,
                execution_time_seconds=out.elapsed, isolation=layers,
            )
        return SandboxResult(
            True, out.stdout, out.stderr, parsed_json=self._extract_proof_json(out.stdout),
            execution_time_seconds=out.elapsed, isolation=layers,
        )

    @staticmethod
    def _child_env() -> dict[str, str]:
        """Allow-list environment: nothing but what the interpreter needs. No secrets, no hooks."""
        keep_exact = {
            "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
            "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "LD_LIBRARY_PATH",
            "DYLD_LIBRARY_PATH", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
        }
        env: dict[str, str] = {}
        for key, value in os.environ.items():
            upper = key.upper()
            if any(marker in upper for marker in _SECRET_MARKERS):
                continue
            if upper in keep_exact or upper.startswith("LC_"):
                env[key] = value
        # One BLAS/OpenMP thread: fewer threads to seal, smaller virtual-memory footprint.
        for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            env[var] = "1"
        env["HOME"] = tempfile.gettempdir()
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
        for match in reversed(list(re.finditer(r"(\{.*\})", stdout, re.DOTALL))):
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
        return None


@functools.lru_cache(maxsize=1)
def capabilities() -> dict:
    """Which isolation layers does THIS host actually enforce? (verified, cached)

    Returns ``{"platform", "layers", "missing", "verified", "strict_ok"}``. Cheap enough
    to call at app start-up and show in the UI, so a deployment can be audited at a glance.
    """
    info: dict[str, Any] = {
        "platform": sys.platform, "layers": [], "missing": [], "verified": {}, "strict_ok": False,
    }
    if not _IS_LINUX:
        info["missing"] = ["linux-only (rlimits, landlock, seccomp unavailable)"]
        return info
    sandbox = ExecutionSandbox(strict=False)
    cwd = tempfile.mkdtemp(prefix="pcda_probe_")
    try:
        out = sandbox._run_child({"probe": True, "strict": False, "preload": []}, cwd, 20.0)
    except Exception as exc:  # noqa: BLE001
        info["missing"] = [f"probe failed: {exc}"]
        return info
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    for event in out.events:
        if event.get("event") == "probe":
            info["layers"] = event.get("applied", [])
            info["missing"] = event.get("missing", [])
            info["verified"] = event.get("verify", {})
    info["strict_ok"] = not info["missing"] and bool(info["layers"])
    return info


def describe_isolation() -> Tuple[str, str]:
    """(level, message) for the UI. level: 'ok' | 'degraded' | 'refused'."""
    caps = capabilities()
    if caps["strict_ok"]:
        return "ok", "Sandbox: " + ", ".join(caps["layers"]) + " (verified at start-up)."
    detail = "; ".join(caps["missing"]) or "unknown"
    if ExecutionSandbox().strict:
        return "refused", f"Sandbox isolation is unavailable on this host, so generated code will be refused: {detail}"
    return "degraded", f"Sandbox running with REDUCED isolation (dev mode): {detail}. Do not expose publicly."

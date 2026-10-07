"""Kernel-level isolation tests.

Every attack here runs with the Python-level guards (AST check, import allow-list,
restricted builtins) switched OFF, i.e. we assume the attacker already beat them.
The OS layers (Landlock, seccomp, rlimits, private working dir) must hold on their own.
"""

import os
import socket
import sys
import tempfile
import threading

import pytest

from sandbox.executor import ExecutionSandbox, capabilities

CAPS = capabilities()
pytestmark = pytest.mark.skipif(
    not CAPS["strict_ok"],
    reason=f"kernel isolation layers unavailable on this host: {CAPS['missing']}",
)


@pytest.fixture()
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    (d / "orders.csv").write_text("order_id,qty\n1,2\n3,4\n", encoding="utf-8")
    (d / "secret.txt").write_text("not a csv", encoding="utf-8")
    (tmp_path / ".env").write_text("GROQ_API_KEY=gsk_leaked\n", encoding="utf-8")
    return d


@pytest.fixture()
def raw():
    """Sandbox with Python-level guards off: only the kernel stands between code and host."""
    return ExecutionSandbox(strict=True, _python_guards=False)


def _denied(res):
    assert not res.success, f"attack succeeded: {res.stdout!r}"
    assert res.error_type == "SandboxSecurityError", (res.error_type, res.stderr)


def test_all_layers_active_and_reported(raw, work):
    res = raw.run("print('ok')", working_dir=str(work))
    assert res.success, res.stderr
    assert {"rlimits", "no_new_privs", "landlock", "seccomp"} <= set(res.isolation)


def test_network_is_blocked_even_for_a_listening_local_server(raw, work):
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(1.0)
    port = server.getsockname()[1]
    accepted = []

    def accept():
        try:
            conn, _ = server.accept()
            accepted.append(conn)
        except OSError:
            pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    res = raw.run(
        f"import socket\ns = socket.socket()\ns.connect(('127.0.0.1', {port}))\nprint('connected')",
        working_dir=str(work),
    )
    t.join(2)
    server.close()
    _denied(res)
    assert not accepted


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess\nprint(subprocess.run(['id'], capture_output=True).stdout)",
        # os.system() reports a failed fork via its return value instead of raising
        "import os\nrc = os.system('echo pwned > /tmp/pcda_pwned_marker')\nif rc != 0:\n    raise PermissionError('os.system could not spawn a shell')",
        "import os\nprint(os.fork())",
        "import os\nos.execv('/bin/sh', ['sh', '-c', 'echo pwned'])",
        "import os, signal\nos.kill(os.getppid(), signal.SIGKILL)",
    ],
)
def test_cannot_spawn_processes_or_signal_the_app(raw, work, code):
    res = raw.run(code, working_dir=str(work))
    _denied(res)
    assert not os.path.exists("/tmp/pcda_pwned_marker")


def test_cannot_read_dotenv_or_system_files(raw, work, tmp_path, monkeypatch):
    # Make the sandbox's private dir a child of tmp_path, so "../.env" really points at the secret.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert (tmp_path / ".env").exists()
    for target in (str(tmp_path / ".env"), "../.env", "/etc/passwd"):
        res = raw.run(f"print(open({target!r}).read())", working_dir=str(work))
        _denied(res)  # PermissionError from Landlock, not merely "file not found"
        assert "gsk_leaked" not in res.stdout + res.stderr


def test_cannot_read_parent_environment_via_proc(raw, work, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_parent_env_secret")
    code = (
        "import os\n"
        "print(open(f'/proc/{os.getppid()}/environ', 'rb').read())\n"
    )
    res = raw.run(code, working_dir=str(work))
    _denied(res)
    assert "gsk_parent_env_secret" not in res.stdout + res.stderr


def test_child_env_has_no_secrets(raw, work, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    monkeypatch.setenv("MY_SECRET_THING", "x")
    res = raw.run("import os\nprint(sorted(k for k in os.environ if 'KEY' in k or 'SECRET' in k))", working_dir=str(work))
    assert res.success, res.stderr
    assert res.stdout == "[]"


@pytest.mark.parametrize(
    "code",
    [
        "open('new.txt', 'w').write('x')",
        "open('orders.csv', 'a').write('x')",
        "open('/tmp/pcda_write_marker', 'w').write('x')",
        "import os\nos.mkdir('d')",
        "import os\nos.remove('orders.csv')",
        "import os\nos.rename('orders.csv', 'x.csv')",
        "import pathlib\npathlib.Path('t').touch()",
        "import pandas as pd\npd.Series([1]).to_csv('out.csv')",  # slipped through the old pandas patch
        "import numpy as np\nnp.save('a.npy', np.arange(3))",
        "import os\nos.symlink('/etc/passwd', 'p')",
    ],
)
def test_cannot_write_or_modify_files(raw, work, code):
    res = raw.run(code, working_dir=str(work))
    _denied(res)
    assert not os.path.exists("/tmp/pcda_write_marker")
    assert (work / "orders.csv").read_text(encoding="utf-8").count("\n") == 3  # original untouched


def test_only_csv_data_is_staged_into_the_sandbox(raw, work):
    res = raw.run("import os\nprint(sorted(os.listdir('.')))", working_dir=str(work))
    assert res.success, res.stderr
    assert res.stdout == "['orders.csv']"


def test_old_bypass_gadgets_are_gone(work):
    guarded = ExecutionSandbox(strict=True)
    for code in ("print(_os)", "builtins.open = _orig_open", "print(_orig_import)", "print(socket)"):
        res = guarded.run(code, working_dir=str(work))
        assert not res.success
        assert "NameError" in res.stderr or "SandboxSecurityError" in res.stderr


def test_limits_cannot_be_raised(work):
    sb = ExecutionSandbox(strict=True, _python_guards=False, memory_limit_mb=256)
    code = (
        "import resource\n"
        "try:\n"
        "    resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))\n"
        "except Exception as exc:\n"
        "    print('raise refused:', type(exc).__name__)\n"
        "x = bytearray(2 * 1024**3)  # still capped at the original limit\n"
        "print('limit was lifted')\n"
    )
    res = sb.run(code, working_dir=str(work))
    assert not res.success
    assert "raise refused" in res.stdout
    assert "limit was lifted" not in res.stdout
    assert "MemoryError" in res.stderr


def test_memory_bomb_is_contained(work):
    sb = ExecutionSandbox(strict=True, _python_guards=False, memory_limit_mb=256)
    res = sb.run("x = bytearray(2 * 1024**3)\nprint(len(x))", working_dir=str(work))
    assert not res.success
    assert "MemoryError" in res.stderr


def test_infinite_loop_is_killed_by_wall_clock(work):
    sb = ExecutionSandbox(strict=True, default_timeout=1.0)
    res = sb.run("while True:\n    pass", working_dir=str(work))
    assert res.error_type == "TimeoutError"


def test_output_flood_is_capped(work):
    sb = ExecutionSandbox(strict=True, max_output_bytes=10_000)
    res = sb.run("while True:\n    print('x' * 1000)", working_dir=str(work))
    assert res.error_type == "OutputLimitError"


def test_timer_starts_after_sandbox_is_sealed(work):
    # start-up (importing pandas + sealing) must not eat the user's time budget
    sb = ExecutionSandbox(strict=True, default_timeout=2.0)
    res = sb.run("print(1)", working_dir=str(work))
    assert res.success, res.stderr


def test_strict_mode_refuses_when_a_layer_is_missing(work, monkeypatch):
    import sandbox._runner as runner

    # Same child, but pretend the kernel has no Landlock: strict must refuse, not silently run.
    monkeypatch.setattr(sys.modules["sandbox.executor"], "_RUNNER", _runner_without_landlock(runner, work))
    res = ExecutionSandbox(strict=True).run("print('should not run')", working_dir=str(work))
    assert res.error_type == "SandboxUnavailable"
    assert "should not run" not in res.stdout
    relaxed = ExecutionSandbox(strict=False).run("print('degraded but running')", working_dir=str(work))
    assert relaxed.success and "landlock" not in relaxed.isolation


def _runner_without_landlock(runner, work):
    from pathlib import Path

    src = Path(runner.__file__).read_text(encoding="utf-8")
    src = src.replace(
        'abi = _sys(444, 0, 0, 1)  # landlock_create_ruleset(NULL, 0, VERSION)',
        'abi = -1  # simulated: no Landlock',
    )
    patched = Path(work).parent / "_runner_nolandlock.py"
    patched.write_text(src, encoding="utf-8")
    return patched

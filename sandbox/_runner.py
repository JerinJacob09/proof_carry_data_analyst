"""Child-process entry point for ``sandbox.executor.ExecutionSandbox``.

Started as ``python -I -B -u _runner.py``. ONE JSON object arrives on stdin
(config + generated code). The runner then

  1. pre-imports pandas/numpy and the allow-listed stdlib modules,
  2. seals itself with kernel-enforced restrictions (Linux):
       * rlimits        - CPU, address space, no core dumps, no file growth
       * no_new_privs   - nothing can regain privileges
       * capabilities   - all dropped
       * Landlock       - read-only access to the CSV copy + the Python install,
                          TCP bind/connect denied, no signals outside the sandbox
       * seccomp-BPF    - no sockets, no exec/fork, no file writes, no ptrace,
                          no mount/bpf/io_uring/...
  3. verifies the seals actually hold (tries to open a socket, read /etc/passwd,
     create a file) - in strict mode any failure aborts with exit code 70,
  4. executes the generated code in a fresh namespace.

The Python-level guards (import allow-list, restricted builtins) only exist to
give the model fast, readable feedback. They are NOT the security boundary; the
kernel layers are. This file must stay self-contained (no project imports).
"""

from __future__ import annotations

import json
import os
import sys

EXIT_HARDENING_FAILED = 70

_STATUS_FD: int | None = None
_CSV_READS: set[str] = set()


def _status(obj: dict) -> None:
    if _STATUS_FD is None:
        return
    try:
        os.write(_STATUS_FD, (json.dumps(obj) + "\n").encode("utf-8"))
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Linux hardening
# --------------------------------------------------------------------------- #

_IS_LINUX = sys.platform.startswith("linux")

# name -> (x86_64, aarch64); None = syscall does not exist on that arch.
_SYS_EPERM = {
    # --- network: no sockets of any kind -------------------------------------
    "socket": (41, 198), "socketpair": (53, 199), "connect": (42, 203),
    "bind": (49, 200), "listen": (50, 201), "accept": (43, 202),
    "accept4": (288, 242), "sendto": (44, 206), "sendmsg": (46, 211),
    "sendmmsg": (307, 269), "recvfrom": (45, 207), "recvmsg": (47, 212),
    "recvmmsg": (299, 243),
    # --- processes / signals / introspection ---------------------------------
    "execve": (59, 221), "execveat": (322, 281), "fork": (57, None),
    "vfork": (58, None), "ptrace": (101, 117), "kill": (62, 129),
    "tkill": (200, 130), "pidfd_open": (434, 434), "pidfd_send_signal": (424, 424),
    "pidfd_getfd": (438, 438), "process_vm_readv": (310, 270),
    "process_vm_writev": (311, 271), "kcmp": (312, 272),
    # --- namespaces / mounts --------------------------------------------------
    "unshare": (272, 97), "setns": (308, 268), "mount": (165, 40),
    "umount2": (166, 39), "pivot_root": (155, 41), "chroot": (161, 51),
    "move_mount": (429, 429), "open_tree": (428, 428), "fsopen": (430, 430),
    "fsconfig": (431, 431), "fsmount": (432, 432), "fspick": (433, 433),
    "mount_setattr": (442, 442),
    # --- kernel attack surface ------------------------------------------------
    "kexec_load": (246, 104), "kexec_file_load": (320, 294),
    "init_module": (175, 105), "finit_module": (313, 273),
    "delete_module": (176, 106), "bpf": (321, 280),
    "perf_event_open": (298, 241), "userfaultfd": (323, 282),
    "io_uring_setup": (425, 425), "io_uring_enter": (426, 426),
    "io_uring_register": (427, 427), "keyctl": (250, 219),
    "add_key": (248, 217), "request_key": (249, 218),
    "open_by_handle_at": (304, 265), "name_to_handle_at": (303, 264),
    "acct": (163, 89), "swapon": (167, 224), "swapoff": (168, 225),
    "reboot": (169, 142), "sethostname": (170, 161), "setdomainname": (171, 162),
    "iopl": (172, None), "ioperm": (173, None),
    # --- limits can not be raised again ---------------------------------------
    "setrlimit": (160, 164), "prlimit64": (302, 261),
    # --- file mutation (open() for writing is handled by flag check below) ----
    "creat": (85, None), "truncate": (76, 45), "ftruncate": (77, 46),
    "fallocate": (285, 47), "rename": (82, None), "renameat": (264, 38),
    "renameat2": (316, 276), "mkdir": (83, None), "mkdirat": (258, 34),
    "rmdir": (84, None), "unlink": (87, None), "unlinkat": (263, 35),
    "symlink": (88, None), "symlinkat": (266, 36), "link": (86, None),
    "linkat": (265, 37), "chmod": (90, None), "fchmod": (91, 52),
    "fchmodat": (268, 53), "fchmodat2": (452, 452), "chown": (92, None),
    "fchown": (93, 55), "lchown": (94, None), "fchownat": (260, 54),
    "utime": (132, None), "utimes": (235, None), "futimesat": (261, None),
    "utimensat": (280, 88), "mknod": (133, None), "mknodat": (259, 33),
    "setxattr": (188, 5), "lsetxattr": (189, 6), "fsetxattr": (190, 7),
    "removexattr": (197, 14), "lremovexattr": (198, 15), "fremovexattr": (199, 16),
}
# glibc falls back to clone()/openat() when it sees ENOSYS.
_SYS_ENOSYS = {"clone3": (435, 435), "openat2": (437, 437)}
_SYS_CLONE = (56, 220)  # allowed only with CLONE_THREAD (threads yes, fork no)
_SYS_OPEN = (2, None)  # flags in args[1]
_SYS_OPENAT = (257, 56)  # flags in args[2]

_ARCHES = {
    "x86_64": (0xC000003E, 0),
    "amd64": (0xC000003E, 0),
    "aarch64": (0xC00000B7, 1),
    "arm64": (0xC00000B7, 1),
}
_SYS_SECCOMP = {0: 317, 1: 277}
_SYS_CAPSET = {0: 126, 1: 91}

_CLONE_THREAD = 0x00010000
# O_ACCMODE | O_CREAT | O_TRUNC | O_APPEND | __O_TMPFILE: any of these => not a pure read.
_OPEN_WRITE_MASK = 0o3 | 0o100 | 0o1000 | 0o2000 | 0o20000000


def _libc():
    import ctypes

    return ctypes.CDLL(None, use_errno=True)


def _sys(nr: int, *args: int) -> int:
    """Raw syscall; returns -1 and leaves errno set on failure."""
    import ctypes

    libc = _libc()
    libc.syscall.restype = ctypes.c_long
    return libc.syscall(ctypes.c_long(nr), *[ctypes.c_long(a) for a in args])


def _prctl(option: int, arg2: int = 0, arg3: int = 0, arg4: int = 0, arg5: int = 0) -> int:
    import ctypes

    libc = _libc()
    libc.prctl.restype = ctypes.c_int
    return libc.prctl(
        ctypes.c_int(option), ctypes.c_ulong(arg2), ctypes.c_ulong(arg3),
        ctypes.c_ulong(arg4), ctypes.c_ulong(arg5),
    )


def _errno() -> int:
    import ctypes

    return ctypes.get_errno()


def _set_rlimits(cfg: dict) -> None:
    import resource

    cpu = int(cfg.get("cpu_seconds") or 12)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    budget = int(cfg.get("memory_mb") or 1024) * 1024 * 1024
    vm = 0
    try:
        with open("/proc/self/statm", "rb") as fh:
            vm = int(fh.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        pass
    limit = vm + budget  # head-room on top of what pandas/numpy already mapped
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _no_new_privs() -> None:
    if _prctl(38, 1) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")


def _drop_capabilities(arch_id: int) -> None:
    import ctypes

    for cap in range(0, 64):  # PR_CAPBSET_DROP; stops at the first invalid cap
        if _prctl(24, cap) != 0:  # PR_CAPBSET_DROP
            break

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        ]

    header = Header(0x20080522, 0)
    data = (Data * 2)()
    if _sys(_SYS_CAPSET[arch_id], ctypes.addressof(header), ctypes.addressof(data)) != 0:
        raise OSError(_errno(), "capset failed")


# ---- Landlock ---------------------------------------------------------------

_LL_FS = {
    "EXECUTE": 1 << 0, "WRITE_FILE": 1 << 1, "READ_FILE": 1 << 2, "READ_DIR": 1 << 3,
    "REMOVE_DIR": 1 << 4, "REMOVE_FILE": 1 << 5, "MAKE_CHAR": 1 << 6,
    "MAKE_DIR": 1 << 7, "MAKE_REG": 1 << 8, "MAKE_SOCK": 1 << 9,
    "MAKE_FIFO": 1 << 10, "MAKE_BLOCK": 1 << 11, "MAKE_SYM": 1 << 12,
    "REFER": 1 << 13, "TRUNCATE": 1 << 14, "IOCTL_DEV": 1 << 15,
}


def _landlock_read_paths(workdir: str) -> list[str]:
    """Everything the child may READ. Nothing is writable."""
    import site
    import sysconfig

    candidates: set[str] = {workdir, sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix}
    candidates.update(v for v in sysconfig.get_paths().values() if v)
    try:
        candidates.update(site.getsitepackages())
    except Exception:  # noqa: BLE001
        pass
    candidates.update(p for p in sys.path if p and os.path.isdir(p))
    candidates.update({
        "/usr/lib", "/usr/lib64", "/lib", "/lib64", "/usr/local/lib",
        "/usr/share/zoneinfo", "/etc/localtime", "/etc/ld.so.cache",
        "/dev/null", "/dev/urandom", "/dev/random", "/sys/devices/system/cpu",
    })
    keep: list[str] = []
    for path in sorted(candidates):
        if not os.path.exists(path):
            continue
        real = os.path.realpath(path)
        # Never expose a directory that holds app secrets (e.g. an editable install
        # that put the project root on sys.path).
        if os.path.isdir(real) and any(
            os.path.exists(os.path.join(real, name)) for name in (".env", "secrets.toml", ".git")
        ):
            continue
        keep.append(real)
    return keep


def _apply_landlock(workdir: str) -> int:
    import ctypes

    abi = _sys(444, 0, 0, 1)  # landlock_create_ruleset(NULL, 0, VERSION)
    if abi < 1:
        raise OSError(_errno(), "Landlock is not available on this kernel")

    fs_bits = (1 << 13) - 1  # ABI 1: bits 0..12
    if abi >= 2:
        fs_bits |= _LL_FS["REFER"]
    if abi >= 3:
        fs_bits |= _LL_FS["TRUNCATE"]
    if abi >= 5:
        fs_bits |= _LL_FS["IOCTL_DEV"]
    net_bits = 0b11 if abi >= 4 else 0  # BIND_TCP | CONNECT_TCP
    scoped = 0b11 if abi >= 6 else 0  # abstract unix sockets | signals

    class RulesetAttr(ctypes.Structure):
        _fields_ = [
            ("handled_access_fs", ctypes.c_uint64),
            ("handled_access_net", ctypes.c_uint64),
            ("scoped", ctypes.c_uint64),
        ]

    attr = RulesetAttr(fs_bits, net_bits, scoped)
    size = 24 if abi >= 6 else 16 if abi >= 4 else 8
    ruleset_fd = _sys(444, ctypes.addressof(attr), size, 0)
    if ruleset_fd < 0:
        raise OSError(_errno(), "landlock_create_ruleset failed")

    class PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    try:
        for path in _landlock_read_paths(workdir):
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                allowed = _LL_FS["READ_FILE"]
                if os.path.isdir(path):
                    allowed |= _LL_FS["READ_DIR"]
                rule = PathBeneath(allowed, fd)
                if _sys(445, ruleset_fd, 1, ctypes.addressof(rule), 0) != 0:  # LANDLOCK_RULE_PATH_BENEATH
                    raise OSError(_errno(), f"landlock_add_rule failed for {path}")
            finally:
                os.close(fd)
        if _sys(446, ruleset_fd, 0) != 0:  # landlock_restrict_self
            raise OSError(_errno(), "landlock_restrict_self failed")
    finally:
        os.close(ruleset_fd)
    return abi


# ---- seccomp ----------------------------------------------------------------

_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_JGE_K = 0x35
_BPF_JSET_K = 0x45
_BPF_AND_K = 0x54
_BPF_RET_K = 0x06
_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_RET_ERRNO = 0x00050000
_EPERM, _ENOSYS = 1, 38


class _Bpf:
    """Tiny assembler with symbolic jump targets."""

    def __init__(self) -> None:
        self.ins: list[list] = []
        self.labels: dict[str, int] = {}

    def label(self, name: str) -> None:
        self.labels[name] = len(self.ins)

    def stmt(self, code: int, k: int = 0) -> None:
        self.ins.append([code, None, None, k])

    def jump(self, code: int, k: int, jt: str | None, jf: str | None) -> None:
        self.ins.append([code, jt, jf, k])

    def assemble(self) -> bytes:
        import struct

        out = bytearray()
        for idx, (code, jt, jf, k) in enumerate(self.ins):
            def rel(target: str | None) -> int:
                if target is None:
                    return 0
                off = self.labels[target] - (idx + 1)
                if not 0 <= off <= 255:
                    raise ValueError(f"BPF jump out of range ({off})")
                return off

            out += struct.pack("<HBBI", code, rel(jt), rel(jf), k & 0xFFFFFFFF)
        return bytes(out)


def _build_seccomp(arch_name: str) -> bytes:
    audit_arch, col = _ARCHES[arch_name]
    prog = _Bpf()
    prog.stmt(_BPF_LD_W_ABS, 4)  # seccomp_data.arch
    prog.jump(_BPF_JEQ_K, audit_arch, None, "kill")
    prog.stmt(_BPF_LD_W_ABS, 0)  # seccomp_data.nr
    if col == 0:
        prog.jump(_BPF_JGE_K, 0x40000000, "eperm", None)  # x32 ABI would dodge the table
    for _name, nrs in _SYS_ENOSYS.items():
        prog.jump(_BPF_JEQ_K, nrs[col], "enosys", None)
    for _name, nrs in _SYS_EPERM.items():
        if nrs[col] is not None:
            prog.jump(_BPF_JEQ_K, nrs[col], "eperm", None)
    prog.jump(_BPF_JEQ_K, _SYS_CLONE[col], "clone", None)
    if _SYS_OPEN[col] is not None:
        prog.jump(_BPF_JEQ_K, _SYS_OPEN[col], "open", None)
    prog.jump(_BPF_JEQ_K, _SYS_OPENAT[col], "openat", None)
    prog.stmt(_BPF_RET_K, _RET_ALLOW)

    prog.label("clone")  # args[0] = flags
    prog.stmt(_BPF_LD_W_ABS, 16)
    prog.jump(_BPF_JSET_K, _CLONE_THREAD, "allow", "eperm")
    prog.label("open")  # args[1] = flags
    prog.stmt(_BPF_LD_W_ABS, 24)
    prog.stmt(_BPF_AND_K, _OPEN_WRITE_MASK)
    prog.jump(_BPF_JEQ_K, 0, "allow", "eperm")
    prog.label("openat")  # args[2] = flags
    prog.stmt(_BPF_LD_W_ABS, 32)
    prog.stmt(_BPF_AND_K, _OPEN_WRITE_MASK)
    prog.jump(_BPF_JEQ_K, 0, "allow", "eperm")
    prog.label("allow")
    prog.stmt(_BPF_RET_K, _RET_ALLOW)
    prog.label("eperm")
    prog.stmt(_BPF_RET_K, _RET_ERRNO | _EPERM)
    prog.label("enosys")
    prog.stmt(_BPF_RET_K, _RET_ERRNO | _ENOSYS)
    prog.label("kill")
    prog.stmt(_BPF_RET_K, _RET_KILL_PROCESS)
    return prog.assemble()


def _apply_seccomp(arch_name: str) -> None:
    import ctypes

    blob = _build_seccomp(arch_name)
    buf = ctypes.create_string_buffer(blob, len(blob))

    class Fprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    fprog = Fprog(len(blob) // 8, ctypes.addressof(buf))
    nr = _SYS_SECCOMP[_ARCHES[arch_name][1]]
    # SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC (covers already-running threads)
    if _sys(nr, 1, 1, ctypes.addressof(fprog)) != 0:
        raise OSError(_errno(), "seccomp(SET_MODE_FILTER) failed")


# ---- orchestration ----------------------------------------------------------

_REQUIRED_LAYERS = ("rlimits", "no_new_privs", "landlock", "seccomp")


def _verify(workdir: str, secret_probe: str | None) -> dict[str, bool]:
    """Try to do the forbidden things. True = the operation was refused."""
    import socket

    result: dict[str, bool] = {}
    try:
        sock = socket.socket()
        sock.close()
        result["network_blocked"] = False
    except OSError:
        result["network_blocked"] = True
    if secret_probe:
        try:
            with open(secret_probe, "rb") as fh:
                fh.read(1)
            result["fs_read_confined"] = False
        except OSError:
            result["fs_read_confined"] = True
    else:
        result["fs_read_confined"] = False
    try:
        with open(os.path.join(workdir, ".sandbox_write_probe"), "w") as fh:
            fh.write("x")
        result["fs_write_blocked"] = False
    except OSError:
        result["fs_write_blocked"] = True
    return result


def _harden(cfg: dict, workdir: str) -> tuple[list[str], list[str], dict]:
    applied: list[str] = []
    missing: list[str] = []
    verify: dict = {}
    if not _IS_LINUX:
        return applied, ["linux-only (rlimits, landlock, seccomp unavailable)"], verify

    import platform

    arch_name = platform.machine().lower()
    secret_probe = next(
        (p for p in ("/etc/passwd", "/etc/hostname", "/etc/os-release", "/bin/sh") if os.path.exists(p)),
        None,
    )

    steps = [
        ("rlimits", lambda: _set_rlimits(cfg)),
        ("no_new_privs", _no_new_privs),
        ("landlock", lambda: _apply_landlock(workdir)),
    ]
    if arch_name in _ARCHES:
        steps.append(("seccomp", lambda: _apply_seccomp(arch_name)))
    else:
        missing.append(f"seccomp (unsupported architecture {arch_name})")
    for name, fn in steps:
        try:
            fn()
            applied.append(name)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{name} ({exc})")
    try:
        _drop_capabilities(_ARCHES[arch_name][1] if arch_name in _ARCHES else 0)
        applied.append("caps_dropped")
    except Exception:  # noqa: BLE001 - best effort; not required
        pass

    verify = _verify(workdir, secret_probe)
    for check, ok in verify.items():
        if not ok:
            missing.append(f"verification failed: {check}")
    return applied, missing, verify


# --------------------------------------------------------------------------- #
# Generated-code execution
# --------------------------------------------------------------------------- #

_USER_FILE = "<generated>"
_REMOVED_BUILTINS = frozenset({
    "eval", "exec", "compile", "input", "breakpoint", "exit", "quit", "help",
    "memoryview", "globals", "locals", "vars", "getattr", "setattr", "delattr",
})


def _make_builtins(allowed_roots: frozenset[str], workdir: str) -> dict:
    import builtins

    real_import = builtins.__import__
    real_open = builtins.open
    root = os.path.realpath(workdir)

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if level or (name or "").split(".", 1)[0] not in allowed_roots:
            raise PermissionError(f"SandboxSecurityError: import of '{name}' is not allowed.")
        return real_import(name, globals, locals, fromlist, level)

    def guarded_open(file, mode="r", *args, **kwargs):
        if set(str(mode)) & set("wxa+"):
            raise PermissionError("SandboxSecurityError: writing files is disabled.")
        if isinstance(file, int):
            raise PermissionError("SandboxSecurityError: opening raw file descriptors is disabled.")
        target = os.path.realpath(os.path.join(root, os.fspath(file)))
        if target != root and not target.startswith(root + os.sep):
            raise PermissionError("SandboxSecurityError: file access is limited to the data directory.")
        return real_open(file, mode, *args, **kwargs)

    safe = {k: v for k, v in vars(builtins).items() if k not in _REMOVED_BUILTINS}
    safe["__import__"] = guarded_import
    safe["open"] = guarded_open
    return safe


def _print_user_exception(exc: BaseException) -> None:
    import traceback

    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == _USER_FILE]
    lines = ["Traceback (most recent call last):\n"]
    lines += traceback.format_list(frames)
    lines += traceback.format_exception_only(type(exc), exc)
    sys.stderr.write("".join(lines))


def _make_parse_money(re_module):
    """Return a parse_money(val) -> (float, str|None) function built from stdlib only.

    Injected directly into the sandbox namespace so generated code can call
    parse_money(val) without importing anything.  Keeping it here (not in the
    project) preserves the runner's self-contained requirement.
    """
    _symbols = [
        ("CA$", "CAD"), ("A$", "AUD"), ("NZ$", "NZD"), ("HK$", "HKD"),
        ("S$", "SGD"), ("US$", "USD"), ("R$", "BRL"),
        ("\u20ac", "EUR"), ("\u00a3", "GBP"), ("\u00a5", "JPY"),
        ("\u20b9", "INR"), ("\u20a9", "KRW"), ("$", "USD"),
    ]

    def parse_money(val):
        """Parse a messy price string into (amount: float, currency: str | None).

        Returns (nan, None) for blanks/nulls.  Never assumes a bare number is USD.
        Longer symbol prefixes are matched first (CA$ before $).
        """
        import math
        s = str(val).strip()
        if not s or s.lower() in {"nan", "none", "null"}:
            return float("nan"), None
        unit = None
        rest = s
        for sym, code in _symbols:
            if sym in s:
                unit = code
                rest = s.replace(sym, "", 1)
                break
        iso = re_module.search(r"(?<![A-Z])([A-Z]{3})(?![A-Z])", rest.upper())
        if iso:
            unit = iso.group(1)
            rest = re_module.sub(iso.group(1), "", rest, count=1, flags=re_module.IGNORECASE)
        if unit is None:
            letters = re_module.findall(r"[A-Za-z]+", rest)
            if letters:
                unit = letters[-1].upper()
                rest = re_module.sub(re_module.escape(letters[-1]), "", rest, count=1, flags=re_module.IGNORECASE)
        num = rest.replace(" ", "")
        if num.count(",") == 1 and num.count(".") == 0:
            num = num.replace(",", ".")
        else:
            num = num.replace(",", "")
        num = re_module.sub(r"[^0-9.\-]", "", num)
        try:
            amount = float(num) if num not in {"", ".", "-", "-."} else float("nan")
        except Exception:
            amount = float("nan")
        return amount, unit

    return parse_money


def _run_user_code(code: str, cfg: dict, namespace_modules: dict, workdir: str) -> int:
    import linecache

    linecache.cache[_USER_FILE] = (len(code), None, code.splitlines(True), _USER_FILE)
    try:
        compiled = compile(code, _USER_FILE, "exec")
    except SyntaxError as exc:
        _print_user_exception(exc)
        return 1
    builtins_for_user = (
        _make_builtins(frozenset(cfg.get("allowed_roots") or ()), workdir)
        if cfg.get("python_guards", True)
        else __builtins__
    )
    namespace = {"__name__": "__main__", "__builtins__": builtins_for_user, **namespace_modules}
    pd = namespace_modules["pd"]
    # Pandas 3 may infer Arrow-backed strings by default when PyArrow is installed.
    # On small hosted instances even tiny Arrow allocations can fail under pressure.
    # Keep CSV string columns on pandas' Python-backed storage in the sandbox.
    try:
        pd.options.mode.string_storage = "python"
    except (AttributeError, ValueError):
        pass
    try:
        pd.options.future.infer_string = False
    except AttributeError:
        pass
    original_read_csv = pd.read_csv

    def tracked_read_csv(filepath_or_buffer, *args, **kwargs):
        result = original_read_csv(filepath_or_buffer, *args, **kwargs)
        if isinstance(filepath_or_buffer, (str, bytes, os.PathLike)):
            candidate = os.path.realpath(os.path.join(workdir, os.fsdecode(filepath_or_buffer)))
            if os.path.dirname(candidate) == os.path.realpath(workdir) and candidate.lower().endswith(".csv"):
                _CSV_READS.add(os.path.basename(candidate))
        return result

    pd.read_csv = tracked_read_csv
    try:
        exec(compiled, namespace)  # noqa: S102 - this IS the sandboxed execution
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 0
    except BaseException as exc:  # noqa: BLE001
        _print_user_exception(exc)
        return 1
    return 0


def main() -> int:
    global _STATUS_FD
    cfg = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    _STATUS_FD = cfg.get("status_fd")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    # Generated code gets no stdin.
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)

    workdir = os.getcwd()
    import importlib
    import re
    from pathlib import Path

    import numpy as np
    import pandas as pd

    for mod in cfg.get("preload") or ():
        try:
            importlib.import_module(mod)
        except Exception:  # noqa: BLE001
            pass

    applied, missing, verify = _harden(cfg, workdir)
    if cfg.get("strict") and missing:
        _status({"event": "error", "applied": applied, "missing": missing})
        sys.stderr.write("SandboxUnavailable: required isolation layers are missing: " + "; ".join(missing) + "\n")
        return EXIT_HARDENING_FAILED
    if cfg.get("probe"):
        _status({"event": "probe", "applied": applied, "missing": missing, "verify": verify})
        return 0

    _status({"event": "ready", "applied": applied, "missing": missing})
    rc = _run_user_code(
        cfg["code"],
        cfg,
        {"pd": pd, "np": np, "json": json, "re": re, "Path": Path,
         "parse_money": _make_parse_money(re)},
        workdir,
    )
    _status({"event": "csv_reads", "files": sorted(_CSV_READS)})
    sys.stdout.flush()
    sys.stderr.flush()
    return rc


if __name__ == "__main__":
    os._exit(main())

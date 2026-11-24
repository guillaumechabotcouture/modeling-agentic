#!/usr/bin/env python3
"""Kernel-enforced, unprivileged confinement of one derivation process (Linux; scripts/pipeline.py `_child`, kind
`landlock-seccomp`; docs/design/v2-derived-inputs.md §7). The launcher applies it to itself before the script runs:

  no_new_privs, then Landlock: reads only beneath the granted read-only roots, writes only in the granted
  directories and only of regular files and directories; every other path, the run directory included, is refused
  whatever name reaches it (ABI 2+: no cross-directory rename or link; 4+: no TCP; 6+: no outside signals), then
  seccomp-bpf: DENY below, clone only for threads (clone3 answers ENOSYS), prlimit64 only to read, kill and tgkill
  only to itself; a foreign architecture or the x32 ABI kills the process.

Both hold for the whole process, native code and second interpreters included. `apply` raises OSError when any step
fails (the caller fails closed).

    python3 scripts/lib/kernel_sandbox.py --self-test
"""
from __future__ import annotations

import ctypes
import os
import platform
import struct
import sys

PR_SET_NO_NEW_PRIVS = 38
SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC = 1, 1
RET_ALLOW, RET_KILL, RET_ERRNO = 0x7FFF0000, 0x80000000, 0x00050000
EPERM, ENOSYS = 1, 38
CLONE_THREAD = 0x00010000
ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
# syscall numbers per architecture (x86_64 table; aarch64 uses the generic table)
NR = {
    "x86_64": dict(seccomp=317, socket=41, socketpair=53, fork=57, vfork=58, clone=56, clone3=435, execve=59,
                   execveat=322, ptrace=101, process_vm_readv=310, process_vm_writev=311, setrlimit=160,
                   prlimit64=302, mount=165, umount2=166, unshare=272, setns=308, pivot_root=155, chroot=161, bpf=321,
                   perf_event_open=298, userfaultfd=323, io_uring_setup=425, io_uring_enter=426,
                   io_uring_register=427, kill=62, tkill=200, tgkill=234, rt_sigqueueinfo=129,
                   rt_tgsigqueueinfo=297, pidfd_send_signal=424, pidfd_open=434, pidfd_getfd=438, add_key=248,
                   request_key=249, keyctl=250, open_tree=428, move_mount=429, fsopen=430, fsconfig=431,
                   fsmount=432, fspick=433, mount_setattr=442, kcmp=312),
    "aarch64": dict(seccomp=277, socket=198, socketpair=199, clone=220, clone3=435, execve=221, execveat=281,
                    ptrace=117, process_vm_readv=270, process_vm_writev=271, setrlimit=164, prlimit64=261, mount=40,
                    umount2=39, unshare=97, setns=268, pivot_root=41, chroot=51, bpf=280, perf_event_open=241,
                    userfaultfd=282, io_uring_setup=425, io_uring_enter=426, io_uring_register=427, kill=129,
                    tkill=130, tgkill=131, rt_sigqueueinfo=138, rt_tgsigqueueinfo=240, pidfd_send_signal=424,
                    pidfd_open=434, pidfd_getfd=438, add_key=217, request_key=218, keyctl=219, open_tree=428,
                    move_mount=429, fsopen=430, fsconfig=431, fsmount=432, fspick=433, mount_setattr=442, kcmp=272)}
DENY = ("socket", "socketpair", "fork", "vfork", "execve", "execveat", "ptrace", "process_vm_readv",
        "process_vm_writev", "setrlimit", "mount", "umount2", "unshare", "setns", "pivot_root", "chroot", "bpf",
        "perf_event_open", "userfaultfd", "io_uring_setup", "io_uring_enter", "io_uring_register", "tkill",
        "rt_sigqueueinfo", "rt_tgsigqueueinfo", "pidfd_send_signal", "pidfd_open", "pidfd_getfd", "add_key",
        "request_key", "keyctl", "open_tree", "move_mount", "fsopen", "fsconfig", "fsmount", "fspick",
        "mount_setattr", "kcmp")
# Landlock
LL_CREATE_RULESET, LL_ADD_RULE, LL_RESTRICT_SELF = 444, 445, 446
LL_RULE_PATH_BENEATH, LL_VERSION = 1, 1
FS_EXECUTE, FS_WRITE_FILE, FS_READ_FILE, FS_READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
FS_REMOVE_DIR, FS_REMOVE_FILE, FS_MAKE_DIR, FS_MAKE_REG = 1 << 4, 1 << 5, 1 << 7, 1 << 8
FS_TRUNCATE = 1 << 14
FS_FILE_ONLY = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | (1 << 15)


def fs_handled(abi: int) -> int:
    """Every filesystem right the ABI knows (each one not granted below is refused)."""
    return (1 << (13 if abi < 2 else 14 if abi < 3 else 15 if abi < 5 else 16)) - 1


def _ld(off):
    return (0x20, 0, 0, off)                     # BPF_LD | BPF_W | BPF_ABS


def _jeq(k, jt, jf):
    return (0x15, jt, jf, k)


def _ret(k):
    return (0x06, 0, 0, k)


def bpf_program(arch: str, pid: int) -> list[tuple]:
    """The filter as (code, jt, jf, k) instructions for `arch` (a key of ARCH), with `pid` the process allowed to
    signal itself. Every block ends in a return, so the accumulator holds the syscall number between blocks."""
    nr = NR[arch]
    perm = RET_ERRNO | EPERM
    prog = [_ld(4), _jeq(ARCH[arch], 1, 0), _ret(RET_KILL), _ld(0)]
    if arch == "x86_64":
        prog += [(0x35, 0, 1, 0x40000000), _ret(RET_KILL)]          # JGE: the x32 ABI
    for name in DENY:
        if name in nr:
            prog += [_jeq(nr[name], 0, 1), _ret(perm)]
    prog += [_jeq(nr["clone3"], 0, 1), _ret(RET_ERRNO | ENOSYS)]
    # clone: threads only
    prog += [_jeq(nr["clone"], 0, 4), _ld(16), (0x45, 0, 1, CLONE_THREAD), _ret(RET_ALLOW), _ret(perm)]
    # prlimit64: allowed only with a NULL new limit (arg 2), i.e. reading a limit
    prog += [_jeq(nr["prlimit64"], 0, 6), _ld(32), _jeq(0, 0, 3), _ld(36), _jeq(0, 0, 1), _ret(RET_ALLOW), _ret(perm)]
    # kill and tgkill: only to this process (arg 0)
    for name in ("kill", "tgkill"):
        prog += [_jeq(nr[name], 0, 6), _ld(16), _jeq(pid & 0xFFFFFFFF, 0, 3), _ld(20), _jeq(0, 0, 1),
                 _ret(RET_ALLOW), _ret(perm)]
    return prog + [_ret(RET_ALLOW)]


def run_bpf(prog: list[tuple], arch_k: int, nr: int, args=(0, 0, 0, 0, 0, 0)) -> int:
    """Interpret the filter on one synthetic seccomp_data (the self-test's model of the kernel's check)."""
    data = struct.pack("<iIQ6Q", nr, arch_k, 0, *[a & 0xFFFFFFFFFFFFFFFF for a in args])
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = prog[pc]
        if code == 0x20:
            acc = struct.unpack_from("<I", data, k)[0]
        elif code == 0x06:
            return k
        elif code in (0x15, 0x45, 0x35):
            hit = acc == k if code == 0x15 else bool(acc & k) if code == 0x45 else acc >= k
            pc += jt if hit else jf
        else:
            raise ValueError(f"unknown BPF code {code:#x}")
        pc += 1


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def landlock_abi() -> int:
    if not sys.platform.startswith("linux"):
        return 0
    try:
        r = _libc().syscall(LL_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(LL_VERSION))
    except (OSError, AttributeError):
        return 0
    return int(r) if r > 0 else 0


def _check(r, what):
    if r < 0:
        e = ctypes.get_errno()
        raise OSError(e, f"{what}: {os.strerror(e)}")
    return r


def apply(ro: list[str], rw: list[str], rw_files: list[str] = ()) -> dict:
    """Confine this process for good: no_new_privs, Landlock (`ro` read-only roots, `rw` writable directories,
    `rw_files` single files read and written, e.g. /dev/null), then seccomp. Returns what was applied. Any failure
    raises OSError: the caller must not run the derivation."""
    arch = platform.machine()
    if not sys.platform.startswith("linux") or arch not in ARCH:
        raise OSError(EPERM, f"no kernel sandbox for {sys.platform}/{arch}")
    abi = landlock_abi()
    if abi < 1:
        raise OSError(EPERM, "Landlock is not available")
    libc = _libc()
    _check(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "prctl(PR_SET_NO_NEW_PRIVS)")
    handled = fs_handled(abi)
    attr = struct.pack("<QQQ", handled, 3 if abi >= 4 else 0, 3 if abi >= 6 else 0)    # net: bind, connect TCP
    size = 8 if abi < 4 else 16 if abi < 6 else 24
    buf = ctypes.create_string_buffer(attr, len(attr))
    fd = _check(libc.syscall(LL_CREATE_RULESET, buf, ctypes.c_size_t(size), ctypes.c_uint32(0)),
                "landlock_create_ruleset")
    try:
        read = FS_READ_FILE | FS_READ_DIR
        write = read | FS_WRITE_FILE | FS_REMOVE_DIR | FS_REMOVE_FILE | FS_MAKE_DIR | FS_MAKE_REG | (
            FS_TRUNCATE if abi >= 3 else 0)
        for paths, access in ((ro, read), (rw, write), (rw_files, (FS_READ_FILE | FS_WRITE_FILE) & handled)):
            for p in paths:
                pfd = os.open(p, os.O_PATH | os.O_CLOEXEC)
                try:
                    acc = access & handled & (FS_FILE_ONLY if not os.path.isdir(p) else -1)
                    rule = ctypes.create_string_buffer(struct.pack("<Qi", acc, pfd), 12)       # packed
                    _check(libc.syscall(LL_ADD_RULE, ctypes.c_int(fd), ctypes.c_int(LL_RULE_PATH_BENEATH), rule,
                                        ctypes.c_uint32(0)), f"landlock_add_rule {p}")
                finally:
                    os.close(pfd)
        _check(libc.syscall(LL_RESTRICT_SELF, ctypes.c_int(fd), ctypes.c_uint32(0)), "landlock_restrict_self")
    finally:
        os.close(fd)
    prog = bpf_program(arch, os.getpid())

    class Fprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]
    raw = b"".join(struct.pack("<HBBI", *i) for i in prog)
    fbuf = ctypes.create_string_buffer(raw, len(raw))
    fp = Fprog(len(prog), ctypes.cast(fbuf, ctypes.c_void_p))
    _check(libc.syscall(ctypes.c_long(NR[arch]["seccomp"]), ctypes.c_uint(SECCOMP_SET_MODE_FILTER),
                        ctypes.c_uint(SECCOMP_FILTER_FLAG_TSYNC), ctypes.byref(fp)), "seccomp(SET_MODE_FILTER)")
    return {"landlock_abi": abi, "seccomp": "filter", "no_new_privs": True, "net_tcp_landlock": abi >= 4,
            "scoped_signals": abi >= 6}


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    perm, allow = 0x00050000 | EPERM, RET_ALLOW
    for arch in ARCH:
        n, prog, me = NR[arch], bpf_program(arch, 4242), ARCH[arch]
        cases = [("socket", n["socket"], (2, 1, 0), perm), ("socketpair (AF_UNIX)", n["socketpair"], (1, 1, 0), perm),
                 ("execve", n["execve"], (), perm), ("ptrace", n["ptrace"], (), perm),
                 ("clone for a process", n["clone"], (0x11,), perm),
                 ("clone for a thread", n["clone"], (0x3D0F00,), allow),
                 ("clone3", n["clone3"], (), 0x00050000 | ENOSYS),
                 ("prlimit64 reading", n["prlimit64"], (0, 0, 0, 1), allow),
                 ("prlimit64 setting", n["prlimit64"], (0, 0, 0x7FFF1000, 0), perm),
                 ("setrlimit", n["setrlimit"], (0, 0x7FFF1000), perm),
                 ("kill self", n["kill"], (4242, 9), allow), ("kill another", n["kill"], (1, 9), perm),
                 ("kill a group", n["kill"], (-1, 9), perm), ("tgkill self", n["tgkill"], (4242, 4243, 6), allow),
                 ("unshare", n["unshare"], (), perm), ("mount", n["mount"], (), perm),
                 ("io_uring_setup", n["io_uring_setup"], (), perm), ("seccomp itself", n["seccomp"], (), allow),
                 ("an ordinary syscall (nr 63)", 63, (), allow)]
        if arch == "x86_64":
            cases += [("fork", n["fork"], (), perm), ("vfork", n["vfork"], (), perm),
                      ("the x32 ABI", 0x40000000 | 41, (), RET_KILL)]
        for label, nr, args, want in cases:
            got = run_bpf(prog, me, nr, tuple(args) + (0,) * (6 - len(args)))
            ok(got == want, f"KS1 {arch}: {label} -> {got:#x} (want {want:#x})")
        other = ARCH["aarch64" if arch == "x86_64" else "x86_64"]
        ok(run_bpf(prog, other, 0) == RET_KILL, f"KS2 {arch}: a foreign architecture is killed")
    ok(fs_handled(1) == (1 << 13) - 1 and fs_handled(4) == (1 << 15) - 1 and fs_handled(8) == (1 << 16) - 1,
       "KS3 the handled filesystem rights follow the Landlock ABI")
    if not sys.platform.startswith("linux"):
        try:
            apply([], [])
            ok(False, "KS4 apply refuses where there is no kernel sandbox")
        except OSError:
            ok(True, "KS4 apply refuses where there is no kernel sandbox (fail closed)")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv[1:] else 2)

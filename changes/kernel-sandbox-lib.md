---
phase: "v2"
commit: "KS α"
---

## Changelog

**Kernel sandbox library: unprivileged Landlock and seccomp confinement of one process.** `scripts/lib/kernel_sandbox.py` (not wired yet; #368 wires it as the `landlock-seccomp` derivation fallback for containers without bubblewrap). `apply(ro, rw, rw_files, ro_files)` confines the calling process for good, in this order:
- it requires exactly one thread;
- as root, it becomes an unprivileged uid and gid (refused without one);
- it drops every capability and checks with capget;
- it sets no_new_privs;
- it applies a Landlock ruleset, which needs ABI 3 or later (Linux 6.2) and otherwise refuses: reads only beneath the read-only grants, writes only in the writable directories (regular files and directories only), TCP refused from ABI 4 and outside signals from ABI 6;
- it installs a hand-built seccomp-bpf filter for x86_64 and aarch64 with TSYNC, where any return but 0 is a failure. The filter refuses sockets, process creation and exec, ptrace, setting limits, mount and namespace calls, io_uring, `fcntl` F_SETOWN/F_SETOWN_EX/F_SETSIG, every `ioctl` outside a short allowlist (answered ENOTTY), chmod/chown/utime/xattr writes, SysV IPC and mqueues, and signals to other processes. It kills a foreign architecture or the x32 ABI, and answers ENOSYS above the highest reviewed syscall (462).

Any failed step raises OSError, so the caller fails closed. Self-tests KS1 to KS9: the filter run through a BPF interpreter for both architectures, the ABI floor, capabilities, TSYNC and the thread check and the root switch.

"""Memory footprint and CPU per owner: each pane, the periscope server, its
webview, and everything else on the machine.

Footprint (`phys_footprint`, Activity Monitor's Memory), not RSS: RSS leaves
out compressed and swapped pages, which is where a machine deep in swap keeps
most of its memory — the webview read 276 MB resident against 2.8 GB of
footprint. Read through libproc/libSystem with ctypes: ~3 µs a process, no
subprocess.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

from periscope.agent_processes import ProcessInfo, descendants

_u64 = ctypes.c_uint64
_RUSAGE_INFO_V1 = 1

# The Tauri shell's executable (src-tauri). Its WebKit helpers have launchd as
# their parent, so they're found by responsible pid instead.
_APP_NAME = "periscope-app"


class _RusageInfoV1(ctypes.Structure):
    # <sys/resource.h> rusage_info_v1: the v0 fields recycle.rs binds, plus
    # the CPU time of reaped children (144 bytes).
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, _u64)
        for name in (
            "user_time",
            "system_time",
            "pkg_idle_wkups",
            "interrupt_wkups",
            "pageins",
            "wired_size",
            "resident_size",
            "phys_footprint",
            "proc_start_abstime",
            "proc_exit_abstime",
            "child_user_time",
            "child_system_time",
            "child_pkg_idle_wkups",
            "child_interrupt_wkups",
            "child_pageins",
            "child_elapsed_abstime",
        )
    ]


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class _SwapUsage(ctypes.Structure):
    # <sys/sysctl.h> struct xsw_usage
    _fields_ = [
        ("total", _u64),
        ("avail", _u64),
        ("used", _u64),
        ("pagesize", ctypes.c_uint32),
        ("encrypted", ctypes.c_int32),
    ]


_libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
_libproc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_libsystem = ctypes.CDLL("/usr/lib/libSystem.dylib")
_libsystem.responsibility_get_pid_responsible_for_pid.argtypes = [ctypes.c_int]
_libsystem.responsibility_get_pid_responsible_for_pid.restype = ctypes.c_int
_libsystem.mach_absolute_time.restype = _u64
_timebase = _Timebase()
_libsystem.mach_timebase_info(ctypes.byref(_timebase))
# rusage times are mach absolute units: 125/3 ns each on Apple silicon.
_NS_PER_TICK = _timebase.numer / _timebase.denom


@dataclass(frozen=True)
class Reading:
    footprint: int  # bytes
    cpu_ns: int  # own CPU time plus its reaped children's
    start: int  # ns on _now()'s clock; (pid, start) is a process's identity


def _read(pid: int) -> Reading | None:
    """One process's rusage, or None when it is gone or not the user's
    (root-owned processes refuse)."""
    info = _RusageInfoV1()
    if _libproc.proc_pid_rusage(pid, _RUSAGE_INFO_V1, ctypes.byref(info)) != 0:
        return None
    ticks = info.user_time + info.system_time + info.child_user_time + info.child_system_time
    return Reading(info.phys_footprint, int(ticks * _NS_PER_TICK), int(info.proc_start_abstime * _NS_PER_TICK))


def _responsible(pid: int) -> int:
    """The pid macOS holds responsible for `pid`: for a WebKit helper, the app
    hosting the webview. A private libSystem call, like the shell's
    `_webProcessIdentifier`."""
    return _libsystem.responsibility_get_pid_responsible_for_pid(pid)


def _now() -> int:
    """ns on the clock `proc_start_abstime` counts on."""
    return int(_libsystem.mach_absolute_time() * _NS_PER_TICK)


def footprint_mb(pid: int) -> int | None:
    reading = _read(pid)
    return None if reading is None else reading.footprint >> 20


def _sysctl(name: str, buf: ctypes.Structure | ctypes.c_int) -> None:
    size = ctypes.c_size_t(ctypes.sizeof(buf))
    _libsystem.sysctlbyname(name.encode(), ctypes.byref(buf), ctypes.byref(size), None, ctypes.c_size_t(0))


def machine() -> dict:
    """Load, swap and free memory, straight from the kernel. mem_free_pct is the
    figure `memory_pressure` prints."""
    swap = _SwapUsage()
    level = ctypes.c_int()
    _sysctl("vm.swapusage", swap)
    _sysctl("kern.memorystatus_level", level)
    return {
        "load1": round(os.getloadavg()[0], 1),
        "swap_used_mb": swap.used >> 20,
        "swap_total_mb": swap.total >> 20,
        "mem_free_pct": level.value,
    }


def _assign(snapshot: dict[int, ProcessInfo], panes: dict[str, int]) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """Every process in exactly one group: ({"server", "webview"?, "panes",
    "elsewhere"} → pids, pane_id → pids). Claimed in that order, so a process
    is never counted twice."""
    claimed: set[int] = set()

    def claim(pids: set[int]) -> set[int]:
        fresh = pids - claimed
        claimed.update(fresh)
        return fresh

    def subtree(root: int) -> set[int]:
        return {p.pid for p in descendants(root, snapshot)}

    named = {"server": claim(subtree(os.getpid()))}
    apps = {p.pid for p in snapshot.values() if os.path.basename(p.comm) == _APP_NAME}
    if apps:
        named["webview"] = claim(apps | {pid for pid in snapshot if _responsible(pid) in apps})
    per_pane = {pane_id: claim(subtree(root)) for pane_id, root in panes.items()}
    named["panes"] = set().union(*per_pane.values())
    named["elsewhere"] = set(snapshot) - claimed
    return named, per_pane

"""Pick how many agents may run at once from the task count and this machine's resources."""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys

AGENT_MEMORY_GB = 0.5  # rough footprint of one agent CLI process (claude or gemini) plus the tools it spawns
MAX_PARALLEL = 8  # beyond this, API rate limits dominate and returns diminish
# macOS has no SC_AVPHYS_PAGES; vm_stat reports its pages instead ("Pages free:   12345.")
VM_STAT_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
VM_STAT_PAGES = re.compile(r"^Pages (free|inactive|speculative):\s+(\d+)\.$", re.MULTILINE)
VM_STAT_TIMEOUT = 10  # seconds


class MemoryStatus(ctypes.Structure):
    # Win32 MEMORYSTATUSEX, read through GlobalMemoryStatusEx (no extra package needed)
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def FreeMemoryGb() -> float | None:
    if sys.platform == "win32":
        status = MemoryStatus(dwLength=ctypes.sizeof(MemoryStatus))
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullAvailPhys / 1024**3
        return None
    if sys.platform == "darwin":
        return VmStatFreeGb()
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024**3
    except (AttributeError, ValueError, OSError):
        return None


def VmStatFreeGb() -> float | None:
    """macOS: free, inactive and speculative pages - what new processes get without swapping. None = unknown."""
    try:
        output = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=VM_STAT_TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    pageSize = VM_STAT_PAGE_SIZE.search(output)
    pages = dict(VM_STAT_PAGES.findall(output))
    if not pageSize or "free" not in pages:
        return None
    return sum(map(int, pages.values())) * int(pageSize[1]) / 1024**3


def AutoParallel(agentsNeeded: int) -> tuple[int, str]:
    """Return (agent count, reason): the tightest of agents needed, CPU cores, free memory and the cap."""
    cpu = os.cpu_count() or 2
    memory = FreeMemoryGb()
    limits = {"agents needed": agentsNeeded, "CPU cores": cpu, "cap": MAX_PARALLEL}
    if memory is not None:
        limits["free memory"] = int(memory / AGENT_MEMORY_GB)
    bound = min(limits, key=limits.get)
    memoryText = f", free memory {memory:.1f} GB" if memory is not None else ""
    reason = f"auto, limited by {bound}: needed {agentsNeeded}, CPU {cpu}{memoryText}, cap {MAX_PARALLEL}"
    return max(1, limits[bound]), reason

"""Windows Job Object supervisor with atomic worker and caller ownership.

Only the supervisor holds the non-inheritable kill-on-close job handle. The
actual command starts inside that job, and the supervisor watches a stable
caller process handle as well as its child. This is not a security sandbox for
work delegated to external services. Native Windows qualification is required.
"""

from __future__ import annotations

import ctypes as ct
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, cast

from bijux_ontology.errors import InputError, ResourceError

_DWORD = ct.c_uint32
_HANDLE = ct.c_void_p
_SIZE = ct.c_size_t
_INVALID_HANDLE = ct.c_void_p(-1).value
_INFINITE = 0xFFFFFFFF
_WAIT_TIMEOUT = 258
_SYNCHRONIZE = 0x00100000
_QUERY_LIMITED = 0x1000
_KILL_ON_CLOSE = 0x2000
_JOB_LIST = 0x0002000D
_HANDLE_LIST = 0x00020002


class _FileTime(ct.Structure):
    """The documented two-DWORD Windows creation timestamp."""

    _fields_ = [("low", _DWORD), ("high", _DWORD)]


class _Startup(ct.Structure):
    """STARTUPINFOW with the worker's explicitly inherited standard handles."""

    _fields_ = [
        ("cb", _DWORD),
        ("reserved", ct.c_wchar_p),
        ("desktop", ct.c_wchar_p),
        ("title", ct.c_wchar_p),
        ("x", _DWORD),
        ("y", _DWORD),
        ("x_size", _DWORD),
        ("y_size", _DWORD),
        ("x_chars", _DWORD),
        ("y_chars", _DWORD),
        ("fill", _DWORD),
        ("flags", _DWORD),
        ("show", ct.c_uint16),
        ("reserved_size", ct.c_uint16),
        ("reserved_bytes", ct.c_void_p),
        ("stdin", _HANDLE),
        ("stdout", _HANDLE),
        ("stderr", _HANDLE),
    ]


class _StartupEx(ct.Structure):
    """STARTUPINFOEXW carrying public job and handle-list attributes."""

    _fields_ = [("startup", _Startup), ("attributes", ct.c_void_p)]


class _ProcessInfo(ct.Structure):
    """The process and primary thread handles returned by CreateProcessW."""

    _fields_ = [
        ("process", _HANDLE),
        ("thread", _HANDLE),
        ("pid", _DWORD),
        ("tid", _DWORD),
    ]


class _BasicLimits(ct.Structure):
    """JOBOBJECT_BASIC_LIMIT_INFORMATION with pointer-sized native fields."""

    _fields_ = [
        ("process_time", ct.c_int64),
        ("job_time", ct.c_int64),
        ("flags", _DWORD),
        ("minimum_working_set", _SIZE),
        ("maximum_working_set", _SIZE),
        ("active_limit", _DWORD),
        ("affinity", _SIZE),
        ("priority", _DWORD),
        ("scheduling", _DWORD),
    ]


class _IoCounters(ct.Structure):
    """The six public unsigned 64-bit IO_COUNTERS fields."""

    _fields_ = [
        (name, ct.c_uint64)
        for name in (
            "read_operations",
            "write_operations",
            "other_operations",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ct.Structure):
    """The required extended job-limit structure, with no breakaway flags."""

    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", _SIZE),
        ("job_memory", _SIZE),
        ("peak_process_memory", _SIZE),
        ("peak_job_memory", _SIZE),
    ]


class _Accounting(ct.Structure):
    """Bounded accounting for completion of the owned job and nested jobs."""

    _fields_ = [
        ("user", ct.c_int64),
        ("kernel", ct.c_int64),
        ("period_user", ct.c_int64),
        ("period_kernel", ct.c_int64),
        ("faults", _DWORD),
        ("total", _DWORD),
        ("active", _DWORD),
        ("terminated", _DWORD),
    ]


class _Kernel:
    """One adapter for documented Kernel32 functions, loaded only on Windows."""

    def __init__(self) -> None:
        factory = getattr(ct, "WinDLL", None)
        last_error = getattr(ct, "get_last_error", None)
        if os.name != "nt" or factory is None or last_error is None:
            raise ResourceError("Windows Job Object ownership is unavailable")
        try:
            self.api: Any = factory("kernel32", use_last_error=True)
        except OSError as error:
            raise ResourceError(
                "Windows Job Object ownership is unavailable"
            ) from error
        self.last_error = cast(Callable[[], int], last_error)
        pointer = ct.c_void_p
        signatures = {
            "GetCurrentProcess": ([], _HANDLE),
            "GetCurrentProcessId": ([], _DWORD),
            "GetProcessTimes": (
                [_HANDLE, pointer, pointer, pointer, pointer],
                ct.c_int,
            ),
            "OpenProcess": ([_DWORD, ct.c_int, _DWORD], _HANDLE),
            "CreateJobObjectW": ([pointer, ct.c_wchar_p], _HANDLE),
            "SetInformationJobObject": ([_HANDLE, ct.c_int, pointer, _DWORD], ct.c_int),
            "QueryInformationJobObject": (
                [_HANDLE, ct.c_int, pointer, _DWORD, pointer],
                ct.c_int,
            ),
            "TerminateJobObject": ([_HANDLE, ct.c_uint32], ct.c_int),
            "GetStdHandle": ([_DWORD], _HANDLE),
            "DuplicateHandle": (
                [_HANDLE, _HANDLE, _HANDLE, pointer, _DWORD, ct.c_int, _DWORD],
                ct.c_int,
            ),
            "CloseHandle": ([_HANDLE], ct.c_int),
            "InitializeProcThreadAttributeList": (
                [pointer, _DWORD, _DWORD, pointer],
                ct.c_int,
            ),
            "UpdateProcThreadAttribute": (
                [pointer, _DWORD, _SIZE, pointer, _SIZE, pointer, pointer],
                ct.c_int,
            ),
            "DeleteProcThreadAttributeList": ([pointer], None),
            "CreateProcessW": (
                [
                    ct.c_wchar_p,
                    ct.c_wchar_p,
                    pointer,
                    pointer,
                    ct.c_int,
                    _DWORD,
                    pointer,
                    ct.c_wchar_p,
                    pointer,
                    pointer,
                ],
                ct.c_int,
            ),
            "WaitForSingleObject": ([_HANDLE, _DWORD], _DWORD),
            "WaitForMultipleObjects": ([_DWORD, pointer, ct.c_int, _DWORD], _DWORD),
            "GetExitCodeProcess": ([_HANDLE, pointer], ct.c_int),
            "ExitProcess": ([_DWORD], None),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name, None)
            if function is None:
                raise ResourceError("Windows Job Object ownership is unavailable")
            function.argtypes, function.restype = arguments, result

    def require(self, success: object) -> None:
        """Raise bounded trusted metadata, never a caller-controlled exception."""
        if not success:
            raise ResourceError(
                f"Windows worker ownership failed (system error {self.last_error()})"
            )

    def creation(self, handle: int) -> int:
        """Read stable creation identity from an already opened process object."""
        created, exited, kernel, user = (_FileTime() for _ in range(4))
        self.require(
            self.api.GetProcessTimes(
                handle,
                ct.byref(created),
                ct.byref(exited),
                ct.byref(kernel),
                ct.byref(user),
            )
        )
        return (int(created.high) << 32) | int(created.low)


def parent_identity() -> tuple[int, int]:
    """Capture the caller identity before launching its Windows supervisor."""
    native = _Kernel()
    return int(native.api.GetCurrentProcessId()), native.creation(
        native.api.GetCurrentProcess()
    )


@contextmanager
def _attributes(
    native: _Kernel, job: int, handles: list[int]
) -> Iterator[ct.Array[ct.c_char]]:
    """Keep both public attribute arrays alive until creation has completed."""
    size = _SIZE()
    native.api.InitializeProcThreadAttributeList(None, 2, 0, ct.byref(size))
    if not 0 < size.value <= 65536:
        raise ResourceError("Windows process attributes are unavailable")
    buffer = ct.create_string_buffer(size.value)
    native.require(
        native.api.InitializeProcThreadAttributeList(buffer, 2, 0, ct.byref(size))
    )
    try:
        jobs = (_HANDLE * 1)(job)
        inherited = (_HANDLE * len(handles))(*handles)
        for key, values in ((_JOB_LIST, jobs), (_HANDLE_LIST, inherited)):
            native.require(
                native.api.UpdateProcThreadAttribute(
                    buffer, 0, key, values, ct.sizeof(values), None, None
                )
            )
        yield buffer
    finally:
        native.api.DeleteProcThreadAttributeList(buffer)


def _command(command: Sequence[str]) -> str:
    """Bound command text before maintained Windows argument quoting."""
    if not command or len(command) > 32767:
        raise InputError("Windows ownership requires a finite command")
    size = 0
    for value in command:
        if not isinstance(value, str) or "\0" in value:
            raise InputError("Windows command arguments must be text without NUL")
        size += len(value)
        if size > 32766:
            raise InputError("Windows command exceeds its native text bound")
    text = subprocess.list2cmdline(command)
    if len(text.encode("utf-16-le")) // 2 > 32766:
        raise InputError("Windows command exceeds its native text bound")
    return text


def _supervise(parent_pid: int, created: int, command: Sequence[str]) -> int:
    """Launch only after caller identity, job limits and stdio are qualified."""
    text, native = _command(command), _Kernel()
    handles: list[int] = []
    job = child = 0
    failure: BaseException | None = None

    def owned(handle: object) -> int:
        """Record each successfully acquired handle before another native call."""
        native.require(handle and handle != _INVALID_HANDLE)
        value = cast(int, handle)
        handles.append(value)
        return value

    def close(handle: int) -> None:
        """Release one known handle without ever targeting a PID by name."""
        native.require(native.api.CloseHandle(handle))
        handles.remove(handle)

    try:
        parent = owned(
            native.api.OpenProcess(_SYNCHRONIZE | _QUERY_LIMITED, False, parent_pid)
        )
        if (
            native.creation(parent) != created
            or native.api.WaitForSingleObject(parent, 0) != _WAIT_TIMEOUT
        ):
            raise ResourceError("Windows worker caller is no longer the captured owner")
        job = owned(native.api.CreateJobObjectW(None, None))
        limits = _ExtendedLimits()
        limits.basic.flags = _KILL_ON_CLOSE
        native.require(
            native.api.SetInformationJobObject(
                job, 9, ct.byref(limits), ct.sizeof(limits)
            )
        )
        current = native.api.GetCurrentProcess()
        inherited = []
        for identifier in (-10, -11, -12):
            standard = native.api.GetStdHandle(identifier & 0xFFFFFFFF)
            native.require(standard and standard != _INVALID_HANDLE)
            duplicate = _HANDLE()
            native.require(
                native.api.DuplicateHandle(
                    current, standard, current, ct.byref(duplicate), 0, True, 2
                )
            )
            inherited.append(owned(duplicate.value))
        startup, process = _StartupEx(), _ProcessInfo()
        startup.startup.cb = ct.sizeof(startup)
        startup.startup.flags = 0x100
        startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = (
            inherited
        )
        with _attributes(native, job, inherited) as attributes:
            startup.attributes = ct.cast(attributes, ct.c_void_p)
            native.require(
                native.api.CreateProcessW(
                    None,
                    ct.create_unicode_buffer(text),
                    None,
                    None,
                    True,
                    0x00080400,
                    None,
                    None,
                    ct.byref(startup),
                    ct.byref(process),
                )
            )
            child = owned(process.process)
            thread = owned(process.thread)
        close(thread)
        for handle in inherited:
            close(handle)
        waiting = (_HANDLE * 2)(parent, child)
        ready = native.api.WaitForMultipleObjects(2, waiting, False, _INFINITE)
        if ready == 0:
            raise ResourceError("Windows worker caller exited before completion")
        if ready != 1:
            raise ResourceError("Windows worker ownership wait failed")
        code = _DWORD()
        native.require(native.api.GetExitCodeProcess(child, ct.byref(code)))
        return code.value
    except BaseException as error:
        failure = error
        raise
    finally:
        cleanup_failed = False
        if job and child:
            if not native.api.TerminateJobObject(job, 1):
                cleanup_failed = True
            if native.api.WaitForSingleObject(child, 600) != 0:
                cleanup_failed = True
            if native.api.CloseHandle(child):
                handles.remove(child)
            else:
                cleanup_failed = True
            deadline = time.monotonic() + 0.6
            while True:
                accounting = _Accounting()
                if not native.api.QueryInformationJobObject(
                    job, 1, ct.byref(accounting), ct.sizeof(accounting), None
                ):
                    cleanup_failed = True
                    break
                if accounting.active == 0:
                    break
                if time.monotonic() >= deadline:
                    cleanup_failed = True
                    break
                time.sleep(0.01)
        for handle in reversed(handles):
            if not native.api.CloseHandle(handle):
                cleanup_failed = True
        if cleanup_failed:
            if failure is None:
                raise ResourceError("Windows owned worker cleanup did not complete")
            failure.add_note("Windows owned worker cleanup also failed")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the private supervisor command without changing worker protocol data."""
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) < 4 or arguments[2] != "--":
            raise InputError("Invalid Windows ownership bootstrap")
        for value, maximum in zip(arguments[:2], (10, 20), strict=True):
            if not value.isascii() or not value.isdecimal() or len(value) > maximum:
                raise InputError("Invalid Windows caller identity")
        pid, created = int(arguments[0]), int(arguments[1])
        if not 0 < pid <= 0xFFFFFFFF or not 0 < created <= 0xFFFFFFFFFFFFFFFF:
            raise InputError("Invalid Windows caller identity")
        result = _supervise(pid, created, arguments[3:])
    except (InputError, OSError, ValueError):
        sys.stderr.write("Windows owned worker could not complete\n")
        return 1
    # ExitProcess preserves the child's full DWORD status, including NTSTATUS.
    _Kernel().api.ExitProcess(result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())

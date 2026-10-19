"""Real Windows workers cannot outlive their owned supervisor or caller."""

import ctypes as ct
import gc
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from bijux_ontology.processes.windows import parent_identity

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Requires actual Windows Job Objects"
)


def _kernel() -> Any:
    """Probe real fixture process handles, independently of the supervisor code."""
    factory = getattr(ct, "WinDLL", None)
    assert factory is not None
    kernel = factory("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ct.c_uint32, ct.c_int, ct.c_uint32]
    kernel.OpenProcess.restype = ct.c_void_p
    kernel.WaitForSingleObject.argtypes = [ct.c_void_p, ct.c_uint32]
    kernel.WaitForSingleObject.restype = ct.c_uint32
    kernel.TerminateProcess.argtypes = [ct.c_void_p, ct.c_uint32]
    kernel.TerminateProcess.restype = ct.c_int
    kernel.CloseHandle.argtypes = [ct.c_void_p]
    kernel.CloseHandle.restype = ct.c_int
    kernel.GetCurrentProcess.restype = ct.c_void_p
    kernel.GetProcessHandleCount.argtypes = [ct.c_void_p, ct.POINTER(ct.c_uint32)]
    kernel.GetProcessHandleCount.restype = ct.c_int
    return kernel


def _handles(kernel: Any) -> int:
    """Measure only this test process's real native handle count."""
    count = ct.c_uint32()
    assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ct.byref(count))
    return count.value


def _wait_file(path: Path) -> dict[str, int]:
    """Wait finitely for the actual nested fixture to publish all identities."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            value: object = json.loads(path.read_text())
            assert isinstance(value, dict)
            assert all(type(k) is str and type(v) is int for k, v in value.items())
            return cast(dict[str, int], value)
        except (OSError, json.JSONDecodeError):
            time.sleep(0.01)
    raise AssertionError("Windows nested fixture did not start")


@pytest.mark.parametrize("stop", ("supervisor", "caller"))
def test_real_windows_nested_workers_and_recovery(tmp_path: Path, stop: str) -> None:
    """Use stable native handles to prove descendants die and unrelated work lives."""
    kernel = _kernel()
    pidfile, helperfile = tmp_path / "nested.json", tmp_path / "supervisor.json"
    grandchild = "import time;time.sleep(60)"
    child = (
        "import os,json,subprocess,sys,time;from pathlib import Path;"
        f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]);"
        f"Path({str(pidfile)!r}).write_text(json.dumps({{'leader':os.getppid(),'child':os.getpid(),'grandchild':p.pid}}));"
        "time.sleep(60)"
    )
    leader = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(60)"
    )
    controller = (
        "import json,subprocess,sys,time;from pathlib import Path;"
        "from bijux_ontology.processes.windows import parent_identity;"
        "pid,created=parent_identity();"
        "command=[sys.executable,'-I','-B','-m','bijux_ontology.processes.windows',str(pid),str(created),'--',sys.executable,'-c',"
        f"{leader!r}];p=subprocess.Popen(command);"
        f"Path({str(helperfile)!r}).write_text(json.dumps({{'supervisor':p.pid}}));"
        "time.sleep(60)"
    )
    baseline = _handles(kernel)
    sentinel = subprocess.Popen([sys.executable, "-c", grandchild])
    caller = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", controller],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    opened: list[int] = []
    try:
        identities = _wait_file(pidfile) | _wait_file(helperfile)
        for pid in identities.values():
            handle = kernel.OpenProcess(0x00100001, False, pid)
            assert handle
            opened.append(handle)
        if stop == "caller":
            caller.kill()
            caller.wait(timeout=5)
        else:
            assert kernel.TerminateProcess(opened[-1], 1)
        for handle in opened:
            assert kernel.WaitForSingleObject(handle, 5000) == 0
        assert sentinel.poll() is None
        pid, created = parent_identity()
        for executable in (sys.executable, Path(sys.executable).name, sys.executable):
            recovered = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "bijux_ontology.processes.windows",
                    str(pid),
                    str(created),
                    "--",
                    executable,
                    "-c",
                    "print('recovered')",
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
            assert recovered.returncode == 0 and recovered.stdout == b"recovered\r\n"
        marker = tmp_path / "must-not-run"
        refused = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "bijux_ontology.processes.windows",
                str(pid),
                str(created + 1),
                "--",
                sys.executable,
                "-c",
                f"from pathlib import Path;Path({str(marker)!r}).touch()",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert refused.returncode != 0 and not marker.exists()
    finally:
        for handle in opened:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
            assert kernel.CloseHandle(handle)
        for process in (caller, sentinel):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        del caller, sentinel, process
        gc.collect()
    assert _handles(kernel) <= baseline + 2

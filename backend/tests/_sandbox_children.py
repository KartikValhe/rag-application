"""
Test-only child module for tests/test_sandbox.py and the worker tests.

Started by the real sandbox (`python -P -m tests._sandbox_children <mode>`), so it goes through
exactly the production spawn path and the shared child bootstrap (app.core.sandbox_child.run):
parent-death signal, resource limits, framed stdin, stdout guard and the error marker.
"""

import os
import subprocess
import sys
import time
from typing import Any

from app.core.sandbox_child import run


def _handle(request: dict[str, Any], data: bytes) -> dict[str, Any]:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "env":
        return {
            "success": True,
            "env": dict(os.environ),
            "cwd": os.getcwd(),
            "listing": os.listdir("."),
            "config_loaded": "app.core.config" in sys.modules,
        }
    if mode == "echo":
        return {"success": True, "request": request, "n": len(data), "head": data[:16].decode("latin-1")}
    if mode == "sleep":
        with open(request["pid_file"], "w") as fh:
            fh.write(str(os.getpid()))
        time.sleep(60)
    if mode == "tree":
        grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open(request["pid_file"], "w") as fh:
            fh.write(f"{os.getpid()},{grandchild.pid}")
        time.sleep(60)
    if mode == "huge":
        return {"success": True, "blob": "x" * int(request["size"])}
    if mode == "noisy":
        # Library prints must never corrupt the JSON on stdout
        print("this goes to stderr, not stdout")
        return {"success": True}
    if mode == "boom":
        sys.stderr.write("secret path /home/private/syllabus.pdf CANARY-STDERR\n")
        raise ZeroDivisionError("CANARY-EXCEPTION-TEXT")
    if mode == "rlimit":
        import resource

        return {
            "success": True,
            "as": resource.getrlimit(resource.RLIMIT_AS)[0],
            "cpu": resource.getrlimit(resource.RLIMIT_CPU)[0],
        }
    if mode == "pdeathsig":
        import ctypes
        import signal

        libc = ctypes.CDLL(None, use_errno=True)
        value = ctypes.c_int(0)
        libc.prctl(2, ctypes.byref(value), 0, 0, 0)  # PR_GET_PDEATHSIG
        return {"success": True, "pdeathsig": value.value, "sigkill": int(signal.SIGKILL)}
    return {"success": False, "error_code": "bad_mode"}


if __name__ == "__main__":
    run(_handle)

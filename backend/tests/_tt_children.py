"""
Test-only child process used by tests/test_timetable_hardening.py.

It is started with `python -m tests._tt_children <mode>` by the real runner, so it goes
through exactly the same spawn path as production. The mode comes in through the
`format_type` argument and the stdin bytes carry mode specific input.
"""

import json
import os
import subprocess
import sys
import time


def _emit(obj: object) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def main() -> None:
    mode = sys.argv[1]
    data = sys.stdin.buffer.read()

    if mode == "env":
        _emit(
            {
                "success": True,
                "records": [{"env": dict(os.environ), "cwd": os.getcwd(), "listing": os.listdir(".")}],
            }
        )
    elif mode == "sleep":
        # stdin carries the path of a file that receives our pid, then we hang
        with open(data.decode(), "w") as fh:
            fh.write(str(os.getpid()))
        time.sleep(60)
    elif mode == "tree":
        grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open(data.decode(), "w") as fh:
            fh.write(f"{os.getpid()},{grandchild.pid}")
        time.sleep(60)
    elif mode == "big":
        _emit({"success": True, "records": [{"raw_cell": "x" * 500_000}]})
    elif mode == "huge":
        _emit({"success": True, "records": [{"raw_cell": "x" * 6_000_000}]})
    elif mode == "fail":
        _emit({"success": False, "error_code": "parse_failure", "error_msg": "boom"})
    elif mode == "crash":
        raise SystemExit(3)
    elif mode == "boom":
        sys.stderr.write("secret path C:\\private\\file.xlsx\nchild_error_type=ZeroDivisionError\n")
        sys.stderr.flush()
        raise SystemExit(7)
    elif mode == "echo_len":
        _emit({"success": True, "records": [{"n": len(data)}]})
    elif mode == "rlimit":
        import resource

        from app.services.timetable_parse_child import apply_resource_limits

        apply_resource_limits()
        _emit(
            {
                "success": True,
                "records": [
                    {
                        "as": resource.getrlimit(resource.RLIMIT_AS)[0],
                        "cpu": resource.getrlimit(resource.RLIMIT_CPU)[0],
                    }
                ],
            }
        )
    else:
        _emit({"success": False, "error_code": "bad_mode", "error_msg": mode})


if __name__ == "__main__":
    main()

"""Keep the two local Flask sites alive without ever opening a console window.

Started once per logon by the scheduled task ``DailyReportWebGuard`` through
``pyw.exe``, which has no console subsystem at all.  Because of that:

* no window pops up every minute (unlike a per-minute ``powershell.exe`` task);
* the Flask children are spawned with ``DETACHED_PROCESS`` so they are not
  attached to any console and cannot be killed by a stray control event
  (the old ``run_web.cmd`` path died with 0xC000013A = STATUS_CONTROL_C_EXIT).

Usage::

    pyw -3.12 scripts/web_supervisor.py            # resident guard
    py -3.12 scripts/web_supervisor.py --once      # single repair pass (CD)

A second instance exits immediately thanks to a loopback socket lock, so it is
safe to start the task again at any time.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"
LOG_PATH = PROJECT_ROOT / ".webguard.log"
OUTPUT_PATH = PROJECT_ROOT / ".web.out.log"

LOCK_PORT = 5099          # loopback-only singleton lock, also used by --once
CHECK_SECONDS = 30        # how often the resident guard looks at the ports
START_GRACE_SECONDS = 45  # a freshly spawned app gets this long to bind
LOG_MAX_BYTES = 256 * 1024

# (port, script) started with the current interpreter, cwd = web/
SERVICES = ((5000, "app.py"), (5001, "publisher_app.py"))

# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: no console, survives its parent.
DETACHED_FLAGS = 0x00000008 | 0x00000200


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def trim(path: Path, keep_lines: int = 200) -> None:
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-keep_lines:]
            path.write_text("\n".join(tail) + "\n", encoding="utf-8")
    except OSError:
        pass


def log(message: str) -> None:
    trim(LOG_PATH)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{now()} {message}\n")
    except OSError:
        pass


def port_is_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except OSError:
        return False


def acquire_singleton() -> socket.socket | None:
    """Bind the loopback lock port; return None when another guard holds it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


def spawn(script: str, port: int) -> subprocess.Popen:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = OUTPUT_PATH.open("a", encoding="utf-8", errors="replace")
    handle.write(f"\n===== {now()} starting {script} (expected port {port}) =====\n")
    handle.flush()

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["START_WEB_NO_WATCHDOG"] = "1"
    env["START_WEB_NO_BROWSER"] = "1"

    process = subprocess.Popen(
        [sys.executable, script],
        cwd=str(WEB_DIR),
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=DETACHED_FLAGS,
        env=env,
        close_fds=True,
    )
    handle.close()
    log(f"started {script} on port {port} (pid {process.pid})")
    return process


def check_once(children: dict[int, tuple[subprocess.Popen, float]]) -> bool:
    """Bring both ports up if needed; return True when both are listening."""
    for port, script in SERVICES:
        if port_is_up(port):
            children.pop(port, None)
            continue

        child, started_at = children.get(port, (None, 0.0))
        if child is not None and child.poll() is None:
            if time.monotonic() - started_at < START_GRACE_SECONDS:
                continue  # still booting, be patient
            log(f"port {port} still down after {START_GRACE_SECONDS}s, killing pid {child.pid}")
            try:
                child.kill()
            except OSError:
                pass

        if child is not None and child.poll() is not None:
            log(f"{script} exited (code {child.returncode}) - see {OUTPUT_PATH.name}")

        log(f"port {port} down - restarting {script}")
        children[port] = (spawn(script, port), time.monotonic())

    return all(port_is_up(port) for port, _ in SERVICES)


def main(argv: list[str]) -> int:
    once = "--once" in argv

    lock = acquire_singleton()
    if lock is None:
        if not once:
            log(f"another supervisor already holds port {LOCK_PORT}, exiting")
        return 0

    log(f"supervisor up (pid {os.getpid()}, interpreter {sys.executable})")

    children: dict[int, tuple[subprocess.Popen, float]] = {}
    try:
        if once:
            healthy = check_once(children)
            log("single pass finished: " + ("both ports up" if healthy else "ports still down"))
            return 0 if healthy else 1

        while True:
            try:
                check_once(children)
            except Exception as error:  # never let one bad pass kill the guard
                log(f"check failed: {error!r}")
            time.sleep(CHECK_SECONDS)
    except KeyboardInterrupt:
        log("supervisor stopped by user")
        return 0
    finally:
        try:
            lock.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

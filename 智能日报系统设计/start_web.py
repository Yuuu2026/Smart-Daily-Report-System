"""Start the archive and publisher sites together from the project root."""

import importlib.util
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


PROJECT_ROOT = Path(__file__).resolve().parent
WEB_DIR = PROJECT_ROOT / "web"
OPENER = build_opener(ProxyHandler({}))
SERVICES = (
    ("归档网站", "http://127.0.0.1:5000/archive", "app.py", "成员信息归档"),
    ("发布网站", "http://127.0.0.1:5001/login", "publisher_app.py", "登录后发布进展"),
)


def is_ready(url, expected_marker):
    try:
        request = Request(url, headers={"Cache-Control": "no-cache"})
        with OPENER.open(request, timeout=1) as response:
            body = response.read().decode("utf-8", errors="replace")
            return 200 <= response.status < 400 and expected_marker in body
    except (OSError, URLError):
        return False


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main():
    if importlib.util.find_spec("flask") is None:
        print("当前 Python 环境没有安装 Flask。请在项目目录运行：")
        print("  python -m pip install -r web/requirements.txt")
        return 1

    running = {}
    for name, url, script, marker in SERVICES:
        if is_ready(url, marker):
            print(f"{name}已运行：{url}")
            continue
        try:
            running[name] = subprocess.Popen(
                [sys.executable, script],
                cwd=WEB_DIR,
            )
        except OSError as error:
            print(f"启动{name}失败：{error}")
            for process in running.values():
                stop_process(process)
            return 1

    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            failed = [name for name, process in running.items() if process.poll() is not None]
            if failed:
                print(f"{', '.join(failed)}启动后退出；请检查上方错误信息。")
                return 1
            if all(is_ready(url, marker) for _, url, _, marker in SERVICES):
                if running:
                    print("两个网站都已就绪。保持此终端打开；按 Ctrl+C 停止本次启动的服务。")
                else:
                    print("两个网站已在其他终端运行；本启动器不会重复启动它们。")
                print(f"归档：{SERVICES[0][1]}")
                print(f"发布：{SERVICES[1][1]}")
                webbrowser.open("http://127.0.0.1:5000/archive")
                if not running:
                    return 0
                while all(is_ready(url, marker) for _, url, _, marker in SERVICES):
                    for name, process in running.items():
                        if process.poll() is not None:
                            print(f"{name}已停止。")
                            return 1
                    time.sleep(2)
                print("检测到网站连接中断，正在停止本次启动的服务。")
                return 1
            time.sleep(0.5)
        print("等待网站启动超时；请检查 Flask 启动错误。")
        return 1
    except KeyboardInterrupt:
        print("正在停止网站服务……")
        return 0
    finally:
        for process in running.values():
            stop_process(process)


if __name__ == "__main__":
    raise SystemExit(main())

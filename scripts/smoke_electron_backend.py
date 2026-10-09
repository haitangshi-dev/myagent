"""Electron 主进程后端路径冒烟测试。

模拟 web/electron/main.cjs 的关键行为：
  1. 以 spawn 方式拉起 python main.py（uvicorn :8000）
  2. 轮询 /api/health 直到 200
  3. 确认根路径 / 返回前端 HTML（即 web/dist 被托管）
  4. 清理后端进程树

不依赖显示器，仅验证「Electron 壳能否在本机正常驱动后端」。
"""
from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8000
BASE = f"http://127.0.0.1:{PORT}"


def find_python() -> str | None:
    for c in ("python", "python3", "py"):
        try:
            r = subprocess.run([c, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return c
        except Exception:
            pass
    return None


def wait_health(timeout=30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def kill_tree(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
    else:
        p.terminate()


def main() -> int:
    py = find_python()
    if not py:
        print("[FAIL] 未找到 python")
        return 1
    print(f"[ok] python = {py}")

    entry = ROOT / "main.py"
    if not entry.exists():
        print(f"[FAIL] 找不到后端入口: {entry}")
        return 1

    print(f"[..] 启动后端: {py} main.py --host 127.0.0.1 --port {PORT}")
    proc = subprocess.Popen(
        [py, "main.py", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT),
    )
    try:
        if not wait_health():
            print("[FAIL] /api/health 30s 内未就绪")
            print(proc.stdout)
            return 1
        print("[ok] /api/health 就绪")

        # 根路径应返回 HTML（web/dist 被 FastAPI 托管）
        with urllib.request.urlopen(BASE + "/", timeout=5) as r:
            body = r.read().decode("utf-8", "ignore")
        if r.status == 200 and "<html" in body.lower():
            print(f"[ok] GET / 返回 HTML（{len(body)} 字节）")
        else:
            print(f"[FAIL] GET / 状态={r.status}，未返回 HTML")
            return 1
    finally:
        kill_tree(proc)
        # 给一点时间退出
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    print("[ok] 后端进程树已清理")
    print("\nSMOKE_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

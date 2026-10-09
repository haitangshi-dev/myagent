"""MY_AGENT — 一体化启动器。

一条命令拉起后端，并按需打开 Web 前端（React，构建产物位于 web/dist，由后端在 / 直接托管）。

用法：
    python start.py                  # 后端 + 浏览器打开 Web 前端（默认）
    python start.py --mode backend   # 仅后端（手动打开 http://localhost:8000）
    python start.py --port 8080      # 自定义端口
    python start.py --host 127.0.0.1 # 自定义监听地址

说明：
    - 前端为 React + Vite 工程（web/），`npm run build` 后产物在 web/dist，
      由 server/api.py 的 StaticFiles 在 / 路径托管。
    - gui 模式会先拉起后端并等待 /api/health 就绪，再用默认浏览器打开 Web UI。

停止：终端按 Ctrl+C 即可，后端会一并退出；
      即便被任务管理器强杀，Windows Job Object 也会回收子进程，不留孤儿。
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent

# Windows Job Object 句柄（非 Windows 或创建失败时为 None）
_job = None

# 停止标志：信号处理器只置位，不抛异常（Windows 信号上下文里抛异常不稳）
_stop = threading.Event()


def _setup_job() -> None:
    """Windows：把当前进程放进 Job Object，KILL_ON_JOB_CLOSE 确保子进程随父退出被回收。"""
    global _job
    if sys.platform != "win32":
        return
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("dummy", ctypes.c_int64 * 6)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_void_p),
                ("MaximumWorkingSetSize", ctypes.c_void_p),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_void_p),
                ("JobMemoryLimit", ctypes.c_void_p),
                ("PeakProcessMemoryUsed", ctypes.c_void_p),
                ("PeakJobMemoryUsed", ctypes.c_void_p),
            ]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        info.BasicLimitInformation.LimitFlags = 0x2000
        if not k32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            return
        if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            # 父进程可能已在别的 Job 中（如被某启动器嵌套），忽略即可
            return
        _job = job
    except Exception:
        _job = None


def wait_for_health(url: str, timeout: float = 30.0) -> bool:
    """轮询后端健康检查，直到返回 200、被要求停止或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline and not _stop.is_set():
        try:
            with urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def terminate(proc) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


_children = []  # 由 main() 填充，供信号处理器清理


def _signal_handler(signum, frame) -> None:
    """SIGINT / SIGTERM：仅置停止标志，由主循环统一清理退出（不抛异常）。"""
    _stop.set()


def main() -> None:
    ap = argparse.ArgumentParser(description="MY_AGENT 启动器")
    ap.add_argument(
        "--mode",
        choices=["backend", "gui"],
        default="gui",
        help="启动模式：gui=后端+浏览器打开 Web 前端（默认）, backend=仅后端",
    )
    ap.add_argument("--host", default=None, help="监听地址（默认用 settings.yaml）")
    ap.add_argument("--port", type=int, default=None, help="监听端口（默认用 settings.yaml）")
    ap.add_argument("--base-url", default=None, help="客户端连接后端地址（默认自动推导）")
    args = ap.parse_args()

    _setup_job()
    _stop.clear()
    _children.clear()
    # SIGTERM（如外部 `kill`）也按 Ctrl+C 处理，确保子进程一并退出
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # 拼接后端启动命令（参数透传给 main.py，由其读 settings.yaml 取默认）
    cmd = [sys.executable, str(ROOT / "main.py")]
    if args.host:
        cmd += ["--host", args.host]
    if args.port:
        cmd += ["--port", str(args.port)]

    # 客户端连接地址：0.0.0.0 / :: 时改用 localhost（解析到 ::1，走 IPv6 回环）
    connect_host = "localhost" if (args.host in (None, "", "0.0.0.0", "::")) else args.host
    port = args.port or 8000
    base_url = args.base_url or f"http://{connect_host}:{port}"

    backend = None
    client = None
    try:
        print(f"[start] 启动后端: {' '.join(cmd)}")
        backend = subprocess.Popen(cmd)
        _children.append(backend)

        health = f"{base_url}/api/health"
        print(f"[start] 等待后端就绪 ({health}) ...")
        if not wait_for_health(health):
            if _stop.is_set():
                print("[start] 启动过程中被中断。")
            else:
                print("[start] ⚠️ 后端健康检查超时，请查看上方日志。")
            terminate(backend)
            return

        print(f"[start] ✅ 后端已就绪: {base_url}")

        if args.mode == "gui":
            print(f"[start] 打开 Web 前端: {base_url}")
            webbrowser.open(base_url)

        print("[start] 运行中，按 Ctrl+C 停止。")
        # 阻塞：任一进程退出或收到停止信号则退出
        while not _stop.is_set():
            if backend.poll() is not None:
                print("[start] 后端已退出。")
                break
            if client is not None and client.poll() is not None:
                print("[start] 客户端已退出。")
                break
            time.sleep(0.5)
    finally:
        if _stop.is_set():
            print("\n[start] 收到停止信号，正在关闭子进程 ...")
        terminate(backend)
        terminate(client)
        print("[start] 已停止。")


if __name__ == "__main__":
    main()

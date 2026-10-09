"""MY_AGENT — 启动入口。

用法：
    python main.py                # 读取 config/settings.yaml，启动 FastAPI
    python main.py --port 8080    # 自定义端口

说明：本项目不提供推理后端，也不内置任何服务商。使用者在前端「设置 → 接口」
填入任意 OpenAI 兼容接口（地址 / 密钥 / 模型名）即可使用。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 让项目根目录在 sys.path 中（无论从哪启动都能 import 包）
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import load_settings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("myagent")


def main() -> None:
    parser = argparse.ArgumentParser(description="MY_AGENT 服务端")
    parser.add_argument("--host", default=None, help="监听地址")
    parser.add_argument("--port", type=int, default=None, help="监听端口")
    args = parser.parse_args()

    settings = load_settings()
    server_cfg = settings.get("server", {})
    host = args.host or server_cfg.get("host", "0.0.0.0")
    port = args.port or int(server_cfg.get("port", 8000))

    import uvicorn

    # PC-only：默认绑 127.0.0.1 回环（见 config/settings.yaml）。
    # 仅本机可达，不暴露到局域网/公网；无需 IPv6 双栈，规避 Windows 上 :: 的 IPv6-only 陷阱。
    logger.info("启动 MY_AGENT → http://%s:%s", host, port)
    uvicorn.run(
        "server.api:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()

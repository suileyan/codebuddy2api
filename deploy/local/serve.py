"""本地原生启动 workbuddy2api 管理后台（无需 Docker）。

用法：
    .venv/Scripts/python.exe deploy/local/serve.py

配置从同目录的 .env 读取（简单 KEY=VALUE 解析，已存在的环境变量优先）。
默认只监听 127.0.0.1，避免把管理后台直接暴露到局域网。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    load_env(ENV_FILE)

    missing = [k for k in ("ADMIN_KEY", "CODEBUDDY_AUTH_DIR") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"缺少必需配置：{', '.join(missing)}（请在 deploy/local/.env 中填写）")

    import uvicorn

    from admin.logstore import setup_logging
    from admin.server import create_app

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8787"))
    app = create_app()
    setup_logging(app.state.logstore.dir)
    print(f"workbuddy2api 管理后台：http://{host}:{port}/admin/")
    print(f"日志目录：{app.state.logstore.dir}")
    # log_config=None：不要让 uvicorn 覆盖日志配置，让它的访问/错误日志
    # 顺着 root logger 一起写进管理目录下的 logs/service.log。
    uvicorn.run(app, host=host, port=port, log_level="info", log_config=None)


if __name__ == "__main__":
    main()

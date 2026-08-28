"""StarRadar Windows 桌面启动入口。

通过 pywebview 将已有本地 Web 服务放进原生窗口；不暴露公网端口，也不需要
Electron、Node.js 或额外数据库服务。
"""
from __future__ import annotations

import logging
import socket
import sys
import threading

from config import DATA_DIR, ensure_dirs, settings


def _pick_port(preferred: int = 8970) -> int:
    """优先沿用 8970；若已有实例占用，则安全地选择一个本机临时端口。"""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                # 已有 StarRadar（或其他本地服务）占用 8970 时，继续让系统
                # 分配临时端口；不要把首次 bind 失败直接显示成崩溃弹窗。
                continue
            return int(probe.getsockname()[1])
    raise RuntimeError("无法分配本地服务端口")


def _configure_logging() -> None:
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_dir / "desktop.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )


def main() -> None:
    import webview
    from src.web.server import create_server

    ensure_dirs()
    _configure_logging()
    port = _pick_port()
    httpd = create_server(port=port)
    worker = threading.Thread(target=httpd.serve_forever, name="StarRadarServer", daemon=True)
    worker.start()

    if port != 8970:
        logging.warning("8970 端口已被占用，桌面版改用端口 %s", port)
    try:
        webview.create_window(
            "航标 Beacon · 开源项目发现",
            f"http://127.0.0.1:{port}/",
            width=1440,
            height=920,
            min_size=(980, 680),
            background_color="#07101e",
        )
        webview.start(debug=settings.debug)
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)


def run_personal_worker() -> None:
    """供打包版后台子进程调用：只刷新个人数据，不创建桌面窗口。"""
    ensure_dirs()
    from src.personal.pipeline import run_personal_pipeline

    run_personal_pipeline()


if __name__ == "__main__":
    if "--personal-worker" in sys.argv:
        run_personal_worker()
    else:
        main()

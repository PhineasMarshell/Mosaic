"""Replit 入口 — 启动 Mosaic Market Intelligence Agent。"""

import os
import uvicorn

from app.main import app

if __name__ == "__main__":
    # Replit 自动分配 PORT；本地默认 8000
    port = int(os.environ.get("PORT", "8000"))
    # Replit 要求监听所有网络接口
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

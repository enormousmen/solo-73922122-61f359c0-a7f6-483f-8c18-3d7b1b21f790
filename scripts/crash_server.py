#!/usr/bin/env python3
"""可自毁的 uvicorn 服务（测试E 专用，仅标准库 + 项目依赖）。

本机 seccomp 过滤了 kill 系调用，外部无法给本进程发信号；
因此用标记文件让进程自身 os._exit() 硬退出（等价 SIGKILL，跳过全部 finally/atexit）：

  环境变量 CRASH_MODE：
    hang  : 观察到 tiles/.reset-<kind>.json 与 tiles/.reset-trash-* 同时出现即硬退出
            （精确模拟“重置改名完成、live 空目录尚未建好时进程退出”）
    其它  : 看到 DATA_DIR/.crash-now 即硬退出（测试用来正常关停实例）
"""
import os
import sys
import threading
import time
from pathlib import Path

DATA = Path(os.environ["DATA_DIR"])
TILES = DATA / "tiles"
MODE = os.environ.get("CRASH_MODE", "normal")


def watcher() -> None:
    t0 = time.time()
    while time.time() - t0 < 120:
        if MODE == "hang":
            if list(TILES.glob(".reset-*.json")) and list(TILES.glob(".reset-trash-*")):
                sys.stderr.write("[crash_server] 重置挂住点达成，硬退出\n")
                sys.stderr.flush()
                os._exit(99)
        elif (DATA / ".crash-now").exists():
            sys.stderr.write("[crash_server] 收到关停标记，硬退出\n")
            sys.stderr.flush()
            os._exit(97)
        time.sleep(0.05)
    os._exit(98)


if __name__ == "__main__":
    sys.path.append("/workspace")
    sys.path.append("/tmp/pylibs")
    os.chdir("/workspace")
    threading.Thread(target=watcher, daemon=True).start()
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("TEST_PORT", "8077")),
        log_level="warning",
    )

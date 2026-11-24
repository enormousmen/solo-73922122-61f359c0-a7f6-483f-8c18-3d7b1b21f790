#!/usr/bin/env python3
"""测试E 编排：模拟“重置途中服务进程退出”后的重启恢复（仅标准库）。

本机 seccomp 过滤 kill 系调用，无法从外部杀进程；配合 scripts/crash_server.py：
服务子进程观察到标记后自身 os._exit() 硬退出（等价 SIGKILL，跳过所有清理）。

覆盖：
  A. 真实重置在提交点（journal+trash 就位）后硬退出 -> 重启恢复为“全部清空”；
  B. 手工构造 journal+trash、live 目录尚不存在的更早崩溃现场 -> 重启同样全部清空；
  C. 无未完成事务正常重启 -> 瓦片全部保留；
  D. 恢复后重新切片从零出满 85 张，另一类任务瓦片全程可取。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DATA = Path("/tmp/tiler-crash-data2")
TILES = DATA / "tiles"
HANG = TILES / ".fault_reset_hang_renamed"
CRASH_NOW = DATA / ".crash-now"
TOTAL = 85
SERVER = "/workspace/scripts/crash_server.py"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(port: int, up: bool, timeout: float = 30) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = socket.socket()
        s.settimeout(0.5)
        ok = s.connect_ex(("127.0.0.1", port)) == 0
        s.close()
        if ok == up:
            time.sleep(0.2)
            return
        time.sleep(0.2)
    raise AssertionError(f"端口 {port} up={up} 等待超时")


def start_server(port: int, crash_mode: str) -> subprocess.Popen:
    env = dict(os.environ, DATA_DIR=str(DATA), TILE_DELAY="0.03",
               TEST_PORT=str(port), CRASH_MODE=crash_mode, PYTHONPATH="/tmp/pylibs")
    log = open(f"/tmp/crash-e-{port}.log", "ab")
    p = subprocess.Popen([sys.executable, "-I", SERVER], stdout=log, stderr=log, env=env)
    wait_port(port, True)
    return p


def stop_server(p: subprocess.Popen, port: int) -> None:
    CRASH_NOW.touch()
    rc = p.wait(timeout=20)
    assert rc == 97, f"正常关停退出码应为 97，实际 {rc}"
    CRASH_NOW.unlink(missing_ok=True)
    wait_port(port, False)


def api(base: str, method: str, path: str):
    req = urllib.request.Request(base + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)
    except (urllib.error.URLError, ConnectionError):
        return None, {}


def job(base: str, kind: str) -> dict:
    with urllib.request.urlopen(base + "/api/status", timeout=10) as r:
        return json.load(r)["jobs"][kind]


def wait_done(base: str, kind: str, timeout: float = 120) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = job(base, kind)
        if j["status"] == "done":
            assert j["published"] == TOTAL
            return
        time.sleep(0.2)
    raise AssertionError(f"{kind} 未在 {timeout}s 内完成")


def http_code(base: str, path: str) -> int:
    try:
        with urllib.request.urlopen(base + path, timeout=10):
            return 200
    except urllib.error.HTTPError as e:
        return e.code


def count_png(kind: str) -> int:
    d = TILES / kind
    return sum(1 for _ in d.rglob("*.png")) if d.exists() else 0


def main() -> int:
    shutil.rmtree(DATA, ignore_errors=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    print("== 预置：出齐两类任务瓦片 ==", flush=True)
    p = start_server(port, "normal")
    api(base, "POST", "/api/jobs/regular/start")
    api(base, "POST", "/api/jobs/emergency/start")
    wait_done(base, "regular")
    wait_done(base, "emergency")
    em_mtime = (TILES / "emergency/0/0/0.png").stat().st_mtime_ns
    assert count_png("regular") == TOTAL
    stop_server(p, port)

    print("== 场景A：真实重置提交后硬退出（journal+trash+空live）-> 重启全部清空 ==", flush=True)
    p = start_server(port, "hang")
    HANG.touch()
    api(base, "POST", "/api/jobs/regular/reset")  # 连接将被硬退出切断，返回值不关心
    rc = p.wait(timeout=20)
    assert rc == 99, f"崩溃退出码应为 99，实际 {rc}"
    wait_port(port, False)
    assert list(TILES.glob(".reset-regular.json")), "journal 应留在磁盘"
    assert list(TILES.glob(".reset-trash-*")), "trash 应留在磁盘"
    assert (TILES / "regular").is_dir() and count_png("regular") == 0
    print("  硬退出现场：journal + trash + 空 live 目录（退出码 99）", flush=True)

    p = start_server(port, "normal")  # 重启 -> recover_pending_resets
    time.sleep(0.5)
    assert not list(TILES.glob(".reset-*.json")), "journal 应被恢复清理"
    assert not list(TILES.glob(".reset-trash-*")), "trash 应被恢复删除"
    assert count_png("regular") == 0
    j = job(base, "regular")
    assert j["published"] == 0 and j["status"] == "idle", j
    for t in ("0/0/0", "2/3/1", "3/7/7"):
        z, x, y = t.split("/")
        assert http_code(base, f"/tiles/regular/{z}/{x}/{y}.png") == 404
    assert (TILES / "emergency/0/0/0.png").stat().st_mtime_ns == em_mtime
    assert http_code(base, "/tiles/emergency/0/0/0.png") == 200
    print("  重启恢复：regular 全清空（进度 0、旧 URL 404），emergency 瓦片完整可取", flush=True)

    HANG.unlink()
    api(base, "POST", "/api/jobs/regular/start")
    wait_done(base, "regular")
    assert count_png("regular") == TOTAL
    assert http_code(base, "/tiles/regular/3/7/7.png") == 200
    print("  恢复后重新切片：regular 从零出满 85/85", flush=True)
    stop_server(p, port)

    print("== 场景B：journal+trash 在位、live 目录尚不存在的更早现场 -> 重启全部清空 ==", flush=True)
    tx = uuid.uuid4().hex[:12]
    live = TILES / "regular"
    trash = TILES / f".reset-trash-{tx}"
    # 把刚出齐的 regular 目录整体挪作 trash，只写 journal、不建 live
    os.rename(live, trash)
    (TILES / ".reset-regular.json").write_text(json.dumps(
        {"kind": "regular", "tx": tx, "stage": "renamed", "started_at": time.strftime("%F %T")}))
    assert not live.exists() and trash.is_dir() and (TILES / ".reset-regular.json").exists()
    print("  已构造现场：journal + trash，live 目录不存在", flush=True)

    p = start_server(port, "normal")
    time.sleep(0.5)
    assert not list(TILES.glob(".reset-*.json"))
    assert not list(TILES.glob(".reset-trash-*"))
    assert live.is_dir() and count_png("regular") == 0
    assert job(base, "regular")["published"] == 0
    assert http_code(base, "/tiles/regular/0/0/0.png") == 404
    assert http_code(base, "/tiles/emergency/0/0/0.png") == 200
    print("  重启恢复：补建空 live、清空 trash，regular 全清空，emergency 不受影响", flush=True)
    stop_server(p, port)

    print("== 场景C：无未完成事务正常重启，瓦片全部保留 ==", flush=True)
    p = start_server(port, "normal")
    assert job(base, "regular")["published"] == 0  # B 场景清空后尚未重切
    api(base, "POST", "/api/jobs/regular/start")
    wait_done(base, "regular")
    stop_server(p, port)
    p = start_server(port, "normal")
    assert job(base, "regular")["published"] == TOTAL
    assert job(base, "emergency")["published"] == TOTAL
    assert http_code(base, "/tiles/regular/0/0/0.png") == 200
    print("  正常重启后两任务瓦片均保留可取", flush=True)
    stop_server(p, port)

    print("\n测试E 全部通过 ✔", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

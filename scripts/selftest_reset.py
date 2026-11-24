#!/usr/bin/env python3
"""重置功能自测（仅标准库）：

  测试C 重置基本语义：
    1. 出齐瓦片 -> 重置 -> 进度归零、旧瓦片 URL 全部 404、磁盘目录已清空；
    2. 重新切片 -> 从零出满 85 张（旧图不会被沿用）；
    3. 另一类任务的瓦片在整个过程中照常可取（互不影响）；
    4. 正在执行的任务拒绝重置（409）。
  测试D 磁盘错误：放置故障标记后重置 -> API 500 报错，瓦片一张不少、进度保留；
    移除标记后重试 -> 重置成功。

“重置途中进程退出后的恢复”见 scripts/test_crash_reset.py（需要可自毁的测试服务）。

用法（服务须已启动，DATA_DIR 与服务一致）：
  BASE=http://127.0.0.1:8000 DATA_DIR=./data python3 -I scripts/selftest_reset.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")
DATA = Path(os.environ.get("DATA_DIR", "./data"))
TILES = DATA / "tiles"
TOTAL = 85
SAMPLE = ("0/0/0", "2/3/1", "3/7/7")
FAULT_BEFORE_RENAME = ".fault_reset_before_rename"


def api(method: str, path: str):
    req = urllib.request.Request(BASE + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}


def status() -> dict:
    return api("GET", "/api/status")[1]


def job(kind: str) -> dict:
    return status()["jobs"][kind]


def wait_for(pred, timeout: float, desc: str):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = status()
        if pred(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f"超时未等到：{desc}")


def wait_idle(timeout: float = 10) -> None:
    wait_for(
        lambda s: all(j["status"] in ("idle", "done", "failed") for j in s["jobs"].values()),
        timeout, "全部任务回到非活动态",
    )


def tile_http(kind: str, t: str) -> int:
    z, x, y = t.split("/")
    try:
        with urllib.request.urlopen(f"{BASE}/tiles/{kind}/{z}/{x}/{y}.png", timeout=10):
            return 200
    except urllib.error.HTTPError as e:
        return e.code


def tile_disk(kind: str, t: str) -> Path:
    z, x, y = t.split("/")
    return TILES / kind / z / x / f"{y}.png"


def count_png(kind: str) -> int:
    d = TILES / kind
    return sum(1 for _ in d.rglob("*.png")) if d.exists() else 0


def start_and_wait_done(kind: str, timeout: float = 120) -> None:
    api("POST", f"/api/jobs/{kind}/start")
    s = wait_for(lambda s: s["jobs"][kind]["status"] == "done", timeout, f"{kind} 切片完成")
    assert s["jobs"][kind]["published"] == TOTAL


def reset_all() -> None:
    for k in ("regular", "emergency"):
        api("POST", f"/api/jobs/{k}/reset")
    wait_idle()


def test_c_basic() -> None:
    print("== 测试C：重置基本语义 ==")
    reset_all()
    start_and_wait_done("regular")
    start_and_wait_done("emergency")
    assert count_png("regular") == TOTAL
    # 记下应急瓦片 mtime，稍后验证它全程不受常规重置影响
    em_mtime = {t: tile_disk("emergency", t).stat().st_mtime_ns for t in SAMPLE}

    code, body = api("POST", "/api/jobs/regular/reset")
    assert code == 200, f"重置应成功，实际 {code} {body}"
    j = job("regular")
    assert j["published"] == 0 and j["status"] == "idle" and j["percent"] == 0.0, j
    print("  重置后常规进度归零、状态 idle")

    for t in SAMPLE:
        assert tile_http("regular", t) == 404, f"旧瓦片 {t} 应 404"
        assert not tile_disk("regular", t).exists(), f"旧瓦片 {t} 磁盘文件应不存在"
    assert count_png("regular") == 0, "磁盘上不应残留 PNG"
    assert not list(TILES.glob(".reset-*")), "journal 应已清理"
    assert not list(TILES.glob(".reset-trash-*")), "trash 目录应已清理"
    print("  旧瓦片 URL 全部 404，磁盘无残留 PNG / journal / trash")

    # 另一类任务不受影响
    for t in SAMPLE:
        assert tile_http("emergency", t) == 200
        assert tile_disk("emergency", t).stat().st_mtime_ns == em_mtime[t]
    print("  应急任务瓦片全程可取且 mtime 未变（两类任务互不影响）")

    # 重新切片必须从零出图，不沿用任何旧图
    start_and_wait_done("regular")
    assert count_png("regular") == TOTAL
    for t in SAMPLE:
        assert tile_http("regular", t) == 200
    print("  重新切片从零出满 85/85，抽样瓦片 200")

    # 进行中的任务拒绝重置
    api("POST", "/api/jobs/regular/reset")  # 先清空
    api("POST", "/api/jobs/regular/start")
    wait_for(lambda s: s["jobs"]["regular"]["status"] == "running", 15, "常规进入渲染中")
    code, body = api("POST", "/api/jobs/regular/reset")
    assert code == 409, f"运行中重置应 409，实际 {code}"
    print(f"  运行中重置被拒绝：HTTP 409（{body.get('detail')}）")
    wait_for(lambda s: s["jobs"]["regular"]["status"] == "done", 120, "常规跑完")
    # 此时另一类任务瓦片仍可取
    assert tile_http("emergency", "0/0/0") == 200
    print("  ✔ 测试C 通过")


def test_d_disk_error() -> None:
    print("== 测试D：清空失败报错且瓦片/进度保留，可重试 ==")
    reset_all()
    start_and_wait_done("regular")
    before = {t: tile_disk("regular", t).stat().st_mtime_ns for t in SAMPLE}
    assert count_png("regular") == TOTAL

    (TILES / FAULT_BEFORE_RENAME).touch()
    code, body = api("POST", "/api/jobs/regular/reset")
    assert code == 500, f"磁盘错误应返回 500，实际 {code}"
    assert "保留" in body.get("detail", ""), body
    print(f"  故障注入下重置返回 HTTP 500：{body.get('detail')}")

    j = job("regular")
    assert j["published"] == TOTAL and j["status"] == "done", "进度必须保留"
    assert count_png("regular") == TOTAL, "瓦片一张都不能少"
    for t in SAMPLE:
        assert tile_disk("regular", t).stat().st_mtime_ns == before[t], f"瓦片 {t} 被改动"
        assert tile_http("regular", t) == 200
    assert not list(TILES.glob(".reset-trash-*")), "失败后不应留下 trash"
    print("  85 张瓦片与 done 进度原样保留，抽样 URL 200、mtime 未变，无 trash 残留")

    (TILES / FAULT_BEFORE_RENAME).unlink()
    code, body = api("POST", "/api/jobs/regular/reset")
    assert code == 200, f"故障排除后重试应成功，实际 {code} {body}"
    assert job("regular")["published"] == 0
    assert count_png("regular") == 0
    for t in SAMPLE:
        assert tile_http("regular", t) == 404
    print("  移除故障标记后重试成功：进度归零、瓦片 404")
    print("  ✔ 测试D 通过")


def main() -> int:
    test_c_basic()
    test_d_disk_error()
    print("\n重置自测全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())

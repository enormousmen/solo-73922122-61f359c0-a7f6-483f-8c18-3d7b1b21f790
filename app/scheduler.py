"""切片调度器。

约束：
- 同一时刻最多运行一个渲染子进程（worker.py）；
- 应急任务受理后立即中止正在渲染的常规子进程（stdin 中止指令 + SIGTERM 双通道），
  等它退出后再开应急；
- 被中止时正在渲染、尚未发布的常规瓦片直接舍弃，恢复后重算；
- 应急无论成功/失败，常规切片都会自动续做；已发布瓦片不重算、照常可取。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

log = logging.getLogger("tiler.scheduler")

TILE_SIZE = 256
ZOOMS = [0, 1, 2, 3]
KINDS = ("regular", "emergency")
ACTIVE = ("queued", "running", "interrupted")
ABORT_RC = 42  # worker 被父进程中止时的约定退出码

# 重置事务的 journal 与故障注入标记（仅用于自测磁盘错误），均为点文件，
# 不会与 z/x/y.png 瓦片路径冲突，也不影响 worker 的 rglob("*.tmp")。
# journal 路径：tiles/.reset-<kind>.json；旧目录：tiles/.reset-trash-<tx>
# 存在该文件时：重命名瓦片目录之前模拟磁盘错误（用于验证“清空失败、瓦片全保留”）
FAULT_BEFORE_RENAME = ".fault_reset_before_rename"
# 存在该文件时：journal 已写、目录改名后卡住，等待外部移除标记（用于模拟重置途中进程退出）
FAULT_HANG_RENAMED = ".fault_reset_hang_renamed"


def all_tiles() -> list[tuple[int, int, int]]:
    return [(z, x, y) for z in ZOOMS for x in range(2 ** z) for y in range(2 ** z)]


TOTAL = len(all_tiles())  # 1 + 4 + 16 + 64 = 85


class ResetError(RuntimeError):
    """重置过程中发生磁盘错误：瓦片与进度均已保留，可在故障排除后重试。"""


def _journal_path(out_dir: Path) -> Path:
    """每任务一份 journal：tiles/.reset-<kind>.json（位于被改名目录之外，改名不动它）。"""
    return out_dir.parent / f".reset-{out_dir.name}.json"


def _write_reset_journal(out_dir: Path, tx: str) -> Path:
    """落盘重置事务 journal（与 tiles/<kind>/ 同级，改名瓦片目录时它不受影响）。"""
    jp = _journal_path(out_dir)
    tmp = jp.with_name(jp.name + ".tmp")
    payload = {
        "kind": out_dir.name,
        "tx": tx,
        "stage": "renamed",
        "started_at": time.strftime("%F %T"),
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, jp)  # 原子落盘
    return jp


def recover_pending_resets(data_dir: Path) -> None:
    """启动恢复：对每个未完成的重置事务继续走完，保证“全部保留或全部清空”。

    journal 存在即代表旧进程已把瓦片目录改名为 .reset-trash-<tx>：
      - 新的空目录尚未建好 -> 先建空目录（对外表现为已清空），再尽力删旧目录；
      - 新的空目录已存在   -> 直接尽力删旧目录。
    删除旧目录若再次遇到磁盘错误：live 空目录已在（重置结论“已清空”成立），
    保留 journal 与 trash 现场并记日志，不阻断本次启动，下次重启继续清理；
    不会出现半清空状态，旧瓦片也不会被当成新任务的成果。
    """
    tiles_root = data_dir / "tiles"
    if not tiles_root.is_dir():
        return
    # journal 落盘中途崩溃可能留下 .reset-<kind>.json.tmp；无正式 journal 即无未完成事务，直接清掉
    for stale in tiles_root.iterdir():
        # 仅处理 journal 临时文件（.reset-<kind>.json.tmp）；trash 目录前缀为 .reset-trash-，跳过
        if (stale.name.startswith(".reset-") and not stale.name.startswith(".reset-trash-")
                and stale.name.endswith(".tmp")):
            try:
                stale.unlink()
            except OSError:
                log.exception("清理过期 journal 临时文件失败：%s", stale)
    for jp in tiles_root.glob(".reset-*.json"):
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.exception("无法读取重置事务日志 %s，保留现场待下次启动处理：%s", jp, e)
            continue
        tx = str(data.get("tx", ""))
        kind = data.get("kind") or jp.name.removeprefix(".reset-").removesuffix(".json")
        live = tiles_root / kind
        trash = tiles_root / f".reset-trash-{tx}"
        log.warning("发现未完成的重置事务 kind=%s tx=%s，启动恢复", kind, tx)
        try:
            live.mkdir(parents=True, exist_ok=True)  # 先保证对外是“已清空”的空目录
            if trash.exists():
                shutil.rmtree(trash)
            jp.unlink()
            log.warning("重置事务 kind=%s 恢复完成：旧瓦片已清空", kind)
        except OSError as e:
            # 现场保留（journal 仍在），下次启动再试；live 目录已空，服务可正常对外
            log.exception("重置事务 kind=%s 恢复清理失败，保留现场待下次重启：%s", kind, e)


class Job:
    """单个切片任务（常规底图 / 应急航片）的运行态。"""

    def __init__(self, kind: str, data_dir: Path):
        self.kind = kind
        self.src = data_dir / "sources" / f"{kind}.png"
        self.out_dir = data_dir / "tiles" / kind
        self.log_file = data_dir / "logs" / f"{kind}.log"
        self.status = "idle"  # idle/queued/running/interrupted/done/failed
        self.published: set[tuple[int, int, int]] = set()
        self.current_tile: tuple[int, int, int] | None = None
        self.error: str | None = None
        self.simulate_fail = False
        self.plan_info: dict | None = None  # 子进程最近一次汇报的 {total, remaining}
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.rescan()

    def tile_file(self, t: tuple[int, int, int]) -> Path:
        z, x, y = t
        return self.out_dir / str(z) / str(x) / f"{y}.png"

    def rescan(self) -> None:
        """以磁盘为准重建已发布集合（启动时、每轮子进程结束后调用）。"""
        self.published = {t for t in all_tiles() if self.tile_file(t).is_file()}
        if not self.published:
            self.status = "idle"
        elif len(self.published) == TOTAL:
            self.status = "done"

    def remaining(self) -> list[tuple[int, int, int]]:
        return [t for t in all_tiles() if t not in self.published]

    def snapshot(self) -> dict:
        done = len(self.published)
        return {
            "kind": self.kind,
            "status": self.status,
            "published": done,
            "total": TOTAL,
            "percent": round(done * 100.0 / TOTAL, 1),
            "current_tile": list(self.current_tile) if self.current_tile else None,
            "error": self.error,
            "plan": self.plan_info,
            "tiles": [f"{z}/{x}/{y}" for (z, x, y) in sorted(self.published)],
        }


class Scheduler:
    def __init__(self, data_dir: Path, tile_delay: float):
        self.data_dir = data_dir
        self.tile_delay = tile_delay
        recover_pending_resets(data_dir)  # 先收尾上次未完成的重置事务，再装载任务
        self.jobs = {k: Job(k, data_dir) for k in KINDS}
        self.proc: asyncio.subprocess.Process | None = None
        self.running_kind: str | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        # 每任务一把重置锁：串行化同任务的重置，避免并发请求在改名/建目录上竞争
        self._reset_locks = {k: asyncio.Lock() for k in KINDS}
        self._worker_py = Path(__file__).resolve().parent / "worker.py"

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="tile-scheduler")

    async def shutdown(self) -> None:
        if self.proc and self.proc.returncode is None:
            self._stop_proc(self.proc)
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _stop_proc(proc: asyncio.subprocess.Process) -> None:
        """立即中止渲染子进程：先写 stdin 中止指令（worker 的 select 会被立即唤醒），
        再补一发 SIGTERM 双保险。某些沙箱环境禁止发信号，两种通道各自容错。"""
        try:
            proc.stdin.write(b"ABORT\n")
        except (BrokenPipeError, ProcessLookupError, RuntimeError):
            pass
        try:
            proc.terminate()
        except (PermissionError, ProcessLookupError):
            pass

    # ---------- 值班员操作 ----------

    def request_start(self, kind: str, simulate_fail: bool = False) -> Job:
        job = self.jobs[kind]
        if job.status in ACTIVE:
            return job  # 幂等：已排队/在跑/被抢占待续，不重复受理
        if not job.remaining():
            job.status = "done"
            return job
        job.status = "queued"
        job.error = None
        job.plan_info = None
        job.started_at = time.time()
        job.finished_at = None
        if kind == "emergency":
            job.simulate_fail = simulate_fail
            if self.running_kind == "regular" and self.proc and self.proc.returncode is None:
                # 立即中止正在渲染的常规瓦片；当前瓦片尚未发布，直接舍弃。
                # 主循环 await proc.wait() 之后才会启动应急子进程。
                self._stop_proc(self.proc)
        self._wake.set()
        return job

    async def reset(self, kind: str) -> Job:
        """对外入口：持每任务锁，把同步磁盘操作放到线程池。"""
        async with self._reset_locks[kind]:
            return await asyncio.to_thread(self._reset_locked, kind)

    def _reset_locked(self, kind: str) -> Job:
        """原子重置：全部保留或全部清空。

        流程（journal + rename 两阶段，崩溃安全）：
          1. 任务非空闲（queued/running/interrupted）一律拒绝（409）；
          2. 写 journal，然后把整个瓦片目录 rename 成 .reset-trash-<tx>（同文件系统原子改名），
             再建一个全新的空目录——这一步之后对外即“已清空”，旧瓦片地址 404；
          3. 同步内存状态；
          4. 尽力删除 trash 与 journal；删不掉只记日志（空间回收问题），不影响重置结果，
             重启时 recover_pending_resets() 会继续清理。
        第 2 步之前的任何磁盘错误都会原样抛出：瓦片一张不少、进度保留，故障排除后可重试。
        """
        job = self.jobs[kind]
        if job.status in ACTIVE:
            raise RuntimeError("任务进行中，无法重置")

        out_dir = job.out_dir
        tiles_root = out_dir.parent
        tx = uuid.uuid4().hex[:12]
        trash = tiles_root / f".reset-trash-{tx}"
        try:
            tiles_root.mkdir(parents=True, exist_ok=True)
            # 自测钩子：模拟“清空失败”的磁盘错误（在任何改动之前触发）
            if (tiles_root / FAULT_BEFORE_RENAME).exists():
                raise OSError(f"模拟磁盘错误：拒绝重置 {kind}（{FAULT_BEFORE_RENAME} 标记存在）")
            _write_reset_journal(out_dir, tx)
            if out_dir.exists():
                os.rename(out_dir, trash)  # 同文件系统原子改名；失败则什么都没发生
            out_dir.mkdir(parents=True, exist_ok=True)
            # 自测钩子：改名已完成、提交点之后挂住，外部杀掉进程以验证重启恢复
            hang_marker = tiles_root / FAULT_HANG_RENAMED
            if hang_marker.exists():
                while hang_marker.exists():
                    time.sleep(0.2)
        except OSError as e:
            # 提交点之前失败：尽力回滚，保证瓦片与进度原样保留
            try:
                if trash.exists() and not out_dir.exists():
                    os.rename(trash, out_dir)
                if not out_dir.exists():
                    out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as rb:
                log.exception("重置回滚也失败了 kind=%s: %s", kind, rb)
            try:
                jp = _journal_path(out_dir)
                if jp.exists() and not trash.exists():
                    jp.unlink()
            except OSError:
                log.exception("清理重置 journal 失败 kind=%s", kind)
            raise ResetError(f"重置失败，瓦片与进度已保留：{e}") from e

        # 提交成功：对齐内存状态（以磁盘为准）
        job.published.clear()
        job.status = "idle"
        job.error = None
        job.current_tile = None
        job.plan_info = None
        job.finished_at = None
        job.rescan()

        # 后台清理旧目录；失败不影响重置结论，重启恢复兜底
        try:
            if trash.exists():
                shutil.rmtree(trash)
            jp = _journal_path(out_dir)
            if jp.exists():
                jp.unlink()
        except OSError:
            log.exception("重置后清理旧瓦片目录失败 kind=%s，将在下次启动时继续清理", kind)
        return job

    # ---------- 调度主循环 ----------

    def _pick(self) -> str | None:
        em, rg = self.jobs["emergency"], self.jobs["regular"]
        if em.status == "queued" and em.remaining():
            return "emergency"  # 应急优先
        if rg.status in ("queued", "interrupted") and rg.remaining():
            return "regular"
        return None

    async def _loop(self) -> None:
        while True:
            kind = self._pick()
            if kind is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await self._run(kind)  # 串行执行 => 同时最多一个子进程
            except Exception as e:  # 子进程spawn失败等意外：标记失败，调度循环存活
                job = self.jobs[kind]
                job.status = "failed"
                job.error = f"scheduler error: {e!r}"
                self.proc = None
                self.running_kind = None

    async def _run(self, kind: str) -> None:
        job = self.jobs[kind]
        job.status = "running"
        # -P：不把脚本目录/当前目录放进 sys.path，但保留 site-packages（Pillow 在其中）
        cmd = [
            sys.executable, "-P", str(self._worker_py),
            "--src", str(job.src),
            "--out", str(job.out_dir),
            "--tile", str(TILE_SIZE),
            "--zooms", ",".join(map(str, ZOOMS)),
            "--delay", str(self.tile_delay),
        ]
        if kind == "emergency" and job.simulate_fail:
            cmd += ["--fail-after", "10"]  # 自测钩子：发布 10 张后模拟崩溃
        logf = open(job.log_file, "ab")
        logf.write(f"\n===== run {time.strftime('%F %T')} =====\n".encode())
        logf.flush()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,   # 中止指令通道
            stdout=asyncio.subprocess.PIPE,  # 进度事件
            stderr=logf,
        )
        self.proc = proc
        self.running_kind = kind
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                self._apply(job, ev)
        finally:
            rc = await proc.wait()  # 等原进程退出，之后主循环才可能开下一个
            logf.close()
            self.proc = None
            self.running_kind = None
            job.current_tile = None
            job.finished_at = time.time()
            job.rescan()  # 以磁盘为准对齐已发布集合
        if rc == 0:
            job.status = "done"
        elif rc == ABORT_RC or rc < 0:
            # 被应急任务中止（stdin 指令或 SIGTERM）；若恰好已全部发布则视为完成
            job.status = "done" if not job.remaining() else "interrupted"
        else:
            job.status = "failed"
            job.error = job.error or self._log_tail(job)

    def _apply(self, job: Job, ev: dict) -> None:
        kind = ev.get("event")
        if kind == "plan":
            job.plan_info = {"total": ev["total"], "remaining": ev["remaining"]}
        elif kind == "start_tile":
            job.current_tile = (ev["z"], ev["x"], ev["y"])
        elif kind == "tile":
            job.published.add((ev["z"], ev["x"], ev["y"]))
            job.current_tile = None
        elif kind == "error":
            job.error = ev.get("message")

    @staticmethod
    def _log_tail(job: Job, n: int = 2000) -> str | None:
        try:
            data = job.log_file.read_bytes()[-n:]
            return data.decode("utf-8", "replace").strip() or None
        except OSError:
            return None

    def snapshot(self) -> dict:
        return {
            "running_kind": self.running_kind,
            "tile_delay": self.tile_delay,
            "jobs": {k: j.snapshot() for k, j in self.jobs.items()},
        }

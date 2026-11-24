# 切片值班台（常规底图 / 应急航片 · 应急插队）

FastAPI + Pillow 的切片演示服务：内置常规底图、应急航片各一张（2048×2048，启动时自动生成），
值班员可分别启动切片、查看进度、打开已发布的 PNG 瓦片；应急任务受理后立即抢占常规渲染。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements.txt
DATA_DIR=./data TILE_DELAY=0.15 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

环境变量：`DATA_DIR`（源图/瓦片/日志目录，默认容器内 `/data`）、`TILE_DELAY`（每张瓦片模拟渲染耗时秒数，默认 0.15，调大可放慢演示节奏）。

## 页面演示（渲染到一半插队 / 重置后重新出图）

1. 点「启动切片」→ 常规底图进度条开始推进，瓦片逐张点亮；
2. 渲染到一半时点「启动应急切片（插队）」→ 常规立即变为「被抢占·待续算」，应急开始渲染；
3. 应急完成后常规自动续做（本轮计划数 = 剩余未发布张数，已发布的不重算）；
4. 勾选「模拟渲染失败」再启动应急，可演示应急失败后常规照常续做；
5. 点击任意已点亮瓦片可在新标签页打开对应 PNG；任务空闲时点「重置」清空该任务全部瓦片，
   再点「启动切片」即从零重新出图（重置后旧瓦片地址返回 404，不会沿用任何旧 PNG）。

## 重置功能的崩溃安全设计（修复重点）

旧实现用 `shutil.rmtree(ignore_errors=True)` 删瓦片：删除失败也静默成功，导致进度归零但旧 PNG
仍在，重新切片还会沿用没删掉的旧图。新实现（`app/scheduler.py`）改为 **journal + 改名两阶段事务**：

1. **进行中拒绝**：任务状态为 `queued/running/interrupted` 时重置直接 409，另一类任务的瓦片不受影响、照常可取。
2. **写 journal**：`tiles/.reset-<kind>.json`（原子 `os.replace` 落盘，位于被改名目录之外）。
3. **原子改名**：整个 `tiles/<kind>/` 同文件系统 `os.rename` 为 `tiles/.reset-trash-<tx>/`，
   再 `mkdir` 一个全新的空目录。改名成功即提交点——此后对外表现为“已清空”，旧瓦片 URL 一律 404；
   改名之前的任何磁盘错误都会抛出并回滚，瓦片一张不少、进度保留，API 返回 500，前端弹窗报错，故障排除后可重试。
4. **清理**：提交后同步内存状态（进度归零、status=idle），再尽力删除 trash 与 journal；
   清理失败只记日志、不影响重置结论。
5. **启动恢复**（`recover_pending_resets`）：服务启动时扫描 journal，对未完成事务收尾——
   live 目录缺失就补建空目录，再删除 trash 和 journal。因此服务在重置途中退出，
   重启后该任务的瓦片必然是“全部保留（提交前崩溃、无 journal）或全部清空（提交后崩溃、有 journal）”，
   不存在半清空状态，旧瓦片也不会被当成新任务的成果。

故障注入钩子（仅自测用，均为点文件，不影响瓦片与 worker）：

- `tiles/.fault_reset_before_rename`：存在时重置在改名前抛 `OSError`，验证“清空失败、全保留、可重试”；
- `tiles/.fault_reset_hang_renamed`：存在时重置在改名提交后挂起，配合测试服务模拟“进程中途退出”。

## 实现内容

- **任务模型**（`app/scheduler.py`）：常规 / 应急两个任务，各 85 张瓦片（z0–z3，256px）。
  状态机：`idle → queued → running → done / failed / interrupted`；`interrupted` 的常规任务在应急结束后被调度器自动续做。
- **单渲染子进程**：调度主循环串行执行 `_run()`，同一时刻最多一个 `worker.py` 子进程；
  应急受理时若常规在跑，先中止旧进程并 `await proc.wait()`，确认退出后才启动应急进程。
- **抢占协议**：父进程向子进程 stdin 写 `ABORT` 指令（worker 用 `select` 监听 stdin，
  渲染睡眠可被立即唤醒，收到指令或 EOF 即以约定退出码 42 退出），同时补发 SIGTERM 双保险。
  不依赖信号即可工作（某些沙箱环境禁止 kill），正常 Linux / Docker 中两条通道都有效。
- **舍弃与重算**：瓦片先写 `.tmp` 再 `os.replace` 原子发布；被中止时当前瓦片尚未发布，
  残存 `.tmp` 由下次启动的 worker 清理，该瓦片在续做时重算。worker 跳过已存在的 PNG，
  已发布瓦片不重算；父进程每轮结束按磁盘重新对齐已发布集合。
- **应急失败钩子**：`POST /api/jobs/emergency/start?simulate_fail=true`，worker 发布 10 张后以退出码 3 模拟崩溃，用于自测与演示。
- **原子重置**：journal + rename 两阶段，提交点为目录改名；失败报错（HTTP 500）且瓦片/进度全保留，
  重启自动收尾未完成事务（详见上节“重置功能的崩溃安全设计”）。
- **API**：
  - `GET /api/status` 两任务进度快照（状态、百分比、在渲瓦片、本轮计划、已发布清单）；
  - `POST /api/jobs/{kind}/start`（kind=regular|emergency，幂等；emergency 支持 `simulate_fail`）；
  - `POST /api/jobs/{kind}/reset`（任务进行中返回 409；清空失败返回 500 且不改动任何瓦片与进度）；
  - `GET /tiles/{kind}/{z}/{x}/{y}.png`（未发布或已重置 404）；
  - `GET /sources/{kind}.png` 源图预览；`GET /` 演示页面。
- **源图**（`app/sources.py`）：启动时用 Pillow 生成两张 2048×2048 区分度高的测试图（渐变 + 网格 + 坐标标注），已存在则不覆盖。

## 实测结果

环境：Python 3.11 / FastAPI 0.165 / Pillow 13，`TILE_DELAY=0.03~0.05`，以容器同款命令
`python -m uvicorn app.main:app` 单进程实测（本机无 Docker 守护进程，见“未完成项”）。

回归自测 `scripts/selftest.py`（仅标准库）：

```
== 测试A：应急插队 ==
  常规已发布 8 张，在渲 2/0/3，此刻受理应急…
  0.11s 内切换到应急，常规状态=interrupted
  应急完成 85/85，期间渲染进程始终为 emergency
  常规续做完成 85/85；8 张已发布瓦片 mtime 未变（未重算）；在渲瓦片 2/0/3 已重算；抽样瓦片均可取
  ✔ 测试A 通过
== 测试B：应急失败后续做 ==
  应急发布 10 张后失败（error='simulated render failure after 10 tiles'），等待常规续做…
  常规续做完成 85/85；应急失败留下的已发布瓦片仍可取
  ✔ 测试B 通过
```

重置自测 `scripts/selftest_reset.py`（HTTP 层面，仅标准库）：

```
== 测试C：重置基本语义 ==
  重置后常规进度归零、状态 idle
  旧瓦片 URL 全部 404，磁盘无残留 PNG / journal / trash
  应急任务瓦片全程可取且 mtime 未变（两类任务互不影响）
  重新切片从零出满 85/85，抽样瓦片 200
  运行中重置被拒绝：HTTP 409（任务进行中，无法重置）
  ✔ 测试C 通过
== 测试D：清空失败报错且瓦片/进度保留，可重试 ==
  故障注入下重置返回 HTTP 500：重置失败，瓦片与进度已保留：模拟磁盘错误：拒绝重置 regular（…标记存在）
  85 张瓦片与 done 进度原样保留，抽样 URL 200、mtime 未变，无 trash 残留
  移除故障标记后重试成功：进度归零、瓦片 404
  ✔ 测试D 通过
```

崩溃恢复自测 `scripts/test_crash_reset.py`（配合 `scripts/crash_server.py`；本机 seccomp 过滤
kill 系调用，由测试服务在重置挂住点自行 `os._exit()` 硬退出，等价 SIGKILL、跳过所有 finally）：

```
== 预置：出齐两类任务瓦片 ==
== 场景A：真实重置提交后硬退出（journal+trash+空live）-> 重启全部清空 ==
  硬退出现场：journal + trash + 空 live 目录（退出码 99）
  重启恢复：regular 全清空（进度 0、旧 URL 404），emergency 瓦片完整可取
  恢复后重新切片：regular 从零出满 85/85
== 场景B：journal+trash 在位、live 目录尚不存在的更早现场 -> 重启全部清空 ==
  已构造现场：journal + trash，live 目录不存在
  重启恢复：补建空 live、清空 trash，regular 全清空，emergency 不受影响
== 场景C：无未完成事务正常重启，瓦片全部保留 ==
  正常重启后两任务瓦片均保留可取
测试E 全部通过 ✔
```

另核验：`/`、`/app.js`、`/style.css`、`/sources/{kind}.png` 均 200；
已发布瓦片 200 且为合法 PNG，重置后旧瓦片地址 404；compose YAML 与 Dockerfile 配置通过静态校验。

## 未完成项 / 说明

- 本交付环境无 Docker 守护进程（`docker` 命令不存在），`docker compose up --build` 未在本机实际构建运行；
  Dockerfile / compose 未做改动，应用本体已在容器外以容器同款 uvicorn 单进程命令实测通过，
  按 compose 启动后可直接在切片页演示“重置 → 旧图 404 → 重新出图”。
- 本机 seccomp 过滤了 `kill` 系调用，无法从外部向进程发信号；“进程中途退出”改用测试服务自身
  `os._exit()` 模拟（退出语义与 SIGKILL 相同：不执行任何 Python 清理）。
- 调度状态在内存中，必须单 uvicorn worker 运行（Dockerfile 已固定单进程）；服务重启后已发布瓦片保留，
  但未完成任务需重新启动（不自动续跑）；未完成的重置事务则会在启动时自动收尾。
- 若重启时 trash 目录仍因磁盘故障删不掉，服务会保留现场并在下次启动继续恢复（不会半清空）；
  该“恢复再次失败”分支未做故障注入实测，代码路径与测试D 的错误处理同源。
- 渲染耗时用 `TILE_DELAY` 模拟，未接真实切图负载；瓦片方案为单源图 z0–z3 固定金字塔，未做 XYZ 坐标系对齐与多比例尺重投影。
- 未做鉴权与并发操作限流（重复启动幂等，重置仅做空态校验）。

# 项目规则

## 状态（重构重启）

- **之前的重构版本（Cardlo 仓的卡分支施工线）做偏离了，已停止，不再延续。**
- 现以 **CardColony** 重新开始重构。本仓（推演agent）仍是原型探索记录，
  画布交互稿/视觉稿继续作为新实现的参照。

## 本线文档

- **执行体路由（wb_exec）交接文档**：`docs/执行体路由(wb_exec)-交接文档-20261106.md`
  （候选表 / 优先级瀑布 / 五个执行体事实卡 / 排障手册 / 定时任务口径 / 命令速查）
- 机制细节与历史实测：`docs/opencode接入coder卡-最佳方案-20261025.md`

## 服务重启必须请示

**禁止未经用户明确同意就重启/杀死 5700 服务进程**（main.py / .server.pid）。
改完代码需要重启生效时：**报告改动＋问一句"要不要现在重启"**，等用户说重启再动。
- 服务是活的：在跑的卡、在途模型回合、断点续跳都会被打断。
- 前端静态文件（static/ 下 JS/CSS/HTML）不用重启，让用户刷新即可——
  只有后端代码（src/、config/）变更才需要重启。
- 用户多次强调过这条，不得"顺手"重启验证。

## 验证方式

- 测试：`PYTHONPATH=tests python -m pytest tests/xxx.py -q`
- 语法：`python -m py_compile <file>` / `node --check <js>`
- 验证通过 ≠ 可以重启；重启永远是请示项。

## 多进程拆分（ECO_ROLE，2026-11-06）

- `python main.py`（不设 ECO_ROLE）＝旧单进程行为，测试/开发照跑。
- 生产拓扑：web×1＋runner×K（ECO_RUNNER_COUNT）＋condense×1。
  web＝`ECO_ROLE=web ECO_RUNNER_COUNT=K python main.py`（5700 对外）；
  runner-i＝`ECO_RUNNER_COUNT=K python runner_main.py i`
  （5701+i 回环，ECO_RUNNER_BASE_PORT 可改基数）；
  condense＝`python condense_main.py`（5710 裸探针，ECO_CONDENSE_PORT 可改）。
  **凝练只在 condense 进程跑**（ECO_ROLE=condense；all 单进程照旧）——
  runner/web 一律被 `_condense_home()` 闸退，扫盘、排水、fold 都是它的专活；
  跨进程重复凝由「只有一个进程能凝」根除（pid 文件 `.server-condense.pid`）。
- 卡按**子树根**稳定哈希（md5，不用内建 hash——跨进程要确定性）分到
  属主 runner；父子必同片 ⇒ 单卡单写者天然成立。串行模式全部归 0 号。
- web：白名单只读 GET 本地答（procs.py `_WEB_LOCAL_GET_TPLS`）；带卡路径
  转属主 runner，无卡路径转 0 号；`/nav/running`、`/pulse`、`/observe/calls`
  扇入全 runner 合并；`/works/{}/events` 向全 runner 各开一条 SSE 扇入。
- 跨片执行：`routes._run_message`/`message_runner.deliver_now` 入口自查
  分片，非本片透明 POST 属主 `/ctl/run_message`·`/ctl/deliver`。
- startup 钩子：驱动器/调度器/续跑全库一份只跑 runner-0；非 0 号只装
  `goal_driver.install()`（goal/changed 是本进程内监听）。
- **新增端点先想它落在哪一侧**；所有进程都要起、都要活；重启=重启一组，
  仍是请示项。
- 启停（2026-11-07）：`start.bat`/`stop.bat`/`restart_daily.bat` 已按拓扑
  重写——先杀看门狗（不然它会复活服务），再起/停整组。
  pid 文件：web/all＝`.server.pid`，runner-i＝`.server-runner{i}.pid`；
  `stop.bat` 立 `logs\.manual_stop`，看门狗见此标记不拉起（`start.bat` 撤）。
  看门狗逐进程探活（web 5700 + 各 runner 5701+i），谁死单独拉起谁，不连坐。

## 上下文缓存板（context_board，2026-11-12）

- 历史区不再从全量原文现算——`context_board` 表（每 work 库内）物化
  「过闸 且 seq>=凝练 mark 且评审 keep-N」的可见块，装配只做机械投影。
- 维护挂在写口：块落库（`write_blocks_sync` 同事务过闸进板）、
  mark 推进（`save_chain`/`_save_to_db` 同事务 `DELETE seq<mark`）、
  评审流量写时逐出。原文账（session_blocks）永远是真相，
  板漂移一键 `context_board.rebuild_board` 重算；读侧有懒自愈
  （板空有货/落后原文 → 就地重建，60s 节流）。
- 开关：`CONTEXT_BOARD=0` 回退旧四层筛路（排障用，默认开）。
- `session_facade.BLOCK_FIELDS` 已含 `reasoning_content`（Kimi 保留式
  思考持久化依赖它落盘）。

## "commit" 的含义

用户说 **"commit"＝同步到 GitHub**，不是只本地落账。完整动作链：

1. 本地 `git add -A && git commit`（排除 `D?Temp*`、`_devin_install.ps1` 等杂物）
2. 克隆到 `D:\Temp\prototype-pushN` → `git-filter-repo` 按既定规则清洗
   （剥 `config/wb_exec*.json`、`docs/workbuddy*`、`docs/opencode*`、
   `docs/release-prep/宣发主导方案-v1.md`；`--replace-text` 抹机器指纹，
   规则存 `D:\Temp\prototype-push-replacements.txt`）
3. 复核零残留后推 `CardColony/cardcolony-prototype`（dev→master）
4. Cardlo 仓同理：剥 `spike/*/data/`、`spike/*/calib/`，强推 `cardcolony-base`

界面类改动例外：**先本地给用户确认，再同步**（用户明确要求过）。

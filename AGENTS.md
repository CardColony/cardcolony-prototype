# 项目规则

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
- 生产拓扑：web×1＋runner×K（ECO_RUNNER_COUNT）。
  web＝`ECO_ROLE=web ECO_RUNNER_COUNT=K python main.py`（5700 对外）；
  runner-i＝`ECO_RUNNER_COUNT=K python runner_main.py i`
  （5701+i 回环，ECO_RUNNER_BASE_PORT 可改基数）。
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

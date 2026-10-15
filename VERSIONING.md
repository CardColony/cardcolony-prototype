# 版本管理规范


---

## 【2026-10-03 事故后加 · 三条永久禁令】

### 事故摘要
2026-10-03 15:01，在**承载运行数据的正式环境目录**（D:\AI_station\推演agent）上执行 `git checkout dev`。
git 切分支的语义是「把工作区全部受追踪文件改写成目标分支版本」——works/ 里 22 个受追踪文件（卡片、对话、画布）
被整体回退到 dev 快照（约 12:50 状态）。三重损害同源：用户对话回退、已删卡复活、新建卡消失。
恢复源：master 的 7aea14b（15:01:03 auto-commit 抢救的最后完整快照）；恢复 tag：recover/prod-2026-10-03。

### 禁令一：禁止在承载运行数据的目录上做任何 git 分支操作
- 禁止：`git checkout` / `git switch` / `git merge` / `git rebase` / `git reset --hard` / `git stash`
- 判据：执行前必须过 `python checkout_guard.py --cmd "<命令>"`，退出码 1 即禁止
- 为什么：只要 works/ 仍在追踪，分支操作就会重写用户数据

### 禁令二：版本管理的操作对象只是代码
- **进版本**：src/ · templates/ · static/ · config/ · 文档 · 脚本（除临时脚本）· SOP · 台账
- **不进版本**：works/（**卡片库 `cards.db`**、对话、画布、work.json）· logs/ · _backups/ · 运行态文件 · .env
- [2026-10-14 卡片存储迁移] 卡片与影子已从 md 迁入 `works/<work_id>/cards.db`（SQLite，WAL）。
  它**仍属运行数据**：整个 `works/` 已被 `.gitignore` 挡住（含 `cards.db` 与其 `-wal`/`-shm` 旁挂文件），
  口径不变——**任何分支操作都不得改写它**（见禁令一）。
- 数据留痕改由**独立快照机制**承担：`D:\AI_station\推演agent_data_snapshots\`（与代码仓库物理分离，git 碰不到）
- 回退通道：`python restore_from_snapshot.py --list` / `--work X --stamp Y`（恢复前强制备份当前状态）

### 禁令三：环境与版本动作先取证再动手
- 涉及环境（正式/测试）、版本（分支/tag/提交）、数据（恢复/清理）的动作，一律先只读取证、再落方案、最后执行
- 禁止凭记忆认领临时品（见 SOP_清理废弃剧本.md）· 禁止在没读路由/返回结构的情况下写脚本

### 第二道防线（如实说明其局限）
- `.git/hooks/pre-commit`：拒绝把 works/ 下的数据提交进 git（已挂，真实验证能拦）
- **局限**：hook 挡不住直接敲 `git checkout`（那是另一个动作，不触发 pre-commit）
- **真正挡住的是**：works/ 退出 git 追踪（剥离后分支操作对数据无影响）——这才是根治

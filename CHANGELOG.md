# CHANGELOG

推演agent 的能力变更与事故记录。版本线以 git tag 为准（`git tag -l`）。

## [未发布] 数据与代码分家（2026-10-03）

**新增**
- `src/storage/data_snapshot.py`：数据快照机制。快照根 `D:\AI_station\推演agent_data_snapshots\`
  （与代码仓库物理分离——git 的任何分支操作都碰不到它）。保留策略：每 work 最近 200 批次 + 每日一份日备（保留 30 天）。
  可恢复性经「真删真恢复、sha 逐字节一致」验证（138B 测试文件，sha b27995a43cfa76b7）。
- `restore_from_snapshot.py`：快照恢复工具（`--list` / `--work X --stamp Y` / `--file`）。
  恢复前强制备份当前状态到 `D:\AI_station\推演agent_backups\pre_restore_<时间戳>\`。
- `checkout_guard.py`：分支操作守卫。机械判据三条（works/ 是否仍被追踪 / 工作区是否有未提交的运行数据 / 是否承载运行数据的环境），
  退出码 1 即禁止。实测拦住 `git checkout master`。
- `.git/hooks/pre-commit`：拒绝把 works/ 下的运行数据提交进 git。真实验证能拦（staged 数据提交被拒，rc=1）。

**变更**
- 七处数据写入点从 `git add -A 提交` 改为写独立快照：
  `card_store.py` L318（create）/ L348（external_raw）/ L354（external_logged）/ L458（update）/ L516（delete）/ L558（derive ending）
  + `work_store.py` L148（create work）。
- 数据留痕与代码版本彻底分家：works/ 不再进 git，回退通道从 git 改为快照。

---

## [事故记录] 2026-10-03 15:01 · 分支操作覆盖运行数据

**发生了什么**：在正式环境目录（承载 works/ 运行数据）执行 `git checkout dev`。
git 切分支的语义是「把工作区全部受追踪文件改写成目标分支版本」——works/ 里 22 个受追踪文件（卡片/对话/画布）
被整体回退到 dev 分支快照（约 12:50 状态）。

**损害范围**（用户可见）：
- 对话回退：work_001 session_001 156,816B → 155,961B；work_002 session_001 28,775B → 12,128B；session_002 16,994B → 1,953B
- 用户 13:10 已删除的卡复活：work_002 card_003 / card_004
- 用户 14:57 新建的卡消失：work_001 card_007 / card_008
- work_002 card_002 回退到旧版：7,153B → 2,502B

**恢复结果**（15:27–15:34，全部落盘并验证）：
- 源：master 的 `7aea14b`（15:01:03，auto-commit 在 checkout 落地前后抢救的最后完整快照）
- 方式：逐文件 `git show 7aea14b:<路径>` 取回（**禁止 checkout/restore、禁止整目录回退**）
- work_001：session_001 复原 156,816B ✓ · card_007/card_008 写回 ✓ · canvas 按真实卡池重建（6 张）✓
- work_002：session_001 复原 28,775B ✓ · session_002 源基底＋15:04 后真实对话追加（812 字，一字未丢）✓ ·
  card_002 保留新版（源旧版另存备查）✓ · card_003/004 重删 ✓ · card_005 保留 ✓ · canvas 重建（3 张）✓
- 验证：磁盘字节对账全 OK · 内容人工核对认出是用户对话 · HTTP 层可见（work_001 6 卡 / work_002 3 卡）· 恢复 tag `recover/prod-2026-10-03` 已打
- **不可恢复空洞**：15:01–15:05 四分钟内的操作无任何副本（已如实告知用户）

**防再犯措施**（全部落地并验证）：
1. 数据与代码分家（见上「未发布」节）
2. 机械守卫：`checkout_guard.py` + `.git/hooks/pre-commit`（均已实测能拦）
3. 纪律落盘：VERSIONING.md 与 SOP_清理废弃剧本.md 写入三条永久禁令
   （禁止在承载运行数据的目录上做任何 git 分支操作 / 版本管理对象只是代码 / 环境与版本动作先取证再动手）

**局限如实说明**：hook 挡不住直接敲 `git checkout`（不触发 pre-commit）；
**真正挡住的是 works/ 退出 git 追踪**（剥离后分支操作对数据无影响）——剥离是根治，hook 是第二道。

---

## [里程碑] v0.1.0-可用态（2026-10-03 12:32）

14 件工具（卡片增删改解线、联网搜索、本地文件四件套）· 三思考席＋统合官链路 · 文本泄漏抢救四形态 ·
防误杀重启规程 · 卡片树与画布前端。端到端活体验证通过（模型真实调用 read_file 读出真实材料）。

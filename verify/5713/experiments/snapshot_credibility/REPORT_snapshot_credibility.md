# 5713 数据快照与恢复链路 · 真实性判定（第一轮交付）

- 结论日期：2026-10-20
- 结论来源：**真删真恢复实验**（脚本可复跑），不采信"读代码觉得对"
- 实验现场（全部在 5713 沙箱内，生产快照根零触碰）：
  - 驱动器：`D:\AI_station\推演agent\.worktrees\5713_sandbox\experiments\snapshot_credibility_exp.py`
  - 机械证据：`...\experiments\snapshot_credibility\evidence.json`（12 个阶段 P0–P12 的原始观测值）
  - 批次完整性对账：`...\experiments\snapshot_completeness_audit.py` → `...\snapshot_credibility\completeness.json`
  - 实验数据根：`...\experiments\snapshot_credibility\{base,snapshots}`（已被脚本重建，可随时重跑）
  - 复跑命令：
    `.venv\Scripts\python.exe D:\AI_station\推演agent\.worktrees\5713_sandbox\experiments\snapshot_credibility_exp.py`
    （脚本自己重建 base/ 与 snapshots/，只写实验根）
- 隔离手法：与 `scripts/run_test_instance_5701.py` 同款——把 `data_snapshot.BASE_DIR / SNAPSHOT_ROOT / _FINGERPRINT_FILE`、`card_store.WORKS_DIR` 指到实验根；恢复动作调用**仓库已发布**的两条通道（根目录 `restore_from_snapshot.py:restore_batch()`、`data_snapshot.restore_from_snapshot()`），只重定向模块级根常量。

---

## 一、判定（一句话）

**能恢复，但只覆盖「单个 work 的 `works/` 目录」，且不是时间点回滚、没有整实例还原点、恢复要靠停机手工跑脚本——按现状它不足以当"回退保险"，只能当"局部误删的补救手段"。**

---

## 二、它真能恢复的部分（实测通过）

| 实验 | 做法 | 结果 |
|---|---|---|
| P2 真删真恢复 | 拍快照（work_007，8 文件/库 172KB）→ `rmtree` 整目录（`dir_deleted=true`）→ `restore_batch()` | 8 文件全回；7 个非库文件 sha256 **逐一致**（`files_same=7, diff/missing/extra=0`）；`cards.db` 逻辑状态一致（`integrity=ok`、13 张表行数全等、卡片集 sha256 相同） |
| P3 单文件恢复 | 破坏 `sessions/session_001.md`（sha 变化）→ `restore_from_snapshot()` | 恢复后 sha 与原逐字相同（33554 B，`restored_exact=true`） |
| P2 库一致性 | 源库当时 `cards.db` 4096B + `-wal` 519KB（数据主要在 WAL 里） | 快照里的库 167936B，行数/卡片与删前完全一致 ⇒ `VACUUM INTO` 抓到了 WAL 里的已提交数据 |
| P12 存量对账 | 读 5713_sandbox 真实快照根 | 30 个 work **全部有批次**（1192 个真批次 + 30 个日备）；活跃 work 最新批次与活数据滞后 ≈0 分钟；真实剧本的 1022 个批次**全部含 cards.db**（另 200 个缺库批次全属 `works/_ops`，该目录本就没有 cards.db，非缺陷） |
| P8 库回退能力 | 造 S1（1 卡）→ 插 card_777 → S2（2 卡）→ 从 S1 批次恢复 cards.db | 磁盘回到 1 张卡；**另开着的活连接**读到的也是恢复后的 1 张卡，写入无报错、`integrity=ok` |

性能基线：work_007（8 文件）单批快照 **563.5 ms**（P1 实测）。

---

## 三、什么情况下失效（每条都有实验编号）

### 1. 覆盖只有 `works/`——works 之外的数据完全没有保险【P5 / P11】
- 实测：在 `trading-workspace/notes/user_note.md` 落文件 → 拍快照 → 删文件 → 恢复，返回 `{"ok": false, "error": "快照不存在: .../trading-workspace/notes/user_note.md"}`，`file_recoverable=false`；批次清单里没有它。
- 量级（5713_sandbox 实测）：`works/` 87.98 MB / 458 文件（**受保护**）、`trading-workspace/` 91.13 MB / 540 文件（**无保护**）、`logs/` 26.80 MB / 137 文件（**无保护**）。→ 实例里**超过一半的用户产物不在保险范围内**。

### 2. 指纹盲区：同尺寸 + 同秒 mtime 的修改被判"内容未变"，永不落快照【P6】
- 实测：33554 B 文件改 1 字节、`os.utime` 精确还原 mtime → 再拍快照得 `files=0, skipped=true`，**不产生新批次**（batches 2→2）。
- 真删该文件后按恢复通道取回：拿到的是**改前内容 v1**（`equals_v1=true, equals_v2=false`）⇒ 这一次修改永久不可恢复。
- 触发条件（真实可发生）：JSON 标志位翻转、状态块同长替换、程序化回写等"同长度 + 同秒"写入。指纹口径是 `路径|size|int(mtime)`，不含内容哈希。

### 3. 整批恢复只"覆盖"不"删除"⇒ 不是时间点回滚【P4】
- 实测：快照之后新建的 `cards/card_999.md` 在整批恢复后**仍在**（`post_snapshot_new_file_still_present=true`）；被改的 `work.json` 回到快照内容（`modified_file_reverted=true`）。
- 恢复后是"快照内文件 ∪ 现存多余文件"的混合态；"只补不删"是脚本设计写明的口径（`restore_from_snapshot.py` docstring），但事故时人会把它当回滚。

### 4. 没有整实例还原点：批次按 work 分别拍【P12】
- 实测（同一实例）：work_001 最新批次 16:54、work_013 16:50、work_016 16:36、work_029 18:29。→ 事故后只能**逐个 work 挑不同时刻**的批次，不存在"某一时刻的全实例"。

### 5. 日备是"当天首拍"，不是日终状态【P7】
- 实测：`daily_20261020/works/work_007/sessions/session_001.md` 与活数据**不同**（`daily_newest_file_equals_live=false`），与最新批次也不同（`daily_equals_latest_batch=false`），文件已 stale 12036s（≈3.3 小时）。日备只在"当天第一次快照"时落一次，之后当天所有变更都不进去。

### 6. 可回退窗口按"条数"算，不按时间算；高写入速率的 work 实际只剩约 1 小时【P12 实测跨度】
- `KEEP_BATCHES=200`（每 work 最近 200 个非日备批次）。实测覆盖跨度：
  - `work_013`：194 个批次只覆盖 **58.3 分钟**（15:52→16:50）
  - `_ops`：199 个批次覆盖 **142.6 分钟**
  - `work_016`：102 个批次覆盖 36.1 分钟
- ⇒ 对写入密集的 work，200 条上限约等于 **1 小时历史**；"日备保留 30 天"救不了当天的细粒度回退，因为日备只有当天最早那一张。

### 7. 仓库自带的恢复 CLI 在非生产目录结构下指错根，一条批次都看不到【P9】
- 实测：在 `.worktrees\5713` 跑 `python restore_from_snapshot.py --list` → 输出
  `快照根: D:\AI_station\推演agent\.worktrees\推演agent_data_snapshots`、`（无批次）`，`computed_root_exists=false`。
  而该实例的真实快照根是 `...\5713_sandbox\snapshots`（实测 1222 个批次）。
- 原因（作为解释，不作为结论依据）：脚本把 `SNAPSHOT_ROOT` 硬编码成 `BASE_DIR.parent/"推演agent_data_snapshots"`，不走 `data_snapshot` 的派生规则，也不认沙箱重定向 ⇒ **worktree / 沙箱 / 任何改名实例上这条恢复通道等于不可用**，只能手工改常量（本次实验正是这么做的）。

### 8. 恢复后库的 `journal_mode` 从 wal 掉成 delete，且不会自愈【P8 / P10】
- 实测：恢复前 `journal_mode=wal`；恢复（`VACUUM INTO` 产物）后为 `delete`；再走产品连接 `card_db.run_read()` 读一次，仍为 `delete`。
- 解释（代码侧，非结论依据）：`PRAGMA journal_mode=WAL` 只在"cards 表不存在（建新库）"时执行一次；恢复出来的库表已存在，故永不重设。

### 9. 没有 UI / API 恢复入口，恢复=停机 + 手工脚本【实测 openapi】
- 实测：5713 实例 `openapi.json` 共 66 条路径，唯一 restore 是 `POST /api/works/{work_id}/cards/{card_id}/restore`——它走 `cards.db` 的回收站（trash 表），**不读快照**。
- ⇒ 快照恢复只能由人记住命令、手工在正确目录跑；事故现场最缺的正是这个。

### 10. 与回收站的分层关系（补充事实）
- 删除卡片进 `trash` 表，而 trash 表在 `cards.db` 里 ⇒ **cards.db 一毁，回收站跟着毁**，此时唯一保险就是快照。P8 已验证快照确实能在库层面回退（S1 恢复后 card_777 消失）。

---

## 四、处置优先序（建议，本轮不改代码）

| 级别 | 缺口 | 处置 | 判定依据 |
|---|---|---|---|
| **P0-1** | 指纹盲区（§3.2）导致"改了但没备份"，且**静默** | 指纹改成内容哈希（或 `size+mtime_ns`），并让"跳过"在日志/状态里可见 | 唯一一条会**静默丢数据**的路径 |
| **P0-2** | 覆盖只有 works/（§3.1），一半用户产物无保险 | 要么把 `trading-workspace` 等纳入 `PROTECTED_DIRS`，要么在文档/UI 明示"这些不保" | 实测删了就真没了，无第二条通道 |
| **P1-1** | 保留策略按条数（§3.6），窗口可能只剩 1 小时 | 改"时间窗 + 条数"双阈值（如"至少保留最近 24h 的全部批次"） | work_013 一小时刷满 194 批 |
| **P1-2** | 恢复 CLI 指错根（§3.7）+ 无 API（§3.9） | 恢复根从 `data_snapshot.SNAPSHOT_ROOT` 派生；补一条"当前实例快照/恢复"入口（先做只读 list + 明确提示，再谈写） | 沙箱实例实测 0 批次可见 |
| **P1-3** | 整批恢复只补不删（§3.3） | 恢复时给显式 `--mirror`（清掉批次外文件）选项，默认打印"将保留 N 个多余文件" | 混合态会让人误以为已回滚 |
| **P2-1** | 无整实例还原点（§3.4）、日备非日终（§3.5） | 拍一份"跨 work 一致性批次"（同一 stamp 下拍全部 work）+ 日备在当天最后一次写后刷新 | 事故通常跨 work |
| **P2-2** | 恢复后 `journal_mode=delete`（§3.8） | 恢复流程末尾显式 `PRAGMA journal_mode=WAL`（或恢复后强制重开连接） | P8/P10 实测 |
| **P2-3** | 批次无清单/校验（本轮未测，仅记录） | 每批落 `manifest.json`（文件清单 + sha256），恢复前校验 | 待第二轮验证 |

**本轮未验证（留给第二轮，勿当结论）**：活进程内存态（画布缓存、goal driver、事件读取位点）热恢复后是否必须重启；200 批次上限真实触发时的修剪行为（当前 `_ops` 199 条，尚未越界）；恢复与并发写的竞态。
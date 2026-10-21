# verify/5713/experiments/b06 —— B-06「快照四病」数据域实测（入册说明）

- **来源**：card_015（数据域·5713下一批修复）在沙箱 `...\.worktrees\5713_sandbox\experiments\b06\` 跑出的原件，
  按 D09 纪律（实验产物必须落在 git 内）从沙箱同步入册；沙箱只留可重建的工作残留。
- **代码版本**：`f822911`（5713 分支 = dev）。本轮**未改任何产品代码**。
- **文件**：
  - `REPORT_b06_snapshot_four_diseases.md` —— 判定与修序（对外结论）
  - `b06_probe.py` —— 驱动器（T1–T10，可复跑；只 monkeypatch 模块级路径常量到沙箱）
  - `evidence_b06.json` —— 机械证据（每次运行的原始观测值）
- **复跑**：
  ```
  cd D:\AI_station\推演agent\.worktrees\5713_sandbox\experiments\b06
  set PYTHONIOENCODING=utf-8
  D:\AI_station\推演agent\.venv\Scripts\python.exe b06_probe.py
  ```
  约 15 秒，自建沙箱根、自清 T9 探针痕迹。
- **边界**：生产快照根 `D:\AI_station\推演agent_data_snapshots` 与 5700 活数据零触碰；
  T9 用仓库自带 CLI 真跑时写进了 `.worktrees\5713\works\work_b06probe\`（平行树，正是病4 的证据），
  跑后已删并核对活实例无残留。
## 追加（第二轮）：R10 复核与唯一裁决素材

- `SECTION_数据域_供唯一裁决并入.md` —— **素材，不是裁决**。收口方整段粘进
  `docs/5713-下一批修复方案裁决-20261020.md` 的数据域一节即可。
- `REPORT_...md` 新增 **§7**：质量域移交的 `test_snapshot_restore_roundtrip` 红复核，判定为**真坏**
  （`ok=True` 但 stale `-wal` 重放，未真正回滚），并据此**修正** §3「能恢复」的口径（限非 WAL／无旁挂场景）。
- 探针（均在沙箱真跑，产物 `evidence_r10*.json`）：
  - `r10_restore_ok_but_no_rollback.py` —— 复现红 + 两条修法候选验证（FIX_A 删旁挂带重试／FIX_B sqlite backup API，两条均实测能真回滚）
  - `r10_holder_forensics.py` —— 告警瞬间抓现场（`card_db._conns`/`_read_conns` 皆空仍 WinError 32）
  - `r10_conn_forensics.py` —— 全进程 `sqlite3.connect` 挂钩测活（告警瞬间零活连接仍 WinError 32）
- 复跑：`cd .worktrees\5713_sandbox\experiments\b06 && set PYTHONIOENCODING=utf-8 && ..\..\..\..\.venv\Scripts\python.exe r10_probe_v5.py`
  （`r10_probe_v3.py` / `r10_probe_v4.py` 同法；三个脚本都自建沙箱根、不碰生产数据）

## 追加（第三轮）：修法B 的落盘形态与验收判据（card_014 复现提醒 → 本域 v6 实测）

- `r10_fixB_sidecar_form.py`（沙箱原件名 `r10_probe_v6.py`）—— 故意留一条活连接贯穿修复过程，
  量「修法B 之后 `-wal`/`-shm` 到底在不在」与「三条读路径各读到哪一版」。
  产物 `evidence_r10_v6.json`。
- **结论（进报告 §7.7）**：修法B 之后 `-wal` **仍存在、被 checkpoint 截断为 0 字节（无待重放页）**，
  `-shm` 也还在；本项目里**「旁挂不存在」不能当验收判据**（同一现场它判不通过、而「读回快照态」判通过）。
  正确判据＝`card_store.read_card` 读回快照态 ＋ 独立只读连接同一状态 ＋ `-wal` 无待重放页 ＋ 回执诚实。
- 复跑：`cd .worktrees\5713_sandbox\experiments\b06 && set PYTHONIOENCODING=utf-8 && ..\..\..\..\.venv\Scripts\python.exe r10_probe_v6.py`


## 追加（第四轮）：旁挂存灭的真变量——关闭顺序 × 最后关闭者的写权限

起因：card_014 报「我（全关后仍在）／card_016（关了就消失）／它自己（剩几个连接）」三方不一致。
我不采信任何一方，自测三轮（v7→v8→v9），结论是**「还剩几个连接」这个归因不成立**。

- `r10_sidecar_alive_conncount.py`（原件 v7）—— 排掉「连接数」：写＋只读＋探针全关、gc 后旁挂**仍在**。
- `r10_sidecar_rw_vs_ro.py`（原件 v8）—— 找到候选变量：全是读写连接则消失；有 `mode=ro` 只读连接碰过则留。
- `r10_sidecar_close_order.py`（原件 v9）—— **单变量定案（只改关闭顺序）**：
  O1 只有读写、关它 → 消失／O2 先关只读再关读写 → 消失／**O3 先关读写（只读还在）再关只读 → 仍在**／
  O4＝O3 之后再开读写连接读一次并关掉 → 消失。
  ⇒ **规则：删 `-wal`/`-shm` 需写权限；最后关闭的若是 `mode=ro` 只读连接，它删不掉，文件留着，
  直到下一次读写连接干净关闭才被清掉。**
- 证据：`evidence_r10_v7.json` / `_v8.json` / `_v9.json`；完整推理与三条施工含义见报告 **§7.8**。

**对我自己 §7.7 的更正**：原文「关掉活连接后旁挂也不消失 ⇒ 连事后补查都不成立」是把单次形态当了通则，已改写。
**溯源**：v6 的 S3 之所以「全关后仍在」，是因为 `card_db.reset_caches()` 先关 `_conns`（读写）、后关 `_read_conns`
（`mode=ro`，`card_db.py:477-493`）⇒ 最后关闭的必然是只读连接，正落在 O3——是产品读路径的常态，不是偶然。

**探针自查**：v9 首跑 `VERDICT` 判 `rule_holds=false`，是我取错字段（O4 取了 reopen 之前的现场）；修后四序全中。
教训：探针自己也是现场的一部分，布尔结论必须回指到它读的那个字段。

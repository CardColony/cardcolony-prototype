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

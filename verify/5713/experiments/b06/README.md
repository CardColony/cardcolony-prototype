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
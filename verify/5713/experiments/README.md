# verify/5713/experiments · 实验交付物存档（入版本控制）

本目录是**从 5713 沙箱抢救进 git 的实验交付物**。放在这里的原因是：沙箱
（`.worktrees/5713_sandbox/`）在仓库之外，git 完全够不到——里面的东西一旦沙箱重建就没了。

## 为什么有这个目录（2026-10-20）

第一轮涌现实验（跑在 5700 树上的 card_008/009/010）的三份交付物，提交前**全部游离在版本控制之外**：

| 卡 | 产物 | 原来的状态 |
|---|---|---|
| card_008 路径与模块边界 | `boundary_scan.py` / `boundary_scan_5bfc537.json` / `docs/5713-边界审计-第一轮-20261020.md` | untracked 散文件（共享 worktree 里） |
| card_010 测试可信度 | `test_credibility_r1.md` / `_flake_ev/`（31 文件） | untracked 散文件 |
| card_009 数据快照可信度 | `REPORT_snapshot_credibility.md` / `evidence.json` / `completeness.json` / 两个驱动器 | **在沙箱里，git 够不到** |
| card_006 拆分方法研究 | 结项报告 / 5 份 evidence JSON / 3 版驱动器 / 2 个分析器 | **在沙箱里，git 够不到** |

⇒ 任何人跑一次 `git clean`、或 worktree 重建、或沙箱清理，第一轮实验证据链即不可恢复。
这不是某张卡的问题——是**没有规定「产物必须落在哪」**的机制缺口。

## 目录内容

```
verify/5713/experiments/
├── snapshot_credibility/          card_009 快照与恢复可信度（真删真恢复实验）
│   ├── REPORT_snapshot_credibility.md   判定报告
│   ├── evidence.json                    P0–P12 阶段原始观测值
│   ├── completeness.json                批次完整性对账
│   ├── snapshot_credibility_exp.py      实验驱动器（自建实验根，可复跑）
│   └── snapshot_completeness_audit.py   完整性对账脚本
└── ask_once/                      card_006 根卡正确拆分方法研究（四臂九样本）
    ├── 结论-问一次与预审闸门-20261020.md   结项报告（含复跑命令）
    ├── evidence_ask_once.json / evidence_v2..v5.json   四臂证据
    ├── books_score_all.json 等         任务书判据打分汇总
    ├── analysis_v2.json / analysis_v3.json   注入后动作序列
    ├── ask_once_experiment{,_v2,_v3}.py     三版驱动器
    └── analyze_arm.py / score_books.py      两个分析器
```

## 沙箱里还有什么（未入 git，属工作残留）

`.worktrees/5713_sandbox/experiments/` 下还有大批**实验工作数据**：5 轮 pytest 原始
输出、`__pycache__`、console 日志、`snapshot_credibility/{base,snapshots,pre_restore_backups}/`
（含 `card_999.md` 等测试样本）。这些是复跑的中间产物，**不入 git**；要复现就直接重跑驱动器。

## 纪律（写给下一轮）

1. **实验产物必须落在 git 内**（`verify/5713/` 或 `docs/`）。沙箱只放可重建的工作残留。
2. 报告引用的路径必须是**入册后的路径**，不是沙箱路径——否则引用会随沙箱清理失效。
3. 提交前先 `git status --porcelain`：`??` 的一律先入册再往下走。
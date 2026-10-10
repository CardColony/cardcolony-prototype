# 迭代一·会话底座 — 部署窗口检查单（S9 交付件）

> 依据：《迭代一会话底座施工任务书》p4b 终稿 · 施工件 S9。
> 适用：5700/5701 双服务生产实例的 V2 格式上线窗口。**六步程序，顺序不可
> 调换；每步有硬判据，判据不过＝停下回报，不得带病进入下一步。**
> 配套脚本：`scripts/backup_works.py` ／ `scripts/migrate_sessions_v1_to_v2.py`
> ／ `scripts/crash_drill_session.py`。

## 第 1 步 · 停写

- 5700 与 5701 双服务全部停止（进程级确认，非仅前端不可达）。
- 硬判据：`works/` 树 mtime 稳定 60s 无变化（`find works -newer <标记文件>` 为空）。

## 第 2 步 · 冷备份

- `python scripts/backup_works.py --dest <E盘备份目录> --sample 30`
- 硬判据：退出码 0；manifest.json 的 `md5_match == 文件总数`；抽验不一致＝0。
- **备份失败＝窗口终止**（回滚基础不存在就不能动生产）。

## 第 3 步 · 副本迁移演练（不上生产）

- 从第 2 步备份目录复制一份演练副本。
- `python scripts/migrate_sessions_v1_to_v2.py --target <演练副本> `（dry-run）
  → 退出码 0、失败＝0；核对清单：转换数＋跳过数＝总会话文件数。
- `--apply` 真写演练副本 → 重跑一次（幂等验证）：第二次转换＝0、跳过＝全部。
- 水位核对：`_migration_report.json` 中每条 `condense_mark` 与源
  `memory/{card}.json` 的 mark 相等；`read()[mark:]` 切片等价已由脚本内断言。
- 崩溃演练：`python scripts/crash_drill_session.py --target <演练副本> --work work_001`
  → verdict=PASS（撕裂检出→旁路留档→重建→续写 seq 接续）。
- **硬判据：dry-run/apply/幂等/水位/崩溃五项全过才准进第 4 步。**

## 第 4 步 · 生产迁移

- `python scripts/migrate_sessions_v1_to_v2.py --target <生产works> --apply`
- 硬判据：退出码 0（any-fail-abort：任何失败＝零接受，清单 `_migration_report.json`
  落在生产树内备查）；转换数＝第 3 步清单数。
- 失败处置：按清单修复后**重跑**（幂等，已转换文件自动跳过）。

## 第 5 步 · 启动验证

- 双服务启动 → 冒烟面（生产端口手测）：
  1. `GET /api/works/{w}/cards/{c}/session` 返回门面块（含 seq/actor/turn）；
  2. `POST .../messages` 一轮对话 → 会话文件首行 `---SESSION---`；
  3. `POST .../stop`（无在途）→ `{"stopped": false, "idempotent": true}`；
  4. 同卡双 POST → 第二个 409；
  5. 导航活跃度排序正常（activity 走门面 scan_tail）。
- 硬判据：五项冒烟全过。

## 第 6 步 · 回滚预案（只在第 4/5 步失败时执行）

- 双服务停写 → 删除生产 `works/`（此为唯一允许的删除：窗口内新数据为零业务
  价值）→ 从第 2 步备份整树恢复 → md5 抽验（backup manifest 对比）→ 回到第 1 步。
- 硬判据：恢复后 manifest 比对一致；**回滚本身也要留痕**（操作人＋时间＋原因）。

## 附：本窗口交付的证据文件

| 证据 | 位置 |
|------|------|
| 备份清单 | `<备份目录>/manifest.json` |
| 迁移清单 | `<works>/_migration_report.json`（演练副本与生产各一份） |
| 崩溃演练 | `<演练副本>/_crash_drill_report.json` |
| 演练总报告 | `D:\AI_station\底座差距研究\迭代一-A9-演练输出-20261010.md` |

# role 存量数据保全档（role_archive）

本目录为**删除旧 role 路径前的数据保全**。删除动作（`role_store.py` / `role_room.py` /
`role_*` 五张表 / `/roles` 端点）一旦执行，生产库里以下数据将永久消失。本目录把有数据的
work 全部导出留档，入 git 后可回溯。

## 为什么必须保全

`work_018` 的 `role_verdicts` 表有 **26 条**涌现观察员（`card_011`）的判断记录——这是
"涌现观察员"课题研究的**唯一原始原料**（decision / reason / sources / targets /
evidence / auto / superseded_by 全部字段）。一旦删表，这 26 条判断及其理由链就永久消失，
课题不可复现。`role_profiles`（角色章程/职责 JSON）、`role_sync`（28 条同步记录）、
`role_alarms`、`role_charter_history` 同理，均为旧 role 机制下的真实运行数据。

## 文件清单

| 文件 | 内容 | 来源 |
|---|---|---|
| `role_archive_work_018.json` | work_018 全部 role 数据 | `D:\AI_station\推演agent\works\work_018\cards.db` |
| `role_archive_work_022.json` | work_022 全部 role 数据 | `D:\AI_station\推演agent\works\work_022\cards.db` |
| `row_counts_summary.json` | 两库各表行数统计摘要 | 同上两库 |
| `export_role_archive.py` | 本次导出用脚本（只读） | 本目录 |
| `_pre_state.json` | 导出前生产库文件状态（mtime/大小），用于核验导出未改动生产库 | 本目录 |

导出时间：2026-10-22（见各 JSON 内 `exported_at` 字段，精确到秒）。

## 导出方式与 sqlite 查询

所有连接一律只读：`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`，
**无任何 INSERT/UPDATE/DELETE/建表**。导出脚本为 `export_role_archive.py`。

每个 work 的 JSON 结构：

- `role_cards` —— role 卡元信息，来自 `cards` 表：
  ```sql
  SELECT card_id, title, card_type, parent, created_at
  FROM cards WHERE card_type='role' ORDER BY card_id
  ```
- `role_tables.<table>.rows` —— 各 role_* 表全量行（逐列原样 dump）：
  ```sql
  SELECT * FROM role_profiles;
  SELECT * FROM role_verdicts ORDER BY id;      -- 按 id 排序，一条不漏
  SELECT * FROM role_alarms ORDER BY id;
  SELECT * FROM role_sync;
  SELECT * FROM role_charter_history ORDER BY id;
  SELECT * FROM role_directives ORDER BY id;    -- 表存在但两库均为 0 行
  ```
  （`role_directives` 表两库均存在，行数为 0。）
- `role_related_inbox_messages` —— 与 role 卡相关的收件箱消息：
  ```sql
  SELECT * FROM inbox
  WHERE target_card_id IN (<role 卡 id>) OR from_card IN (<role 卡 id>)
  ORDER BY seq
  ```
  **结果：两库均为 0 条**。`inbox` 表结构（`from_card` / `target_card_id`）允许按收发方
  匹配，但 role 卡（work_018 的 `card_011`、work_022 的 `card_003`/`card_004`）从未作为
  消息收发方出现在 inbox 中（work_018 全部 107 条、work_022 全部 0 条均不匹配）。
  *备注（非判据）：work_018 有 6 条 inbox 消息的 content 文本提及 "card_011"，属于话题
  提及而非收发方，未纳入导出。*
- `row_counts` —— 本 work 各表导出行数。

## 行数统计摘要（供核对完整性）

| 表 | work_018 | work_022 |
|---|---|---|
| role_cards | 1 | 2 |
| role_profiles | 1 | 2 |
| role_verdicts | **26** | 0 |
| role_alarms | 1 | 0 |
| role_sync | 28 | 0 |
| role_charter_history | 4 | 2 |
| role_directives | 0 | 0 |
| role_related_inbox_messages | 0 | 0 |

其余 20 个 work 的五张 role_* 表全空，未导出（无数据可保）。

## 数据保全判据（导出时实测）

1. **生产库未被修改**：导出前后 work_018 / work_022 的 `cards.db`、`cards.db-wal`、
   `cards.db-shm` 文件大小全部一致；其中 work_018 的 `cards.db-wal` mtime 在导出窗口内
   有变化，但经隔离实验验证（连续 5 次纯只读 SELECT，WAL 的 mtime 与内容 sha256 完全
   不变），该变化来自生产库自身活跃运行（5713 实例在持续写 WAL），与本次只读导出无关。
2. **行数逐表相等**：导出的 JSON 内各表行数 = 库内直查行数（见 `row_counts_summary.json`，
   逐表核对通过，尤其 `role_verdicts`=26）。
3. **26 条判断一条不漏**：`role_archive_work_018.json` 内 `role_verdicts` 的 id 列表
   与库内 `SELECT id FROM role_verdicts ORDER BY id` 逐条相等、顺序一致（26 条）。
4. **内容可读**：verdict 含完整 reason / sources / targets / evidence 原文。
5. git status 仅含本目录新增文件。

## 删除 role 路径后这些数据会丢什么（一句话结论）

`role_store.py` / `role_room.py` / `role_*` 五张表 / `/roles` 端点删除后，work_018 的
26 条涌现观察员判断簿（唯一原始原料）+ 1 份角色档案 + 28 条同步记录 + 4 条章程历史，
以及 work_022 的 2 份角色档案 + 2 条章程历史，将全部永久消失——本目录即为此保全。

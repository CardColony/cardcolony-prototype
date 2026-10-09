# THINK-ECO-permgmt · T2/R3 等价验证报告（2026-10-09 00:10）

> 任务书《权限管理_落地方案》可实现化 · T2（红线 R-B 施工先决）· 验收项 **V7**
> 上游：方案终稿（SHA256 C5C8AE96）§4.2 · 对账底册 PM-22
> 结论先行：**`_is_in_subtree`（新，`src/permissions/policy.py`）与现行 `card_tools.can_edit`（`src/api/card_tools.py` L877）严格等价成立——27,605 对判定，破例 0。**

## 一、判据（任务书 T2 逐字）

> 写测试对比 `_is_in_subtree`（新）与现行 `can_edit`（L877）对同一组卡树的全部判定结果，**逐例相等才算严格等价**（R3）。验证报告（对照表）落盘 `docs/exploration/THINK-ECO-permgmt-R3-等价验证-<ts>.md`，作 T3 可否删函数的前置证据。

## 二、对照对象

| 函数 | 位置 | 语义 |
|---|---|---|
| `can_edit(current_id, target_id, cards)` | `src/api/card_tools.py` L877（实读核验，与任务书锚一致） | 自指直真 → 沿 target 的 `parent` 链上溯找 current；visited 防环、悬空 parent 断链、`guard<1000` |
| `_is_in_subtree(actor_id, target_id, cards)` | `src/permissions/policy.py`（T1 新建） | 遍历口径与上者逐字同源（原方案 §3.2 的 dict 形态落点修正版） |

`cards` 形态双方一致：`{card_id: meta dict}`，只读 `parent` 字段（与工具层 `_collect_card_meta` 产物同形）。

## 三、夹具四族与结果

| 族 | 内容 | 判定对数 | 破例 |
|---|---|---|---|
| A 手工边界树 | 单卡无父／直子旁支／血统上级／悬空 parent／自环／二环／三环带出口／parent 键缺失／chain 指向不存在 current／不存在 target，每棵全部 (actor, target) 有序对（含不存在的 `card_999` 互配） | 121 | 0 |
| B 深链穿 guard | 1501 节点链，root 视角 depth 0/1/998/999/1000/1001/1500 逐点 | 7 | 0 |
| C 定种子随机林 | seed=20261008 × 400 棵（1–12 卡，父随机含空/自指成环/悬空），每棵全有序对 | 27,477 | 0 |
| **合计** | | **27,605** | **0** |

### 附带发现（等价成立的前提下，把 can_edit 既有语义钉进报告）

1. **guard 边界**：`return True` 发生在 guard 自增之后、循环条件复检之前 ⇒ 深链 depth=1000 仍判真、1001 起判假（非 999/1000）。两函数同口径。
2. **自指即真不查存在性**：`can_edit(x, x)` 在 x 不存在于 cards 时也返回 True（首分支先行）。新函数同。
3. **悬空 parent 字面命中 current 判真**：target 的 parent 链上出现与 current_id 相同的字符串（哪怕该 current 已不在 cards，如已删卡）即放行——can_edit 既有语义，逐字继承，未修正（修正它不属于本任务书）。

## 四、逐例对照表（族 A/B 全量、族 C 汇总）

由 `_t_perm_r3_table.py`（仓库根，机械枚举脚本）产出，原件 `_t_perm_r3_table_out.txt`：

全量 137 行对照输出落盘于仓库根 `_t_perm_r3_table_out.txt`（族 A 121 行逐例＋族 B 7 点＋族 C 汇总 27,477 对）。关键抽行：

| 树 | actor → target | can_edit | _is_in_subtree | 等价 |
|---|---|---|---|---|
| 直子与旁支 | card_001 → card_004 | True | True | ✅ |
| 直子与旁支 | card_004 → card_001 | False | False | ✅ |
| 血统上级（子树外） | card_003 → card_001 | False | False | ✅ |
| 悬空 parent | card_999 → card_002 | True | True | ✅（悬空链字面命中 current） |
| 二环 | card_002 → card_001 | True | True | ✅（环成员互含，两边同判） |
| 三环带出口 | card_004 → card_001 | False | False | ✅ |
| 深链 depth=1000 | chain_0000 → chain_1000 | True | True | ✅ |
| 深链 depth=1001 | chain_0000 → chain_1001 | False | False | ✅ |
| 随机林汇总 | 400 棵全有序对 | — | — | ✅ 27,477 对 0 破例 |

## 五、结论与施工先决状态

1. **R3 严格等价成立**（判据「逐例相等」满足：27,605/27,605）。
2. 红线 R-B 的「T2 验证通过后删 `can_read`/`can_edit`」一侧由此**解锁**。
3. 但 T3（删函数＋7+1 处改接 `check`/`check_with_scope`）与 T4/T5/T6 的生效语义全部取决于 **POLICY 表值**——表值受红线 R-D 冻结（PM-27/PM-28 待用户裁），故删除与改接不与验证同批执行，停报候裁。

## 六、证据件与复现

| 件 | 路径 |
|---|---|
| 等价测试（pytest，6 条） | `tests/test_perm_r3_equivalence.py` |
| 对照表机械枚举脚本 | `_t_perm_r3_table.py` |
| 对照表全量输出 | `_t_perm_r3_table_out.txt`（族 A 121 对＋族 B 7 点＋族 C 27,477 对） |
| 被对照的新函数 | `src/permissions/policy.py` |

复现：

```
cd D:\AI_station\推演agent
.venv\Scripts\python.exe -m pytest tests/test_perm_r3_equivalence.py -v   # 6 passed
.venv\Scripts\python.exe _t_perm_r3_table.py                              # 汇总行：27,605 对，破例 0
```

pytest 实跑记录（2026-10-09 00:08）：

```
tests/test_perm_r3_equivalence.py::test_r3_crafted_forests_pairwise_equivalent PASSED
tests/test_perm_r3_equivalence.py::test_r3_guard_boundary_chain PASSED
tests/test_perm_r3_equivalence.py::test_r3_random_forests_pairwise_equivalent PASSED
tests/test_perm_r3_equivalence.py::test_placeholder_policy_denies_everything PASSED
tests/test_perm_r3_equivalence.py::test_placeholder_unregistered_type_denies PASSED
tests/test_perm_r3_equivalence.py::test_check_with_scope_placeholder_denies_before_scope PASSED
============================== 6 passed in 4.66s ==============================
```

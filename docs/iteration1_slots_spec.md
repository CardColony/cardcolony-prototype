# 迭代一·会话底座 — 六槽规格与对齐决议（S8 交付件）

> 依据：《迭代一会话底座施工任务书》p4b 终稿 · 施工件 S8 ＋
> 《迭代一会话底座方案-链终稿-20261010.md》（sha16=AD028889D5C0A1FC）。
> 本件是**规格声明**：每个槽的「现在是什么样、什么时候填、谁来填」。
> 迭代一铁律：留位不占位——槽位存在，判据内容一件不写（写了就是越权设计）。

## 六槽总表

| 槽 | 载体 | 迭代一状态 | 填充时点 |
|----|------|-----------|---------|
| R1 类型行为注册表 | `config/behavior_registry.json` | **空壳** `{"version":1,"types":{}}` | 迭代二（类型自带完成判据） |
| R2 方案板 | `works/{work_id}/`（会话实例数据） | **对齐决议 A 已裁**（见下） | 迭代二按 A 落地 |
| R3 控制三态 | `src/api/control.py` | **已实现**（STOP/CONTINUE/DEFER_HUMAN） | 迭代一唯一真实来源＝人的手动停 |
| R4 收件位 inbox_ref | V2 文件头字段 `inbox_ref` | **字段已留位**，缺省空串 | 迭代二定义收件语义 |
| R5 过程线持久化 | `config/session_base.json` `persist_process` | **开关已实现**，默认 off | 用户开启即生效，格式不随开关变化 |
| R6 变更留痕通道 | `works/{w}/_ops/revisions.jsonl` ＋ `works/_ops/session_ops.jsonl` | **已实现**（升格/append/体检/处置/人停全留痕） | 持续累积 |

## R1 — 类型行为注册表（空壳）

- 载体：`config/behavior_registry.json`，`{"version": 1, "types": {}}`。
- 消费点：`control.check()`——注册表恒空 ⇒ `check` 恒返回 `CONTINUE`，
  不会返回 `DEFER_HUMAN`；**消费侧路由已实现**（DEFER_HUMAN → 轮末
  `end=policy_stop` 闭合），接口非空置。
- 迭代二填充内容：类型自带的完成判据（「什么算做完」）。迭代一**判据
  内容一件不写**——停续策略的唯一真实来源是人的手动停（T124/X4）。

## R2 — 方案板（对齐决议 A · 代拍留痕）

- **决议**：A——方案板按**会话实例数据**落 `works/{work_id}/`，与工具卡
  剧本库（`toolcard_scripts/`，全局共享）分层；不做全局剧本库挂载。
- **代拍依据**：用户「你直接执行」授权（2026-10-10）＋ 任务书 §2.4 推荐
  口径。**用户可事后推翻**；推翻时本件此条作废、迁移责任在推翻方裁定。
- 迭代一不涉 R2 落地（无方案板读写代码）；本条仅为决议留痕。

## R3 — 控制三态（已实现）

- 接口：`control.check(work_id, card_id) -> STOP | CONTINUE | DEFER_HUMAN`；
  `control.request_stop()` 人停置位（consume-once）；`control.discard_stop()`
  回合收口清扫位。
- 消费点：`message_runner.check_control`（step 入口/出口边界串行消费）——
  STOP → `end_reason="human_stop"`；DEFER_HUMAN → `end_reason="policy_stop"`。
- **共存禁令（§5-10）**：不得以 `toolcard_driver.on_step_boundary` 返回值为
  停的依据——「看到了进度」≠「按进度判停」。两调用串行、各司其职、互不解释。
- 端点：`POST /works/{work_id}/cards/{card_id}/stop`；无在途回合＝幂等 no-op
  （T123）；置位在回合窗口内有效、回合结束未消费即作废（T124 不误伤下回合）。

## R4 — 收件位（字段留位）

- 载体：V2 会话文件头 `inbox_ref:` 行（`session_facade._render_v2_header`
  写位 ＋ `_parse_v2_header` 读位），缺省空串。
- 迭代一**不定义**收件语义、不产生读写流量；字段存在即为契约（前向兼容
  白名单内字段）。

## R5 — 过程线持久化（开关已实现，默认 off）

- 载体：`config/session_base.json` 的 `persist_process`（缺省/损坏＝off）。
- 行为：on 时主 journal 帧逐帧追加落 `works/{work}/process/{card}.jsonl`，
  帧形态与 journal 帧逐字一致（格式不随开关变化，T145）；off＝零落盘。
- 运维拨动：`event_stream.set_persist_process(on)`（进程内即时生效，重启
  回读配置）。

## R6 — 变更留痕通道（已实现）

- `works/_ops/session_ops.jsonl`：会话操作五键流水（ts/action/work/card/
  detail）——append/upgrade/health_check/damage_repair/mutex_reject/
  human_stop 全留痕；fail-open（写失败仅 warning，不阻断主流程）。
- `works/{work}/_ops/revisions.jsonl`：实体级变更台账（entity/change/detail）
  ——V1→V2 现场升格首笔真实消费（T114）。

## 附：对齐决议记录格式（供迭代二沿用）

每条决议必须含：决议内容 ＋ 选项空间（A/B/…各是什么）＋ 裁定人 ＋ 裁定
时间 ＋ 依据 ＋「可否事后推翻」。本件 R2 条目即格式样例。

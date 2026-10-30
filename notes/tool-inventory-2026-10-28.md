# 工具统计（2026-10-28）

**口径**
- 用量 = work_027 全部 `tool_call` 事件里该工具的调用次数（`cards.db` session_blocks，只读统计）。
- 可见面 = 在**临时沙箱**（tmp 目录，不碰生产）里建一套卡，逐个调 `build_tool_schemas` 实算。
  口径：project=根卡 / sub_project=作业卡（topic 同支）/ coder / rule / function（职能回合）。
  **`—` 表示"本次沙箱没触发"，不等于谁都看不见**（如 op 回合、auto 卡、末段按目录追加的那几件）。
- 落点/唤醒取自 `ToolDoc.effect`（声明与实现同源，由 `test_tool_doc_facts` 核）。

**总量**：注册 **69** 件；有用量 **56** 件、零用量 **13** 件；另有 **18 个 handler 未注册**（模型看不见）。
本轮已退役 6 件（`wake_card`/`scene_report`/`send_card_message`/`read_inbox`/`issue_directive`/`verify_directive`）。

---

## 一、全部 69 件（按用量降序）

| 工具 | 开关 | 用量 | 只读 | 可见面 | ToolDoc | 落点 | 唤醒 |
|---|---|---|---|---|---|---|---|
| `read_card` | read | 2583 | 是 | project/sub_project/coder/rule/function | 有 | none | 否 |
| `write_ledger` | read | 2244 | 否 | project/function | 无 | — | — |
| `bash` | tools_file | 1709 | 否 | coder | 无 | — | — |
| `read_file` | tools_read_file | 1220 | 是 | project/sub_project/coder/function | 无 | — | — |
| `glob_files` | tools_read_file | 880 | 是 | project/sub_project/coder/function | 无 | — | — |
| `list_files` | tools_read_file | 741 | 是 | project/sub_project/coder/function | 无 | — | — |
| `update_shadow` | shadow_write | 623 | 否 | project/sub_project/coder/function | 无 | — | — |
| `search_files` | tools_read_file | 556 | 是 | project/sub_project/coder/function | 无 | — | — |
| `sync` | read | 459 | 否 | project/sub_project/function | 有 | session_block | 否 |
| `record_verdict` | read | 379 | 否 | project/function | 无 | — | — |
| `append_ruling` | edit | 266 | 否 | — | 无 | — | — |
| `update_status` | edit | 244 | 否 | — | 无 | — | — |
| `edit_card` | edit | 229 | 否 | project/sub_project/coder/function | 无 | — | — |
| `read_shadow` | read | 222 | 是 | project/sub_project/coder/rule/function | 无 | — | — |
| `curate_face` | edit | 205 | 否 | project/function | 无 | — | — |
| `raise_alarm` | read | 186 | 否 | project/function | 无 | — | — |
| `python` | tools_file | 178 | 否 | coder | 无 | — | — |
| `report_up` | read | 153 | 否 | sub_project | 有 | session_block | 是 |
| `direct` | read | 147 | 否 | project/sub_project/function | 有 | session_block | 是 |
| `read_linked` | read_linked | 111 | 是 | project/sub_project/coder/function | 无 | — | — |
| `read_memory` | read | 93 | 是 | project/sub_project/coder/rule/function | 无 | — | — |
| `create_coder_card` | create_child | 70 | 否 | project/sub_project/function | 无 | — | — |
| `write_file` | tools_file | 64 | 否 | coder | 无 | — | — |
| `link_cards` | link | 63 | 否 | project/sub_project/function | 无 | — | — |
| `silence_card` | read | 55 | 否 | project/sub_project/function | 无 | — | — |
| `read_archive` | read | 44 | 是 | project/sub_project/coder/rule/function | 无 | — | — |
| `correct_alarm` | read | 42 | 否 | — | 无 | — | — |
| `report_to_user` | read | 41 | 否 | project/function | 无 | — | — |
| `create_card` | create_child | 36 | 否 | project/sub_project/function | 无 | — | — |
| `get_goal` | plan | 33 | 是 | — | 无 | — | — |
| `stop_card` | read | 30 | 否 | project/sub_project/function | 无 | — | — |
| `delete_card` | delete | 28 | 否 | sub_project | 无 | — | — |
| `edit_file` | tools_file | 23 | 否 | coder | 无 | — | — |
| `web_search` | tools_web | 21 | 是 | project/sub_project/coder/rule/function | 无 | — | — |
| `link_rel` | link_rel | 15 | 否 | project/sub_project/function | 有 | none | 否 |
| `fetch_web` | tools_web | 14 | 是 | project/sub_project/coder/rule/function | 无 | — | — |
| `list_functions` | read | 14 | 是 | project/function | 无 | — | — |
| `update_goal` | plan | 13 | 否 | — | 无 | — | — |
| `set_child_mission` | create_child | 12 | 否 | project/sub_project/function | 无 | — | — |
| `list_manuscript` | manuscript | 7 | 是 | project/sub_project/function | 无 | — | — |
| `archive_card` | delete | 7 | 否 | sub_project | 无 | — | — |
| `unlink_cards` | unlink | 6 | 否 | project/sub_project/function | 无 | — | — |
| `unlink_rel` | unlink_rel | 5 | 否 | project/sub_project/function | 无 | — | — |
| `continue_task` | read | 4 | 否 | project/sub_project/function | 无 | — | — |
| `update_function_profile` | create_child | 4 | 否 | — | 无 | — | — |
| `read_manuscript` | manuscript | 3 | 是 | project/sub_project/function | 无 | — | — |
| `confirm_with_user` | read | 3 | 否 | project/function | 无 | — | — |
| `create_function` | create_child | 3 | 否 | — | 无 | — | — |
| `report_done` | tools_file | 2 | 否 | coder | 无 | — | — |
| `delete_file` | tools_file | 2 | 否 | coder | 无 | — | — |
| `stage_open` | read | 2 | 否 | project/sub_project/coder/rule/function | 无 | — | — |
| `unarchive_card` | delete | 2 | 否 | sub_project | 无 | — | — |
| `search_manuscript` | manuscript | 1 | 是 | project/sub_project/function | 无 | — | — |
| `draft_rule_edit` | read | 1 | 是 | project/function | 无 | — | — |
| `resume_child` | plan | 1 | 否 | — | 无 | — | — |
| `move_file` | tools_file | 1 | 否 | coder | 无 | — | — |
| `edit_note` | shadow_write | 0 | 否 | project/sub_project/coder/function | 有 | shadow | 否 |
| `edit_shadow` | shadow_write | 0 | 否 | project/sub_project/coder/function | 有 | shadow | 否 |
| `propose_status` | read | 0 | 是 | — | 无 | — | — |
| `create_goal` | plan | 0 | 否 | — | 无 | — | — |
| `rename_file` | tools_file | 0 | 否 | coder | 无 | — | — |
| `reparent_card` | edit | 0 | 否 | project/sub_project/function | 无 | — | — |
| `open_review` | create_child | 0 | 否 | project/sub_project/function | 无 | — | — |
| `approve` | read | 0 | 否 | project/function | 无 | — | — |
| `set_criteria` | edit | 0 | 否 | project/sub_project/function | 无 | — | — |
| `revoke_approve` | edit | 0 | 否 | project/function | 无 | — | — |
| `decide` | edit | 0 | 否 | project/function | 无 | — | — |
| `function_watch_cards` | link | 0 | 否 | — | 无 | — | — |
| `set_function_duty` | create_child | 0 | 否 | — | 无 | — | — |

---

## 二、零用量 13 件，分三种性质

| 工具 | 性质 | 判据 |
|---|---|---|
| `edit_shadow` / `edit_note` | **新工具，还没机会用** | 10-27 才加，5700 未重启 |
| `open_review` / `approve` / `set_criteria` / `revoke_approve` / `decide` | **新机制，生产没跑过** | 伴生评审卡 10-27 才建，只有沙箱验证 |
| `create_goal` / `propose_status` / `resume_child` | **机制在，没触发** | 自动模式长目标 / op 回合 / 恢复子卡，条件未出现 |
| `rename_file` / `reparent_card` | **真没用过** | 改名（move_file 的特例）／换父 |
| `function_watch_cards` / `set_function_duty` | **目录预设接管后没人调** | 职能由 catalog 定义，不再手工设 |

## 三、真重叠（模型会挑错）

| 组 | 成员 | 重叠点 |
|---|---|---|
| 影子写入 | `update_shadow`(623) / `edit_shadow`(0) / `edit_note`(0) | 都是写影子；update_shadow 是整块覆盖（事故：45,935→351 字），schema 自己写着"优先用 edit_shadow" |
| 改名 | `rename_file`(0) / `move_file`(1) | 同目录改名＝移动的特例 |

## 四、注册表外的 18 个 handler（模型看不见）

**本轮退役留的 6 个**：`_h_send_card_message`、`_h_read_inbox`、`_h_wake_card`、`_h_scene_report`、`_h_issue_directive`、`_h_verify_directive`

**老死的 12 个**：
- 5 个 `_legacy`：`_h_correct_alarm_legacy`、`_h_issue_directive_legacy`、`_h_raise_alarm_legacy`、`_h_record_verdict_legacy`、`_h_verify_directive_legacy`
- `_h_run_tool_card`（全仓零引用，`run_tool_card` 也不在注册表）
- 角色时代的 5 个：`_h_create_role`、`_h_list_roles`、`_h_role_watch_cards`、`_h_set_role_duty`、`_h_update_role_profile`
- `_h_update_goal`（旧版；真正生效的是 `src/goal/tools.py` 的 `h_update_goal`）

## 五、三条死分支

| 位置 | 为什么死 |
|---|---|
| `message_runner.deliver_now` 非 text 分支 | 靠 `take_pending`，而账本自「inbox 取消」后不再有新行 |
| `routes.py` 回合收口调 `deliver_now()`（无 text） | 同上 ⇒ 永远返回 False，排队接续失效 |
| `GET /card-messages` | 读账本 ⇒ 永远空结果（左栏未读角标没数据源） |

## 六、ToolDoc 覆盖

69 件里只有 **7 件**有 `ToolDoc`（`read_card`、`sync`、`direct`、`report_up`、`link_rel`、`edit_shadow`、`edit_note`），
其余 **62 件**只有 `schema.description`——这是「工具结果即引导」那条线没做完的部分。

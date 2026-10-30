# 工具是什么 · 怎么被调用 · 有没有冲突重复（2026-10-28）

**口径**
- **是什么**＝`schema.description` 首句（原文，不转述）。
- **调用情况**＝work_027 全部 `tool_call` 的**参数级**统计（14,582 次调用），不是只数次数。
- **冲突标记**：`【重复】`同一件事两个工具 ／ `【交叉】`语义邻近易选错 ／ `【错位】`开关或可见面与语义不符 ／ `【空转】`机制在但实际没用上。
- 注册 69 件；有用量 56、零用量 13。

---

## A. 读（14 件）

| 工具 | 是什么 | 调用情况 | 冲突 |
|---|---|---|---|
| `read_card` | 读指定卡的标题/父卡/连线/三个内容块 | **2583 次，100% 都带 card_id**（从不读自己——自己三块在 150 区常驻） | 【交叉】与 `read_linked(正文)` 重叠：都是读别人卡面 |
| `read_memory` | 下探某卡已凝练的记忆条目（【结论】/【依据】） | 93 | — |
| `read_shadow` | 读影子卡全文（只读自己的） | 222 | 【交叉】与 `read_linked(part=影子)` 重叠 |
| `read_linked` | 沿通信线读对方卡的正文/影子/对话 | 111：**对话 45 / 正文 43 / 影子 23** | 【交叉】见上两条；「对话」这一档是跨卡读上下文，是"对账"的来源之一 |
| `read_archive` | 读归档记录（归档卡 read_card 查不到） | 44 | — |
| `read_file` | 读文本文件（可按行区间） | 1220 | — |
| `list_files` | 列目录一层 | 741 | 【交叉】三个文件查找 |
| `glob_files` | 按文件名模式匹配路径 | 880 | 【交叉】三个文件查找 |
| `search_files` | 目录内逐文件搜关键词/正则 | 556 | 【交叉】三个文件查找（三件都在用，语义不同，**不建议合并**） |
| `read_manuscript` | 调取原文资料某章 | 3 | — |
| `list_manuscript` | 列原文资料章节目录 | 7 | — |
| `search_manuscript` | 在原文资料里检索 | 1 | — |
| `web_search` | 网页搜索 | 21 | — |
| `fetch_web` | 抓网页正文 | 14 | — |

## B. 写卡面（9 件）

| 工具 | 是什么 | 调用情况 | 冲突 |
|---|---|---|---|
| `edit_card` | **精准编辑**某一块里的一处（old_string→new_string） | 229 次：**整篇覆盖 195 / 列版本 27 / 回退 6 / 精准编辑 1** | 【交叉】与 `update_status`(现状)、`append_ruling`(需求文档) 三条路都能写同一块。**且历史调用 195 次全是整篇覆盖**——精准编辑 10-27 才改，还没在真机验证过 |
| `update_status` | 整块替换「现状」块 | 244 次，每次写 **2100–3400 字** | 【交叉】同 B1；【空转】它是"整块替换"，但每次替换两三千字＝现状块天生是流水账 |
| `append_ruling` | 写「需求文档」块（精准编辑/整块两模式） | 266 次，**256 次文本 <100 字**（短条目） | 【交叉】与 `curate_face` 都写需求文档 |
| `curate_face` | 总裁执笔任意卡的引导块（需求文档/裁定） | 205 | 【交叉】同上 |
| `set_child_mission` | 给子树内的卡设定/修订使命（立项定稿） | 12 | — |
| `update_shadow` | 整块覆盖影子卡（四节） | **623 次，每次 1–4 千字**；schema 自己写"优先用 edit_shadow" | 【重复】与 `edit_shadow`/`edit_note` 同组 |
| `edit_shadow` | 精准编辑影子某一节 | 0（10-27 才加，未重启） | 【重复】同上 |
| `edit_note` | 精准编辑影子的「笔记」节（私有） | 0（同上） | 【重复】同上 |
| `propose_status` | 提议现状新内容（只出草案，用户点同意才写） | 0 | 【交叉】与 `update_status` 都针对现状（提议 vs 直写） |

## C. 通信与线（10 件）

| 工具 | 是什么 | 调用情况 | 冲突 |
|---|---|---|---|
| `sync` | 让对方知道一件事，进上下文、不唤醒 | 459 次：**458 次显式点名 card_ids，仅 1 次走"沿连线"** | 【空转】"缺省沿连线送达"这个设计基本没人用——实际用法是点名发信 |
| `direct` | 下达任务，沿连线送达＋叫起来，验收判据必填 | 147 次：**127 带判据 / 20 不带** | 20 次"下指令没判据"——已改为必填 |
| `report_up` | 完成一段工作，向上一级交代 | 153 次：旧口径 phase 95 / done 34 / ask 18 / blocked 6 | 【交叉】与 `report_done` 都是向上交代（coder 用 done） |
| `report_done` | 编程卡完工收口，回执由系统发给主卡 | 2 | 【交叉】同上；**【错位】挂 `tools_file` 开关**（完工回执挂在"文件读写"上） |
| `report_to_user` | 总裁向用户做中间汇报 | 41 | 【交叉】与 `confirm_with_user` 都面向用户（汇报 vs 要确认） |
| `confirm_with_user` | 总裁向用户发起确认（推定前提必须确认） | 3 | 【交叉】同上 |
| `link_cards` | 建通信线（type：先后/关联） | 63 次：**关联 62 / 先后 1** | 【空转】"先后"这个类型形同虚设 |
| `unlink_cards` | 解通信线 | 6 | — |
| `link_rel` | 建依赖/关联线（不用于通信） | 15 | — |
| `unlink_rel` | 解依赖线 | 5 | — |

## D. 建卡与组织（9 件）

| 工具 | 是什么 | 调用情况 | 冲突 |
|---|---|---|---|
| `create_card` | 建一张子卡 | 36 | 【交叉】三种建卡 |
| `create_coder_card` | 建编程卡并立即开跑 | 70 | 【交叉】同上 |
| `create_function` | 根卡组建一个职能（不占卡号、不进树） | 3 | 【交叉】同上；【空转】职能已由目录预设播种，手工组建基本不用 |
| `update_function_profile` | 更新职能档案（职责书变更新版本） | 4 | — |
| `function_watch_cards` | 调整职能关注对象 | 0 | 【空转】目录预设接管后没人调 |
| `set_function_duty` | 调整职能值班态（active/suspended/archived） | 0 | 【空转】同上 |
| `list_functions` | 列出本项目全部职能 | 14 | — |
| `stage_open` | 开启子卡阶段任务，唤醒职能做立项评审 | 2 | **【错位】挂 `read` 且在基础池 ⇒ 实算可见面里 coder/rule 也拿得到**（"开阶段"按语义是父卡动作） |
| `reparent_card` | 把自己子树内的卡挂到另一张卡下 | 0 | — |

## E. 卡生命周期（6 件）

| 工具 | 是什么 | 调用情况 | 冲突 |
|---|---|---|---|
| `delete_card` | 删卡（根卡/有子卡的不可删） | 28 | 【交叉】与 `archive_card` 都是"让卡消失"（删不可逆 / 归档可恢复） |
| `archive_card` | 归档：卡面收成一条记录，会话与事件原位保留 | 7 | 【交叉】同上 |
| `unarchive_card` | 恢复归档卡为活卡 | 2 | — |
| `stop_card` | 打断（interrupt）或强制停止（halt）子树内的卡 | 30 | 【交叉】与 `silence_card` 都是"让卡别动"（停当前轮 / 完工后静音） |
| `silence_card` | 让完工的卡沉默，或重新激活 | 55 | 【交叉】同上 |
| `continue_task` | 给完工/暂停的编程卡续派下一件任务 | 4 | 【交叉】与 `resume_child`/`stage_open` 都是"让卡接着干" |

## F. 自动模式（6 件）

| 工具 | 是什么 | 调用情况 | 冲突 |
|---|---|---|---|
| `get_goal` | 读当前目标的精确 id/revision/phase/轮次/activation | 33 | — |
| `create_goal` | 建持久目标（跨多轮自主推进） | 0 | 【空转】机制在，没触发 |
| `update_goal` | 更新目标（edit/pause/resume/complete/blocked） | 13 | — |
| `resume_child` | 把停下的子卡目标恢复 active 并点火 | 1 | 【交叉】三个"让卡接着干" |
| `write_ledger` | 职能写自己的专业台账（卡面正本） | **2244（第二高）**，全部带 text，1232 次还带 note | 【交叉】与 `append_ruling` 都是"写正本"（职能台账 vs 卡面第三块） |
| `record_verdict` | 职能每次巡检必须落判断（speak/silent） | 379 次：**speak 371 / silent 8** | 【空转】"沉默也是判断"实际不成立——几乎从不沉默 |

## G. 职能警报与规则（3 件）

| 工具 | 是什么 | 调用情况 | 冲突 |
|---|---|---|---|
| `raise_alarm` | 把严重风险直通用户（带 question/options/recommendation） | 186 | — |
| `correct_alarm` | 更正自己的旧警报 | 42 | — |
| `draft_rule_edit` | 起草规则卡某分区修改（只出草案，用户审批） | 1 | 【交叉】与 `propose_status` 同为"只提议不落盘" |

## H. 执行（7 件）

| 工具 | 是什么 | 调用情况 | 冲突 |
|---|---|---|---|
| `bash` | 执行 shell 命令 | 1709 | — |
| `python` | 执行一段 Python | 178 | 【交叉】与 `bash` 都能跑代码（一个 shell 一个解释器，实际都常用） |
| `write_file` | 写文本文件（覆盖前自动备份） | 64 | — |
| `edit_file` | 精准编辑文件（old_string 必须唯一） | 23 | 【交叉】与 `write_file` 都能改文件（精准 vs 覆盖） |
| `move_file` | 移动文件 | 1 | 【重复】与 `rename_file` |
| `rename_file` | 同目录改名 | 0 | 【重复】改名＝同目录移动 |
| `delete_file` | 删文件 | 2 | — |

## I. 评审（5 件，10-27 新建）

| 工具 | 是什么 | 调用情况 | 冲突 |
|---|---|---|---|
| `open_review` | 为子卡开一个评审批次（评审发生在它的伴生评审卡上） | 0 | — |
| `approve` | 通过本批次（调了就是通过） | 0 | 【交叉】与 `decide(pass)` 都能让批次通过 |
| `set_criteria` | 写/改评审卡判据（版本 +1，已通过作废） | 0 | — |
| `revoke_approve` | 撤销某角色在本批次的通过 | 0 | — |
| `decide` | 裁决批次：pass（一票通过）/ close（解散） | 0 | 【交叉】同上 |

---

# 冲突与重复总表

## 一、真重复（同一件事，必须合并）
| 组 | 成员 | 判据 |
|---|---|---|
| 影子写入 | `update_shadow`(623) / `edit_shadow`(0) / `edit_note`(0) | 都写影子；update_shadow 整块覆盖出过 45,935→351 事故，schema 自己写"优先用 edit_shadow" |
| 改名 | `rename_file`(0) / `move_file`(1) | 同目录改名是移动的特例 |

## 二、语义交叉（能做同一件事，模型容易选错）
| 交叉点 | 涉及工具 | 现状 |
|---|---|---|
| 写「现状」块 | `edit_card` / `update_status` / `propose_status` | 三条路；update_status 每次整块写两三千字 |
| 写「需求文档」块 | `edit_card` / `append_ruling` / `curate_face` | 三条路（卡自己 / 卡自己 / 总裁） |
| 读别人卡面 | `read_card` / `read_linked(正文)` | 两条路；read_linked 额外要连线 |
| 读影子 | `read_shadow` / `read_linked(影子)` | 两条路（自己 / 别人） |
| 向上交代 | `report_up` / `report_done` | 两条路（普通卡 / coder） |
| 面向用户 | `report_to_user` / `confirm_with_user` | 汇报 vs 要确认 |
| 让卡接着干 | `resume_child` / `continue_task` / `stage_open` | 三个，语义相邻 |
| 让卡别动 | `stop_card` / `silence_card` | 停当前轮 vs 完工后静音 |
| 让卡消失 | `delete_card` / `archive_card` | 不可逆 vs 可恢复 |
| 写正本 | `write_ledger` / `append_ruling` | 职能台账 vs 卡面第三块 |
| 只出草案 | `propose_status` / `draft_rule_edit` | 两个"提议不落盘" |
| 跑代码 | `bash` / `python` | 两个执行口，都常用 |
| 改文件 | `write_file` / `edit_file` | 覆盖 vs 精准 |

## 三、错位（开关或可见面与语义不符）
| 工具 | 问题 |
|---|---|
| `report_done` | 挂 `tools_file`（文件读写）开关——完工回执与文件权限绑在一起 |
| `stage_open` | 挂 `read` 且在基础池 ⇒ 实算可见面里 coder/rule 也有它 |

## 四、空转（机制在，实际没用上）
| 工具/分支 | 证据 |
|---|---|
| `sync` 的"缺省沿连线送达" | 458/459 显式点名，只有 1 次走缺省 |
| `link_cards` 的 type=先后 | 62 关联 / 1 先后 |
| `record_verdict` 的 silent | speak 371 / silent 8 |
| `edit_card` 的精准编辑 | 229 次里只有 1 次用 old_string/new_string（其余是旧版整篇覆盖） |
| `create_goal` / `function_watch_cards` / `set_function_duty` / `reparent_card` | 0 次 |
| 评审五件 | 0 次（10-27 才建，只在沙箱跑过） |

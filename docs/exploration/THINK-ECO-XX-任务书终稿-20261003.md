# 影子卡机制 · 实现任务书（终稿）

> 落盘日期：2026-10-03 18:51
> 来源：任务书链 taskbook 六环节产出（p0 条目清单 → p1 调研实读 → p2 骨架草稿 → p3 九条质疑 → p4/p4b 修订 → p5 放行）
> 放行时间戳：2026-10-03T18:37:00+08:00（用户授权诸葛代行放行）
> 依据：SHADOW_CARD_RULINGS.md（九条裁定，提交 f28b4e1）＋ THINK-ECO-XX-方案终稿-20261003.md（185 行，提交 6b1714c，17:51 放行）
> 本任务书 report-only 产物，执行需另行派发

## 一、背景与裁定

**背景**：推演系统（`D:\AI_station\推演agent`，FastAPI + 静态前端，正式实例 5700）的卡片承载推演结论。正卡推进时会产生大量过程性信息——进度、产物位置、迭代改动。这些塞进正卡正文会污染给人看的内容，扔掉则丢过程痕迹。**盘上实证**：`work_002/cards/card_002.md`（「项目现状地图」）建于 12:51:52、`updated_at` 同为 12:51:52，**建后一次未更新**，而同期盘上剧本从 2 个长到 6 个。手工快照必然腐烂。

**机制**：为正卡配一个「影子卡」——同构 `.md`、落 `works/{work_id}/shadows/{owner_card_id}.md`、由正卡自己书写、随正卡生命周期存续。

**裁定依据（九条，用户亲口，唯一依据）**：`SHADOW_CARD_RULINGS.md`（提交 `f28b4e1`）——①推送 ②格式授权（已定 `.md`）③一正一影 ④模型主动记 ⑤记什么＝用户 15:05 三类 ⑥人不能直接改 ⑦存量补 ⑧不做总览 ⑨读都能读、写只有自己的正卡。

**方案依据**：`docs\exploration\THINK-ECO-XX-方案终稿-20261003.md`（185 行，提交 `6b1714c`，17:51 放行）。

**本任务书的锚点分级**：A 级＝已实读原文（行号与内容逐字对回）；B 级＝方案声明、执行前须实读回对；C 级＝待侦察项（未闭合前不得动手对应部位）。

## 二、现场锚

### A 级：已实读原文（行号与内容逐字对回）

| # | 锚点 | 实读原文 | 条目 |
|---|---|---|---|
| A1 | `src/storage/card_store.py:318` | `await data_snapshot.snapshot(f"[snapshot][{work_id}] create: {card_id}")` | S-43 |
| A2 | `card_store.py:316-317` | **是注释**（「[数据与代码分家 2026-10-03] 不再 git 提交运行数据……改由独立快照目录留痕」）——**不是被替换掉的旧调用行** | S-43 |
| A3 | `card_store.py:344-353` | L344 `stored = meta.get("content_hash","")`／L345 `computed = calc_card_hash(raw)`／L346 `if computed != stored:`／L348 snapshot／L350 `_append_history_line(raw, f"- [{now}] [外部修改]")`／L351 `replace_fm_field(raw,"updated_at",…)`／L352 `apply_hash(raw)`／L353 `write_text` | S-42 |
| A4 | `card_store.py:466` 起 | **`delete_card` 函数定义** | S-45 |
| A5 | `card_store.py:480-481` | 剧本根卡不可删判据 `card_id == work_store.work_card_id_of(work_id)` | S-46 |
| A6 | `card_store.py:488` / `L490` | `cd = _cards_dir(work_id)` ／ `for path in cd.glob("card_*.md"):`——子卡判据**只扫 `cards/`** | S-47 |
| A7 | `card_store.py:503` / `507-512` | L503 `card_path.unlink()`；L507 `session_path = _session_path(…)`、L508-512 try/unlink | S-45 |
| A8 | `card_store.py:516` | `await data_snapshot.snapshot(f"[snapshot][{work_id}] delete: {card_id}")` | S-45 |
| A9 | `card_store.py:466-517` 全段 | **全文零处出现 `shadow`** | S-45 |
| A10 | `src/api/card_tools.py:155-169` | `_collect_card_meta` 定义 L155；L158 `cd = card_store._cards_dir(work_id)`；L162 `cd.glob("card_*.md")` | S-48 |
| A11 | `card_tools.py:172-231` | `can_read` 四判据：子树 L187-201／血统链 L203-217／relations L219-222／锚卡全局 L224-229（L231 `return False`） | S-49 |
| A12 | `card_tools.py:234-259` | `can_edit` **只判子树**（L241-258 反向遍历 parent 链，**零血统链判据**） | S-50 |
| A13 | `card_tools.py:262` 起 | `execute_tool` 定义行 L262 | S-53 |
| A14 | `card_tools.py:274-288` | `read_card` 分支返回体五字段：`card_id`／`title`／`parent`／`relations`／`body` | S-54 |

### B 级：方案声明、执行前须逐条实读回对

**执行作业（动手改代码之前必须完成）**：下列 10 处逐一跑核实命令、看输出、与方案描述比对，每处一句话写进回报第 1 项。**任何一处与方案描述不一致 → 停下、写进回报、该处不动手。**

| # | 锚点文件:行号 | 方案说什么 | 核实命令 / 判据 | 条目 |
|---|---|---|---|---|
| B1 | `src/storage/card_store.py:118` | `cd.glob("card_*.md")` 只扫 `cards/` | read 第 118 行，看是否 `cd.glob("card_*.md")` 且 `cd` 来自 `_cards_dir` | S-47 |
| B2 | `card_store.py:238` | `_subtree_ids` 内同族 glob，只扫 `cards/` | read 第 238 行 | S-47 |
| B3 | `src/storage/canvas_store.py:73` | `cd.glob("card_*.md")` 只扫 `cards/` | read 第 73 行 | S-21 |
| B4 | `src/api/context_assembler.py:39` | 同上 | read 第 39 行 | S-21 |
| B5 | `src/api/card_tools.py:162` | 同族 glob（已实读 · A10，此处复核 `cd` 来源） | 见 A10 | S-48 |
| B6 | `src/storage/work_store.py:67` | `cards_dir.glob("card_*.md")` 只数 `cards/` | read 第 67 行 | S-21 |
| B7 | `work_store.py:151` | `(_work_dir(work_id)/"cards").glob("card_*.md")` | read 第 151 行 | S-21 |
| B8 | `src/storage/data_snapshot.py:54` | `root.rglob("*")` 全树遍历、`PROTECTED_DIRS = ("works",)` | read 第 50-58 行，确认 rglob 与 PROTECTED_DIRS 原文 | S-29 |
| B9 | `src/api/progress.py:12` | `_store: Dict[Tuple[str,str],…]` | read 第 12 行 | S-30 |
| B10 | `src/api/routes.py` 全文 | 无 SSE／事件通道（现有端点全为请求-响应式） | grep `EventSource\|StreamingResponse\|text/event-stream\|sse` routes.py，**零命中即方案说法成立** | S-57/S-58 |

### C 级：待侦察项（未闭合前不得动手对应部位）

**闭合方式统一为**：实读原文并在回报里写明文号 → 闭合。**仍不能闭合 = 停下、写进回报第 2 项、对应改件不动手。**

| # | 内容 | 影响 | 闭合判据 | 对应改件 |
|---|---|---|---|---|
| **R1** | `create_card` 中旧 `git_auto_commit.commit` 的**确切行号未实读**（L300 之前未读）——方案「L316 原…改为 L318」的表述**可能被误读为「同行替换」**，实际 L316-317 是注释、L318 是新调用行 | 实施者按「替换」理解会找错行 | read `card_store.py` L290-320，写出旧调用是否存在、在哪一行；若根本不存在（已删净），写「已删净，无原位」 | 改件二·第 8 条 |
| **R2** | `_next_seq`（序号分配函数）**未实读**——方案称「与其余 8 处 glob 同族、只扫 `cards/`」，**盘上未见原文依据** | 不变量①「序号不变」的验收缺一半依据 | grep `_next_seq\|next_seq\|序号` 全库，找到实现后 read 之，确认其扫描范围 | 验收 V3 |
| **R3** | `_cards_dir` 函数本体未实读（只读到调用点） | 低 | read `card_store.py` L110-125 附近，写出 `_cards_dir` 本体 | 全改件（只作理解） |
| **R4** | `read_card` 工具的**工具描述文本**（挂进 `BASE_PROMPT` 的那段）未实读 | `read_shadow` 的描述要与之同族 | grep `read_card` 于 `src/api/prompts.py`，读出描述原文 | 改件四·第 19 条 |
| **R5** | 方案称「`routes.py` 已核无 SSE」——**本轮未核**（并入 B10 一并核） | 推送机制依据 | 见 B10 | 改件六 |

## 三、任务分解

**改件一 · 新建 `src/storage/shadow_store.py`**〔S-37～S-42〕

1. `_shadows_dir(work_id)` —— 影子目录唯一来源，与 `card_store._cards_dir` 同构〔S-38〕
2. `shadow_path(work_id, owner_card_id)` → `works/{work_id}/shadows/{owner_card_id}.md`〔S-39〕
3. `read_shadow_raw` / `write_shadow_raw` —— 走 `.md` ＋ front matter ＋ `apply_hash`（复用 `card_store.parse_front_matter`）〔S-40〕
4. 路径锁：与 `update_card` 共用 `card_store._get_lock`〔S-41〕
5. 写入后调 `data_snapshot.snapshot(…)`（对照 `card_store.py:318/458/516/558`）〔S-42〕
6. 影子文件模板：front matter 七字段（`card_id`／`card_type: shadow`／`parent`／`shadow_of`／`created_at`／`updated_at`／`content_hash`）＋ 正文三节（进度／产物清单／迭代历史）〔S-60〕
7. **派生函数抽为可外部调用形态**（`create_card` 内部与存量补跑脚本共用同一函数）〔S-59〕

**改件二 · `src/storage/card_store.py` 两处**〔S-43～S-46〕

8. `create_card`：在 **L318 同一 try 段内、与 snapshot 并列**新增派生调用——**是新增，不是替换 L316 那一行（L316-317 是注释）**〔S-43〕〔挂 R1〕
9. 派生失败：捕获异常 → 写 warning 日志（含 `work_id`／`card_id`／异常原文）→ **建卡照常返回 201**〔S-61〕
10. `delete_card`：在 L507-512 unlink session 段**之后**新增一步删影子〔S-45〕
11. **`card_store.py:488/490` 的子卡判据一行不动**（只扫 `cards/`，影子不在其内）〔S-47〕

**改件三 · `src/api/card_tools.py` 三处**〔S-48～S-56〕

12. `_collect_card_meta`（L155-169）在 `cd.glob("card_*.md")` 循环**之后**追加一段扫 `shadows/` 的循环，影子 meta（`parent`＝正卡、`shadow_of`＝正卡 id）并入同一字典〔S-48〕
13. `can_read` 四判据（L172-231）**一行不改**〔S-49〕
14. `can_edit` 判据本体（L234-259）**一行不改**〔S-50〕
15. **`edit_card` 执行体入口新增拒写判据**：目标 `card_type == "shadow"` → 直接返回 error（「影子卡只能由对应正卡经 `update_shadow` 修改」）〔S-51〕
16. **不提供**删影子工具〔S-56〕

**改件四 · 两个新工具挂进 `execute_tool` 分发链**〔S-53～S-55〕

17. `update_shadow(content)` —— **schema 无 `card_id` 参数**，执行体写 current 自己的影子〔S-53〕
18. `read_shadow(card_id 可选，缺省＝自己)` —— 返回体含**影子全文／`owner_card_id`／最后更新时间**；带 id 时校验目标在 `can_read` 范围内〔S-54〕〔对齐对象见 A14〕
19. 两个工具的描述挂进 `src/api/prompts.py` 的工具描述段（与 `read_card` 描述同族）〔S-55〕〔挂 R4〕

**改件五 · 存量补跑**〔S-59〕

20. 新建一次性脚本（项目根目录，与 `restore_from_snapshot.py` 同族），对 `works/work_001`–`work_006` 逐个正卡派生；**调改件一第 7 条那个共用函数**

**改件六 · 推送（裁定 1）**〔S-57～S-58〕

21. **推什么**：影子三类内容；**推到哪**：上级卡；**机制**：上级卡在自身调用中经 `read_shadow` 拉取——`routes.py` 无 SSE／事件通道〔挂 B10〕

**改件七 · 失败处理与打点**〔S-61～S-68〕

22. `update_shadow` 失败：异常上抛，返回 error 字段原文〔S-62〕
23. 补跑单卡失败：记录 `work_id`／`card_id`／异常原文后**跳过继续**；结束打印成功／失败计数〔S-63〕
24. **不设重试、不设超时、不设轮次上限**〔S-64〕（用户 13:37 永久禁令）
25. 派生／写入／删除三类动作各记一行日志；工具调用走既有 `tool_events`〔S-66～S-68〕

## 三·五、代拍留痕

**本节逐条列出本次由诸葛代用户拍板的决定。每条格式：拍什么 ／ 依据 ／ 此条由诸葛代拍，用户可事后推翻。**

**代拍 1 —— 影子文件 front matter 的字段命名**
拍什么：七字段取 `card_id`／`card_type: shadow`／`parent`／`shadow_of`／`created_at`／`updated_at`／`content_hash`。
依据：第 2 条裁定「格式授权」（用户明令「哪个更好用哪个」）＋ 方案 4.7 节。
**此条由诸葛代拍，用户可事后推翻。**

**代拍 2 —— `read_shadow` 返回体的三字段**
拍什么：返回影子全文 ＋ `owner_card_id` ＋ 最后更新时间。
依据：方案 4.4 节「逐字段对齐 `read_card`」＋ A14 实读的 `read_card` 返回体形态。
**此条由诸葛代拍，用户可事后推翻。**

**代拍 3 —— 存量补跑脚本的落点与文件名**
拍什么：落项目根目录，与 `restore_from_snapshot.py` 同族命名。
依据：方案 4.6 节。
**此条由诸葛代拍，用户可事后推翻。**

**代拍 4 —— 影子文件正文的三节标题用字**
拍什么：三节标题取「进度」／「产物清单」／「迭代历史」。
依据：用户 15:05 原话三类（进度更新／文档与代码文件信息和路径／迭代历史与更改记录）的直接对应。
**此条由诸葛代拍，用户可事后推翻。**

**代拍 5 —— C 级待侦察项的处理粒度**
拍什么：R1–R5 五项统一按「实读原文 → 写明文号 → 闭合」处理；不能闭合则停下、对应改件不动手。
依据：方案 4.8 节终止条件与失败处理的原则（不设重试、不设超时、遇阻停手如实报）。
**此条由诸葛代拍，用户可事后推翻。**

## 四、红线

1. **不得改动九条裁定**——任务书只落实，不反改裁定
2. **不得超出方案终稿范围自加需求**；不得写「酌情／视情况／等等」类含糊表述
3. **`card_store.py:488/490` 子卡判据一行不动**〔S-47〕——这是「影子不计入有子卡判据」的机械保证。**说明：该两行位于 `delete_card` 内（L466-517）；改件二第 10 条的删影子新增点位于 L507-512（session unlink 段）之后——两处相隔 17 行以上，互不影响，不得顺手改动 L488/L490 附近任何一行**
4. **`can_read`／`can_edit` 判据本体一行不改**〔S-49／S-50〕——写侧独占靠工具签名，不靠判据
5. **不设任何闸**：零轮次上限、零 `max_tokens`、零墙钟熔断〔S-64〕
6. **双环境纪律**——执行环节的全部改动与验证，先在**测试实例 `D:\AI_station\推演agent_test`（端口 5701）**跑通；生产实例 `D:\AI_station\推演agent`（端口 5700）只在最后做一次部署窗口。判据：①测试实例 `restart_safe.py` 判据全绿后重启、V1–V13 全部通过；②生产实例部署前先跑 `restart_safe.py --check`，**判据全绿才重启**；③生产实例部署后**只做探活**（`GET /api/works` 返回 200）＋**一条真实调用冒烟**，**不在生产上跑 V1–V13 全套**
7. **B 级锚点在动手前必须实读回对**——方案说的行号，实施者要亲眼看过再用（B1–B10 十处逐条作业）
8. **C 级待侦察项未闭合前不得动手对应部位**〔R1–R5〕
9. 不动工作区四处未提交改动（`card_tools.py`／`card_store.py`／`work_store.py`／`workspace.js`）
10. **并发写**：影子读写**必须走异步路径**——`shadow_store` 的函数在 `execute_tool` 内**必须 `await`**，文件 IO 走 `await asyncio.to_thread(…)` 或复用 `card_store` 已有的 async 链路；**严禁同步直调**（2026-10-03 16:20 刚修过 `file_tools` 四件同类问题）
11. **生产窗口**：任何重启动作**必须走 `restart_safe.py`**（判据全绿才重启），**不得直接杀进程**（2026-10-03 16:49／18:10 两次误杀用户消息）
12. **git 追踪**：`shadows/` 目录**不得进入 git 追踪**——`works/` 已于 2026-10-03 15:41 剥离，影子落 `works/{wid}/shadows/` 属运行数据，`.gitignore` 的 `works/` 规则应已覆盖。验证方式：`git check-ignore -v works/work_001/shadows/test.md`（有输出即已覆盖；**无输出 = 停下报告，不得继续**）

## 五、验收（全部实跑，机械判定）

| # | 判据 | 条目 |
|---|---|---|
| V1 | `ls works/*/shadows/` 条数＝各剧本正卡数；文件名＝正卡编号 | S-20 / S-39 |
| V2 | 建影子前后：`canvas.json` 逐字节相同；`context_assembler.py:39` glob 结果条数不变 | S-21 |
| V3 | 建影子前后对同一剧本调序号分配函数，返回值逐字相同〔挂 R2〕 | S-22 |
| V4 | 建影子后对正卡执行删除 → **不因影子报「存在子卡」** | S-23 |
| V5 | 删正卡后 `shadows/{owner_card_id}.md` `exists()==False`，目录无残留 | S-24 |
| V6 | 模拟派生失败（权限异常）时建卡仍返回 201，日志有 warning | S-25 |
| V7 | `update_shadow` schema 无 `card_id` 参数 | S-26 |
| V8 | 四组样本（子树／血统链／relations／锚卡）逐组调 `read_shadow`，记录 `ok` 与返回体；脚本落 `scripts/verify_shadow_read.py`，**不建 `tests/` 目录** | S-27 |
| V9 | 写影子后**连续 `read_card` 该影子 N 次，历史行数不增** | S-28 |
| V10 | 写影子后查 `D:\AI_station\推演agent_data_snapshots\{work_id}\` 最新批次**含 `shadows/` 路径** | S-29 |
| V11 | 影子正文含三节，逐节与用户 15:05 三类对账 | S-30 |
| V12 | 前端无影子编辑入口 | S-31 |
| V13 | 补跑后每个剧本 `shadows/` 条数＝其正卡数 | S-32 |

**判据全部以真实产物为准——不得以「回归脚本全绿」替代上述任一条。**

## 六、回报要求

1. **实读回对报告**：A 级 14 条（已核，可略）＋ **B 级 10 处逐条实读作业**，每条写明「方案说什么／原文是什么／是否一致／不一致时的处置」；C 级 5 项待侦察项逐条写「闭合了没／文号是什么／仍不闭合的原因」
2. **改件清单**：逐文件逐处，写「改了什么行／依据哪条条目」
3. **验收实测**：V1–V13 逐条，写「跑了什么／输出是什么／过没过」
4. **未做项**：如实列，不得把未做写成已做
5. **代拍复核栏**：对三·五节的五条代拍逐条表态（同意执行／推翻并说明）
6. **三笔悬账随书交出**：①方案链的三张对账表与红灯账 JSON 全盘不存在（确认未落盘）；②方案链条目清单 34/36 数字未查实；③工作区四处未提交改动保持原状

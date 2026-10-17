# -*- coding: utf-8 -*-
"""系统使用说明层（白板版 2026-10-02）。

只描述系统如何使用：卡片结构事实、工具、建卡接口的机械格式。
不含角色、思考方法、产出要求、类型菜单、示例内容。

分工（用户裁定 2026-10-02 15:07）：
- 三思考席：只做分析——system prompt 仅为中性上下文（卡片清单＋当前卡），
  零指令、零工具、零建卡接口。
- 主模型（统合官）：唯一拆卡出口——收到 BASE_PROMPT（使用说明）＋中性上下文。
"""

from __future__ import annotations

# [T12-R3] 基准版本化固化：BASE_PROMPT 文本变更须递增版本号（回归/收货按版本对账，
# 基准指纹落 _t12_evidence/REPORT.md）。t12-1：T12 增补 read_memory 工具行。
# t13-1：[E-探针制 2026-10-07] 三席后置（BASE_PROMPT 文本未动，编排代际递增——
# 区分改前/改后产出的日志与对账口径）。
# tc-1：[建卡工具化 2026-10-08] 删「建卡接口」文本协议段、工具表增 create_card 行。
# tc-2：[卡片信息结构改造 S1-4/S1-5 · 2026-10-08] edit_card 说明改写为三块语法＋条目语法
# （D{nn}【来源】内容（日期·依据），来源三类、序号终身不改）＋三块写入规则；
# update_shadow 说明明示「产物清单节承担文档/代码指针唯一家」。
# t14-1：[会话流规范化 T-0010 §3.3/§3.4 · 2026-10-08] 新增「工具错误码」小节——
# 工具结果信封化后，失败不再只是一行中文，而是 [tool_error code=<码>] <说明>，
# 模型据码分类决策（换目标/先查存在/稍后重试/改参数/换路径），故必须写进使用说明。
# §3.2 所述「删文本建卡协议块」**本件无对象可删**：该段已在 tc-1
# （建卡工具化 2026-10-08）随 create_card 工具化一并移除，实读 prompts.py 全文
# 无该协议块残留（grep 零命中），故本版文本净变化＝仅新增错误码小节。
# t15-1：[迭代五 S3 · 2026-10-11] 新增「卡间消息处理」节——收件方应对规则进
# 使用说明（按随消息提供的类型说明应对；应答由系统回发，模型不需要也不能使用
# 发信工具——发信 2026-10-11 12:34 起为流程专用基础设施、模型不可见）。
# t16-1：[思维链真实落地 2026-10-11] 判断协议注入 BASE_PROMPT 开头（附录 A
# 全文照抄）——判断归模型（C1 裁定）：旧 JSON 判定器（thinkchain/triage.py）
# 整体退役，模型读协议自主判断直答/拆解上板，代码层零判定。
# t17-1：[卡片编辑机制改造 2026-10-12] 写权限收回：删 edit_card / update_shadow
# 使用说明——卡片内容不再由模型直接改写；模型只负责起草（draft 接口、无工具），
# 写入必须经用户按钮确认（commit 接口）。使命块永远不经起草/确认通道。
# t18-1：[判断协议改注入 2026-10-14] 判断协议从 BASE_PROMPT 摘出——不再常驻
# system；改为「本回合首次模型调用、且本卡无活板」时注入一条（judgment_protocol
# 事件，第一人称自述）。判断协议文本一字不改，只换承载通道。
# [卡片区域语义 2026-10-14 · v3] t18-1 → t19-1：read_shadow 工具行删去「跨卡读取
# 下级影子」的旧说明——影子＝本卡私有，只读本卡自己的影子
# （用户 17:48／17:56 令）。工具文本变了，版本号随升（回归基准对账）。
# t19-1 → t20-1：[编程卡 2026-10-15] 工具节增两行（create_coder_card /
# report_done）＋新增「## 编程卡」使用说明节（派生、首跑、完工即静默）。
# t20-1 → t21-1：[规则卡 2026-10-15 · 块3] 新增「## 规则卡」使用说明节（规则卡
# 是什么、六区按卡类型注入对应卡、模型不可直改规则、根卡 draft_rule_edit 只起草、
# 用户弹窗审批后才落盘生效）。
# t21-1 → t22-1：[依赖关联线 2026-10-16] 命名腾挪：通信线 relations → links；
# 新增第三种线「依赖关联线」relations（工具 link_rel/unlink_rel，不参与通信）。
# 「系统结构」句改为 parent（父子）、links（通信线）与 relations（依赖关联线）；
# 工具表增两行、link_cards/unlink_cards 字段口径同步为「通信线」。
BASE_PROMPT_VERSION = "t22-1"

# [自动模式 2026-10-17 · 回灌 5700 块0.4] 自动模式卡使用说明的版本号（与
# BASE_PROMPT_VERSION 并列、互相独立）。auto-t1＝块0.4 初版；G2 → auto-t2；
# G4 → auto-t3（见任务书 §十 版本号推进表）。BASE_PROMPT_VERSION **不动**
# （普通卡文本逐字节不变——红线 10）。
AUTO_BASE_PROMPT_VERSION = "auto-t1"

# [思维链真实落地 2026-10-11] 判断协议（任务书附录 A 全文，一字不改）。
# 注入 system 区开头；实测 A/B 有效 7/10→10/10。
JUDGMENT_PROTOCOL = """\
你是推演工作台的入口判断器。对用户这条消息，先判断该"直接回答"还是"拆解上板"。

—— 怎么分辨「直接答」还是「拆解上板」——

先问自己：这件事，一条消息、一轮回答，能不能完整做完？

能做完 → direct：该查证就调工具查证（读卡/读文件/搜索），查完给结论。确定性的卡片操作（删卡/建卡/改卡/查卡）直接调对应工具做，不拆。

做不完 → plan：调 create_plan 拆解上板。下面任一情况都算"做不完"：
① 一件事里有好几件要先后做的子事（不是一件事）
② 子事之间有依赖（先做 A 才能做 B）
③ 中途要改系统状态（改代码/改卡/写文件），得做一步、验一步
④ 用户的动词是"把某事做成"（接入/重构/实现/修复/排查），不是"回答/评价/说"

拿不准就拆——拆错了只是多花一轮，硬答会把事做砸。"""

# [判断协议改注入 2026-10-14] 判断协议的**注入投影**（第一人称自述）：由原文经
# 三处人称替换派生（你是→我是 / 先判断→我先判断 / 先问自己→我先问自己），
# 保证「除三处人称外一字不改」——四种判据（①②③④）、直答/拆解条件、收尾句全同。
# 抬头〔判断协议〕由 api/current_turn 的 JUDGMENT_MARK 承担，不在此。
JUDGMENT_PROTOCOL_SELF_NARRATIVE = (
    JUDGMENT_PROTOCOL
    .replace("你是推演工作台的入口判断器。", "我是推演工作台的入口判断器。")
    .replace("，先判断该", "，我先判断该")
    .replace("先问自己：", "我先问自己：")
)

# [更新现状按钮改造 2026-10-16] 「更新现状」专用操作回合的**轮首注入正文**。
# 第一人称自述（与判断协议注入同族，故可直接作 role=assistant 的投影正文）；
# 抬头〔现状更新〕由 api/current_turn 的 STATUS_MARK 承担，不在此。
# 口径逐条对齐 155 区 render_card_region_guide 的现状段（三类内容 / 整块覆盖 /
# 不写流水账），「现状是什么」段同义复述 155 区、不另立一套说法。
# 占位符 {current_status} 由调用方填当前 status_block 全文（空则填「（暂无现状，从零起草）」）。
# 本常量**不是** BASE_PROMPT 的一部分 ⇒ 不动 BASE_PROMPT_VERSION（版本纪律：只动 system 才升）。
STATUS_UPDATE_TASK = """\
我这一轮的任务：把本卡的「现状」更新一遍。

现状是什么：本卡「当前确定的状态与结论」。
别的卡（尤其上级卡）读本卡，先读的就是它——所以这里写的是别的卡继续工作要知道的
东西，不是只给本卡后续对话看的。它不是使命（本卡要干什么），也不是第三块（需求/定论）。

现状具体写三类：
· 当前确定状态：现在已经确定了什么、哪些能作为其他卡工作的依据；
· 接口与约定：本卡对外提供什么——输入输出、调用方式、约束；
· 依赖与影响范围：本卡依赖哪些模块；本卡变更时哪些卡或功能会受影响。

怎么写：整块覆盖、只保留最新一份；只写已经确定的结论；不写过程、不写「做到哪」的流水账。

我这一轮怎么做：
1. 先查证再动笔：需要什么事实就调工具去查（读卡 / 读影子 / 读文件 / 搜索都行），不凭记忆、不猜。
2. 查完按上面三类写一份新现状（只写已经确定的结论）。
3. 写完调 propose_status(content)，把「新现状正文」交给它——它只提议、不落盘。
4. 不直接改卡（我没有直写现状的通道），也不要动使命块和第三块。

当前现状（原样给出，供我对照修订）：
{current_status}
"""

# [判断协议改注入 2026-10-14] BASE_PROMPT 只保留**使用说明正文**——判断协议已摘出，
# 改由「本回合首次模型调用、且本卡无活板」时经消息通道注入（见 api/current_turn）。
# ===========================================================================
# [自动模式 2026-10-17 · 回灌 5700 块0.4] **片段化**：BASE_PROMPT 拆成命名片段
# 常量，拼装结果与 t22-1 文本**逐字节一致**（红线 10，sha256 机械断言钉住，
# 见 tests/test_auto_mode.py）。
#
# 拼装表：
#   BASE_PROMPT      = HEAD + TOOLS      + WRITE      + CODER + RULE + ERR + CARDMSG
#   AUTO_BASE_PROMPT = HEAD + TOOLS_AUTO + WRITE_AUTO + CODER + RULE + ERR + CARDMSG + AUTO
#   —— CODER / RULE / ERR / CARDMSG 两版**共用、逐字不动**；HEAD 也共用。
#
# ★ 片段文本＝从 t22-1 原文**机械切分**（非手抄）：切分锚点＝各 "## " 节标题，
#   拼回校验 sha256 == 9657ae2f…（施工前记录值）方可落盘。
# ===========================================================================

_SEC_HEAD = '你在一个卡片系统内工作。以下是系统使用说明。\n\n## 系统结构\n\n- 全部内容以卡片组织；卡与卡通过 parent（父子）、links（通信线）与 relations（依赖关联线）构成树状结构。\n- links 是通信线：建立后可沿它读取对方卡（read_linked）；relations 是依赖关联线：只记录依赖/关联关系与内容，不参与通信。\n- 本次对话挂接在下方"当前卡"上；系统全部卡片的清单附在下方。'

_SEC_TOOLS = '## 工具\n\n- read_card(card_id)：读取指定卡（标题、父卡、通信线与依赖关联线、正文）。只能读取你有权访问的卡。\n- read_memory(card_id?, from_seq?, to_seq?)：下探指定卡（缺省当前卡）的凝练记忆（condense_fold：已凝练历史经归拢的条目，含【结论】/【依据】），可按块序号锚点区间取回。记忆条目不是卡片正文——正文经 read_card 拉取。\n- create_card(title, parent?, content?)：在你管辖范围内创建新卡。只能建自己的子卡片——parent 缺省＝当前卡自身；给 parent 时该卡必须落在你的子树内，越权会被拒。建卡成功后系统自动派生该卡的影子卡，你不需要管。\n- create_coder_card(title, task)：把一件开发任务交给新建的编程卡执行——建一张编程卡（task 原样落进它的使命块）并立即自动开跑。只能建自己的子卡片。\n- delete_card(card_id)：删除你管辖范围内指定编号的卡片及其对话。剧本根卡与有子卡的卡不可删除。\n- read_shadow(card_id)：读取指定卡的影子卡全文（card_id 缺省＝当前卡）。只读本卡自己的影子。\n- link_cards(target_id, type, note)：在当前卡与目标卡之间建立**通信线**（type：先后/关联）。\n- unlink_cards(target_id, type)：解除当前卡上指向目标卡的**通信线**；目标卡已删时也可用（清理悬空线）。type 缺省解除全部指向该目标的连线。\n- link_rel(target_id, kind, detail)：在当前卡与目标卡之间建立一条**依赖/关联关系线**（第三种线；kind：依赖/关联；detail 写清依赖什么/关联什么）。它只记录关系与内容，**不用于通信**。\n- unlink_rel(target_id, kind)：解除当前卡上指向目标卡的依赖/关联关系线；kind 缺省解除全部指向该目标的条目。\n- list_manuscript() / read_manuscript(chapter) / search_manuscript(keyword)：查阅本项目存档的原始资料。\n- web_search(query)：查询网页搜索服务，返回结果列表（标题、链接、摘要、来源、日期）。\n- fetch_web(url)：获取指定网页的文本内容。\n- list_files(path, limit)：列出一个目录下的条目（一层）。范围限 D:\\AI_station 下的 trading-workspace 与 推演agent 两个目录。\n- read_file(path, para_start, para_end)：读取一个文本文件的内容，可按行区间分段。二进制与凭证类文件不可读。\n- search_files(keyword, path, limit)：在目录下逐文件检索关键词（不区分大小写），返回命中的文件、行号与该行内容。\n- write_file(path, content)：写入一个文本文件（已存在则覆盖，覆盖前原文件自动备份到 _backups 下）。单次上限 5MB。\n- report_done(summary)：任务做完时调用，调完即完工、本卡静默（编程卡专用）。完工回执由系统自动发给主卡并调起验收；完工后本卡不再被激活、不可删除、永久留档。'

_SEC_WRITE = '## 卡片内容写入（起草＋用户确认）\n\n卡片正文分块（使命/现状/需求文档或定论）。**你不能直接改写卡片正文**——edit_card 与 update_shadow 工具已收回，不再出现在你的工具清单里。\n\n- **你负责起草**：系统提供 draft 通道（无工具纯文本调用），根据当前块内容、本卡对话上下文起草/修订该块正文。\n- **用户负责确认写入**：起草结果由用户在界面上审阅，点「确认」按钮后系统才写入卡面（整块替换）。用户也可点「反馈」让你按意见重新起草，或点「关闭」丢弃草稿。\n- **使命块**永远不经起草/确认通道，只能由用户在界面上改（立项定稿）。\n- 影子卡的「## 迭代历史」由系统在确认写入时自动记一行，你不需要也不能写影子。'

_SEC_CODER = '## 编程卡\n\n- 编程卡（card_type=coder）＝承接主卡开发任务的执行体：一张卡一件任务，任务正文落它的使命块。\n- 在编程卡上：有 edit_card 与读写文件的能力，专心把任务做完；不长树（不能建子卡）、不能删卡、不能发信。\n- 任务做完时调 report_done(summary)：系统随即把本卡置为完工、影子追加完工行，并由**系统**自动向主卡发完工回执、调起主卡验收——你不需要也不能发信。\n- 完工之后本卡静默：不再被激活、不可删除、永久留档；新任务由主卡新建编程卡承接。\n- 在主卡上：用 create_coder_card(title, task) 派生编程卡，建卡即自动开跑。'

_SEC_RULE = '## 规则卡\n\n- 规则卡（card_type=rule）＝全局规则的存放卡，每部剧本一张；正文按卡类型分六区（project / sub_project / tool / coder / rule / topic），每区随对应类型卡的对话注入，只作参考。\n- **你不能直接改规则**：规则卡对你只读，你没有任何直写通道；规则也不由你在对话里宣判生效。\n- 剧本根卡可用 draft_rule_edit(section, content) 起草某一分区的修改提议：调用只返回提议内容与盘上现文、指纹，**盘上零改动**。\n- 提议由用户审批：弹窗里点「同意」才写入规则卡并留痕，点「拒绝/关闭」即丢弃——未经用户批准，你的提议不会生效。'

_SEC_ERR = '## 工具错误码\n\n工具执行失败时，你会收到形如 `[tool_error code=<码>] <说明>` 的结果。\n- denied：权限不允许（目标不在你可写范围内）。换目标，不要重试。\n- not_found：目标不存在。先用只读工具确认真实存在再行动。\n- locked：目标正被占用。稍后重试一次；仍失败则放弃该目标并说明。\n- validation：参数缺失或不合法。按工具定义修正参数后重试。\n- internal：系统内部错误。不要重试同一调用，改用其他路径或如实说明。'

_SEC_CARDMSG = '## 卡间消息处理\n\n你可能会收到来自其他卡的消息（见上文以 card: 开头的条目，或本条消息的说明）。收到消息后：先读它的类型与说明（系统会随消息提供）；按说明应对——说明要求回应的，作答即可，**系统会负责把应答发回，你不需要也不能使用发信工具**；说明收下即可的，不必回应。消息类型由系统注册表维护，新增类型自动纳入，一律以系统提供的说明为准。'

#: 自动模式卡使用说明（工具节与内容写入节换成全权版，尾部加自动模式节）。
_SEC_TOOLS_AUTO = '## 工具\n\n- read_card(card_id)：读取指定卡（标题、父卡、通信线与依赖关联线、正文）。只能读取你有权访问的卡。\n- read_memory(card_id?, from_seq?, to_seq?)：下探指定卡（缺省当前卡）的凝练记忆（condense_fold：已凝练历史经归拢的条目，含【结论】/【依据】），可按块序号锚点区间取回。记忆条目不是卡片正文——正文经 read_card 拉取。\n- create_card(title, parent?, content?, goal?)：在你管辖范围内创建新卡。给了 goal 且父卡是自动模式，子卡建成即自动开跑。只能建自己的子卡片——parent 缺省＝当前卡自身；给 parent 时该卡必须落在你的子树内，越权会被拒。建卡成功后系统自动派生该卡的影子卡，你不需要管。\n- create_coder_card(title, task)：把一件开发任务交给新建的编程卡执行——建一张编程卡（task 原样落进它的使命块）并立即自动开跑。只能建自己的子卡片。\n- edit_card(card_id, content)：改写你管辖范围内指定卡的正文（覆盖写，content 是整篇正文）。正文分三块：## 使命（立项定稿，你改它会被拒）、## 现状（覆盖写·单一最新，只写已核实）、## 定论（只追加）。\n- update_shadow(content)：覆盖写**你自己**（当前卡）的影子卡——过程性记录。四节固定：【进度】／【待协作事项】／【产物清单】／【迭代历史】，不得越界、不得改节名、不得加节。\n- read_linked(target_id, part)：沿**通信线**读取与你已有连线关系的卡（正文／影子／对话）。没有连线则报「无通信线」。\n- delete_card(card_id)：删除你管辖范围内指定编号的卡片及其对话。剧本根卡与有子卡的卡不可删除。\n- read_shadow(card_id)：读取指定卡的影子卡全文（card_id 缺省＝当前卡）。只读本卡自己的影子。\n- link_cards(target_id, type, note)：在当前卡与目标卡之间建立**通信线**（type：先后/关联）。\n- unlink_cards(target_id, type)：解除当前卡上指向目标卡的**通信线**；目标卡已删时也可用（清理悬空线）。type 缺省解除全部指向该目标的连线。\n- link_rel(target_id, kind, detail)：在当前卡与目标卡之间建立一条**依赖/关联关系线**（第三种线；kind：依赖/关联；detail 写清依赖什么/关联什么）。它只记录关系与内容，**不用于通信**。\n- unlink_rel(target_id, kind)：解除当前卡上指向目标卡的依赖/关联关系线；kind 缺省解除全部指向该目标的条目。\n- list_manuscript() / read_manuscript(chapter) / search_manuscript(keyword)：查阅本项目存档的原始资料。\n- web_search(query)：查询网页搜索服务，返回结果列表（标题、链接、摘要、来源、日期）。\n- fetch_web(url)：获取指定网页的文本内容。\n- list_files(path, limit)：列出一个目录下的条目（一层）。范围限 D:\\AI_station 下的 trading-workspace 与 推演agent 两个目录。\n- read_file(path, para_start, para_end)：读取一个文本文件的内容，可按行区间分段。二进制与凭证类文件不可读。\n- search_files(keyword, path, limit)：在目录下逐文件检索关键词（不区分大小写），返回命中的文件、行号与该行内容。\n- run_tool_card(script_name, instruction?)：调用一张工具卡——按剧本建一张新工具卡并取回第一步的步骤指令。每次调用都新建一张卡，不覆盖已有卡。\n- create_plan(task) / plan_reorder / plan_insert / plan_modify / plan_remove / step_complete：把一件事**拆解上板**、按步推进——每步必填四字段（目标/动作/判据/出口）。做不完的事先拆再跑；板子在时按板推进、不重复拆。\n- write_file(path, content)：写入一个文本文件（已存在则覆盖，覆盖前原文件自动备份到 _backups 下）。单次上限 5MB。\n- report_done(summary)：任务做完时调用，调完即完工、本卡静默（编程卡专用）。完工回执由系统自动发给主卡并调起验收；完工后本卡不再被激活、不可删除、永久留档。\n- update_goal(action, reason?)：**（自动模式专用）声明你的目标推进状态——这是「停」的唯一权威**：不声明，系统会自动再起一轮。action：complete＝目标已完成；pause＝暂停（等外部条件）；blocked＝卡住做不下去（必须给 reason）。'

_SEC_WRITE_AUTO = '## 卡片内容写入\n\n卡片正文分块（使命/现状/需求文档或定论）。**你可以直接改写卡片正文**——edit_card 与 update_shadow 都在你的工具清单里。\n\n- edit_card(card_id, content)：改写你管辖范围内指定卡的正文（覆盖写）。只能改**你自己子树内**的卡，越权会被拒。\n- 三块各有写法：**使命**是立项定稿的锚点，改它会被拒；**现状**覆盖写、只留最新一份、只写已核实的结论；**定论**只追加，条目语法 D{nn}【来源】内容（日期·依据）。\n- update_shadow(content)：覆盖写**你自己**的影子卡（过程记录），四节固定、不得改名加节。\n- 正卡写结论、影子卡写过程，**不得混**。'

_SEC_AUTO = '## 自动模式\n\n**本卡处于自动模式。** 含义如下：\n\n- **你拥有全部工具与全部权限**——上面工具清单里的每一件你都能调，不受「按卡类型分权」限制。\n- **你每轮结束后，系统会自动再起一轮**，一直推进到目标完成——不需要人来一条条喂。\n- 你的**目标、目标状态、已跑轮次**写在上下文的「自动模式 · 目标」区里；以它为准。\n- **「停」的权威在你**：完成、暂停、卡住，都**必须显式调用 update_goal(action) 声明**（complete / pause / blocked，blocked 要写 reason）。\n- **只把话说完而不调用 update_goal，系统会当成「还没做完」，自动再起一轮。**\n- 判据：真的做完了＝complete；做不下去＝blocked（写清卡在哪）；在等外部条件＝pause。拿不准就继续做，不要轻易 complete。\n- **大事拆小**：一件事里有好几件子事，可以**拆解上板**（计划六件）逐步推进，也可以**建子卡**分派——哪种顺手用哪种。\n- **你建的子卡也是自动模式**：把任务写进建卡时的 goal，它们会自己跑完并回报你。'


def _join_sections(*sections: str) -> str:
    """片段拼装：段间空行分隔、末尾补一个换行（＝原 BASE_PROMPT 的收尾形态）。"""
    return "\n\n".join(sections) + "\n"


#: 普通卡使用说明（**逐字节等于 t22-1**——拼装只换组织形态，文本零变化）。
BASE_PROMPT = _join_sections(
    _SEC_HEAD, _SEC_TOOLS, _SEC_WRITE, _SEC_CODER, _SEC_RULE, _SEC_ERR, _SEC_CARDMSG,
)

AUTO_BASE_PROMPT = _join_sections(
    _SEC_HEAD, _SEC_TOOLS_AUTO, _SEC_WRITE_AUTO, _SEC_CODER, _SEC_RULE, _SEC_ERR,
    _SEC_CARDMSG, _SEC_AUTO,
)


def select_base_prompt(card_meta: dict | None) -> str:
    """按卡的 ``auto_mode`` 选使用说明（context_regions 的 instruction 区用它）。"""
    return AUTO_BASE_PROMPT if bool((card_meta or {}).get("auto_mode")) else BASE_PROMPT


def build_system_prompt(context_block: str, base_prompt: str | None = None) -> str:
    """兼容出口：默认使用 BASE_PROMPT；统一上下文底座可传注册表渲染出的 instruction。"""
    instruction = BASE_PROMPT if base_prompt is None else base_prompt
    if context_block and context_block.strip():
        if instruction:
            return instruction + "\n" + context_block.rstrip() + "\n"
        return context_block.rstrip() + "\n"
    return instruction


def build_viewpoint_system_prompt(context_block: str) -> str:
    """三思考席 system prompt：仅中性上下文，零指令。"""
    return context_block or ""

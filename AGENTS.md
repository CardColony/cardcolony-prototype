# 项目规则

## 状态（重构重启）

- **之前的重构版本（Cardlo 仓的卡分支施工线）做偏离了，已停止，不再延续。**
- 现以 **CardColony** 重新开始重构。本仓（推演agent）仍是原型探索记录，
  画布交互稿/视觉稿继续作为新实现的参照。

## 本线文档

- **执行体路由（wb_exec）交接文档**：`docs/执行体路由(wb_exec)-交接文档-20261106.md`
  （候选表 / 优先级瀑布 / 五个执行体事实卡 / 排障手册 / 定时任务口径 / 命令速查）
- 机制细节与历史实测：`docs/opencode接入coder卡-最佳方案-20261025.md`

## 服务重启必须请示

**禁止未经用户明确同意就重启/杀死 5700 服务进程**（main.py / .server.pid）。
改完代码需要重启生效时：**报告改动＋问一句"要不要现在重启"**，等用户说重启再动。
- 服务是活的：在跑的卡、在途模型回合、断点续跳都会被打断。
- 前端静态文件（static/ 下 JS/CSS/HTML）不用重启，让用户刷新即可——
  只有后端代码（src/、config/）变更才需要重启。
- 用户多次强调过这条，不得"顺手"重启验证。

## 验证方式

- 测试：`PYTHONPATH=tests python -m pytest tests/xxx.py -q`
- 语法：`python -m py_compile <file>` / `node --check <js>`
- 验证通过 ≠ 可以重启；重启永远是请示项。

## 多进程拆分（ECO_ROLE，2026-11-06）

- `python main.py`（不设 ECO_ROLE）＝旧单进程行为，测试/开发照跑。
- 生产拓扑：web×1＋runner×K（ECO_RUNNER_COUNT）＋condense×1。
  web＝`ECO_ROLE=web ECO_RUNNER_COUNT=K python main.py`（5700 对外）；
  runner-i＝`ECO_RUNNER_COUNT=K python runner_main.py i`
  （5701+i 回环，ECO_RUNNER_BASE_PORT 可改基数）；
  condense＝`python condense_main.py`（5710 裸探针，ECO_CONDENSE_PORT 可改）。
  **凝练只在 condense 进程跑**（ECO_ROLE=condense；all 单进程照旧）——
  runner/web 一律被 `_condense_home()` 闸退，扫盘、排水、fold 都是它的专活；
  跨进程重复凝由「只有一个进程能凝」根除（pid 文件 `.server-condense.pid`）。
- 卡按**子树根**稳定哈希（md5，不用内建 hash——跨进程要确定性）分到
  属主 runner；父子必同片 ⇒ 单卡单写者天然成立。串行模式全部归 0 号。
- web：白名单只读 GET 本地答（procs.py `_WEB_LOCAL_GET_TPLS`）；带卡路径
  转属主 runner，无卡路径转 0 号；`/nav/running`、`/pulse`、`/observe/calls`
  扇入全 runner 合并；`/works/{}/events` 向全 runner 各开一条 SSE 扇入。
- 跨片执行：`routes._run_message`/`message_runner.deliver_now` 入口自查
  分片，非本片透明 POST 属主 `/ctl/run_message`·`/ctl/deliver`。
- startup 钩子：驱动器/调度器/续跑全库一份只跑 runner-0；非 0 号只装
  `goal_driver.install()`（goal/changed 是本进程内监听）。
- **新增端点先想它落在哪一侧**；所有进程都要起、都要活；重启=重启一组，
  仍是请示项。
- 启停（2026-11-07）：`start.bat`/`stop.bat`/`restart_daily.bat` 已按拓扑
  重写——先杀看门狗（不然它会复活服务），再起/停整组。
  pid 文件：web/all＝`.server.pid`，runner-i＝`.server-runner{i}.pid`；
  `stop.bat` 立 `logs\.manual_stop`，看门狗见此标记不拉起（`start.bat` 撤）。
  看门狗逐进程探活（web 5700 + 各 runner 5701+i），谁死单独拉起谁，不连坐。

## 上下文缓存板（context_board，2026-11-12）

- 历史区不再从全量原文现算——`context_board` 表（每 work 库内）物化
  「过闸 且 seq>=凝练 mark 且评审 keep-N」的可见块，装配只做机械投影。
- 维护挂在写口：块落库（`write_blocks_sync` 同事务过闸进板）、
  mark 推进（`save_chain`/`_save_to_db` 同事务 `DELETE seq<mark`）、
  评审流量写时逐出。原文账（session_blocks）永远是真相，
  板漂移一键 `context_board.rebuild_board` 重算；读侧有懒自愈
  （板空有货/落后原文 → 就地重建，60s 节流）。
- 开关：`CONTEXT_BOARD=0` 回退旧四层筛路（排障用，默认开）。
- `session_facade.BLOCK_FIELDS` 已含 `reasoning_content`（Kimi 保留式
  思考持久化依赖它落盘）。

## 动态材料走当轮输入口＋按任务范围缩量（2026-11-20·成本优化批二）

- **清单/子卡现状已离开 system**：分区表 `card_list`(100) 与 `child_status`(170)
  是 `delivery="tail"`——材料一字不动包进当轮事实消息（`〔卡片清单〕`／
  `〔直接子卡现状〕`），首跳随用户消息之后下发、工具续跳每跳仍在。
  ⇒ 稳定前缀不再随卡面变化分叉（实测：可缓存前缀 79% → 100%）。
- **按可信任务范围缩量**（新增两模块）：
  - `src/api/task_scope.py`：范围**只由「本轮可信任务绑定」判出来**——
    ① 显式绑定（回合入口给的 `task_binding={"kind":"card"|"jurisdiction",
    "card_id":…}`）；② **差事账绑定**（在派差事的唯一 `target_card`，**任意
    卡型**都算）；③ 本卡在执行自己的任务（活板当前步／目标续轮／自动模式）。
    **卡型与子树不再用于推断档位**（那会把「有领域子卡的管理卡的普通聊天」
    错判成全局统筹），它们只用于确定**已证实范围内有哪些卡**。
    **没有绑定 ⇒ unknown ＝ 完整材料**（正文里的卡号只供查漏，不用于选材）。
  - `src/api/doc_locator.py`：依赖正稿**条款定位**（条款号锚／标题短语锚）⇒
    给「条目标题＋行段坐标＋版本指纹＋`read_card` 行段回读命令」；
    条款号必须是正文自己标出来的（加粗/标题首词/表格首列），散文同形词不认；
    **坐标按存档原文算**（不 strip——前导空行也算行号，否则读回两行空白）；
    定位不到 ⇒ single 回退全文、global 只给坐标、**无 `read_card` 一律回退全文**；
    回退全文时**沿用批一去重**（同目标同正文只展开一次）。
- **主动投影按现有读权限逐块取材**：对象卡正文（`scope_target_body`）与
  `read_card` 同一道闸——父子线才给使命/现状，关系线只给对外信息，无线且无
  差事授面（实发面含 read_card）⇒ 一概不给（不许绕过读权限的暗门）。
- 缩量**只对 main 档**生效；三席（viewpoint）与起草（draft）保持原有材料；
  评审/场授/席位回合（`source.kind=review_step|align_turn`、`act_as`≠本卡）
  本批明确不缩量。
- **同回合当轮事实要刷新**：工具续跳前 `context_assembler.refresh_tail_facts()`
  刷新清单/子卡现状/**对象卡正文**的数据源（system 通道不动）——回合里建的
  新卡、跑过一步的子卡、改过现状的对象卡，下一跳看得见。
- 开关：**`CONTEXT_SCOPE=0`** ⇒ 范围一律 unknown ⇒ 三处材料逐字同旧
  （与 `CONTEXT_BOARD=0` 同族口径；回退不用回滚代码）。
- 回报与证据：`docs/模型调用成本优化-第二批施工回报-20261120.md`
  （三组读数、前缀分歧、验收矩阵对照、完整回退场景与放行条件）；
  用例 `tests/test_batch2_outbound.py`（实际出站）＋`tests/test_batch2_scope.py`
  （缩量语义）；回放脚本 `scripts/perf_batch2_outbound_replay.py`
  （**先做固定副本再跑**——`build_ctx` 会取走并清空待注入队列，回放绝不能碰
  生产库；读数一律取**适配器实际出站**）。

## 水位结算与一致发布（修复方案 §三，2026-11-20）

- **水位只按连续消耗前缀推进**：`_condense_one_batch` 返回结构化
  `{ok,mark,stage,consumed,detail}`。送凝按窗口位置定界——
  大块（seq≥2e9 分片）须**全片**被候选覆盖才算消耗，缺任一分片原块
  保持可见、越界候选不提交；部分消费后从剩余分片继续送凝。
  `consumed`＝窗口位前缀（空块/工具块随批销账）。发布事务复核旧水位、
  链和待提交原文，范围外追加仍可提交。
  `orig_seq=0` 合法（判空用 `is not None`，勿用 `or`）。
- **失败留账**：失败批守住旧水位，已提交前批保留。
  `condense_debt` 通过独立元数据更新落 `batch_starts`，索引或裁板写失败
  时仍可记录。退避到期后从已提交水位续跑，低于触发线也能重试，成功才清账。
  `_run_condense_pipeline` 每批成功后**从已提交水位 `_read_origin` 重定位**，
  显式合成窗口测试用 `verify_source=False` 沿输入前缀位消费。
- **一致发布**：`save_memory`/`_save_to_db` 统一走 `_publish_memory`——
  memory 行（链+condense_mark+batch_starts 簿记）＋ `_sync_nodes_tx`
  （编号分配在同事务连接上读旧索引）＋ `trim_below_tx`，一个
  `_write_sync`（BEGIN IMMEDIATE），任一失败整体回滚。**链列载荷＝
  entries 原样**（load_memory 下游依赖原始字段形态；归一化只在
  memory_nodes 派生侧 `_plan_node_rows`）。
- **同版本读**：`read_snapshot` 在单读事务（显式 BEGIN）取
  会话自己的水位、记忆节点和缓存板；板不新鲜或关闭时使用同事务读出的
  原文。卡身份、权限、模型来源沿用卡锚。`rebuild_board` 在一个写事务内
  读取当前水位和原文，并替换板行。
- **CAS/局部复活**：`memory_domain.save_chain(expected_chain=)` 事务内
  比对当前链，不一致整链不写。归拢、迁移、回收的整链发布同样核对读入版本；
  `revive_sunk`＝
  读-改-写同事务的局部生命周期更新（sunk→False＋lastUsedAt，连带
  memory_nodes.sunk 同事务翻位），`memory_expand` 复活走它——旧整链
  快照覆盖不了新记忆。
- 验收：`tests/test_section3_watermark_publish.py`。

## 异常正文隔离（quarantine/ctx_content，2026-11-20）

- 模型把调用写进正文且未派发（`pseudo_call_unparsed`/`pseudo_call_refused`/
  `rescue_refused`）⇒ 收口回复块打 `quarantine`（原因标记）＋
  `ctx_content`（剥掉坏片段的残部＋宿主一行状态，JSON 单行）。
  原文原样留 session/事件/界面档案面；**供模型使用的内容**统一走
  `predicates.context_content`（历史投影/缓存板/凝练同口径），同跳
  reasoning 不上板不回传。
- 残部判据只用已知调用族（func/invoke/parameter/tool_calls/antml:*/
  funccall/DSML 竖线等行首标签＋行尾家具串），代码示例/引用不误伤；
  无宿主异常事件的块不打标（`scripts/quarantine_blocks.py` 列出候选
  供人工核对，`--include-seq` 显式并入）。
- **假结果同口径**（card_049 实录）：模型自写 `<function_results>` 整段、
  `<function_calls>{json}</function_calls>` 结果壳（剥壳留 content 报告）、
  `〔系统结果〕`/`〔系统…〕` 仿写行均随块退出投影。
  **检测只在逐跳模型原始正文上判**（assistant_content）——finish 的
  clean_text 已拼入宿主生成的真实系统结果行（复现误标先例）；
  存量块分类只信 function_results/结果壳硬家具（include_sys_lines=False），
  〔系统结果〕在落存块里有真有假（宿主行会落进块正文）。
- **解析遮蔽口径**：`_mask_regions`（围栏代码 + function_results 结果区）
  等长空格遮蔽后才进解析/疑似判定——围栏及结果区里的调用样例只供
  审计、零派发；形态B（```json``` 数组）因此被遮蔽（代码示例红线＞
  兼容抢救）。`<call>` 块剥完 name/parameter 须只剩空白才成立；
  DSML 参数按声明类型还原（string 不翻布尔、number 转数值）。

## 正文协议按路由启用（text_protocols，2026-11-20 · 方案 §四）

- `models.yaml models.<名>.text_protocols: [dsml, call]` 声明该路由直接
  支持的正文协议：`extract_text_tool_calls(content, protocols=…)` 只在
  声明集内解析形态E（DSML `｜｜DSML｜｜ invoke`，单/双竖线变体）与
  形态F（`<call><name>` 块）。**开关查网关回报的实际座位键**
  （`result["model_used"]`；fallback 时＝请求座）——请求启用、实际
  回退到未启用路由 ⇒ 零派发只记识别事实（`proto_hint`）。
- 首轮试点＝ECO 五座（ds-v41-flash-eco-{amr,amr2,amr3,volc,volc2}）且
  **只读闸**：textE_dsml/textF_call 解析出的调用 ∉ READONLY_TOOLS
  ⇒ `text_proto_refused` 事件 + ledger `proto_side_effect_denied`，不派发；
  只读调用照常走兼容代执行链（origin=pseudo_compat 台账）。
- `<function_calls>` 结果壳不是调用：识别外壳零派发（假结果规则治理）。

## 记忆找回第一批（read_memory/memory_expand，2026-11-20 · 方案 §二）

- `read_memory`＝锚点**索引**：只列可展开的 gist 条目（含沉底/归拢），
  锚点升序 20 条/页；`cursor` 绑目标卡+筛选+索引版本+页位（串卡/改筛/
  索引变 ⇒ validation 提示从第一页重翻）；`has_more`/`next_cursor`/
  `index_rev`；每项 ≤200 字 gist 预览+level+timeline/recorded。
  锚点规则唯一：`anchor.seq` 优先、旧顶层 `seq` 兜底、0 合法——
  索引与展开共用（`memory_expand._anchor_seq`）。
- `memory_expand`＝逐字原文找回：新节点按 `coveredSeqs` 去重升序精确取块
  （**空洞保持空洞**——min/max 区间曾把覆盖外几十条无关块回灌）；旧条目
  回退 `seq_from/seq_to` 闭区间或单锚点；`coverage` 报
  exact/legacy_range/anchor/unknown（合成锚点不伪装有覆盖）。
- `page`＝**字符偏移**（0 起；next_page＝本页实际末偏移）——截短页在
  下一页从断点续读不跳字；每页 ≤6000 字且 `render_tool_result` ≤32KiB
  （先压缩结论和提示等预览，超限时据实际序列化体积一起缩短正文及
  对应坐标，返回能装下的连续前缀；每个返回字符都有来源坐标；
  无法容纳有效页时返回 `result_too_large`）。`section=details`
  按同样的体积上限分页看结论+描述+子条目；缺块
  `missing` 独立页轴 `missing_page`（50/页）；`text` 只放本页逐字
  （纯拼接零分隔），`fragments` 带块号/角色/时间/隔离标记与
  原文·text 双半开区间（sender 认 data.from 卡间发信卡）。
- **版本＝逐段内容哈希**（记长度会被等长改写骗过，整文哈希会被跨块
  挪字符骗过，少字段会被同正文来源改写骗过）：expand revision 对
  coverage/seqs/section/missing + 逐 (seq, 型|发信者|时间|隔离态|正文)
  取 sha256——拼接全文不变、块边界变了、发信卡 card_017→card_049
  也换版；read_memory index_rev 对 [seq,gist,level,sunk,degraded,
  timeline,coveredSeqs] 逐条取 sha256。不匹配 → 空页+提示回 page=0 /
  validation 提示从第一页重翻。索引回执同按实际序列化 ≤32KiB
  （超限逐条让位、游标续页补回，长字段先封顶）。
- **归拢回灌口**（`_expand_for_fold`）：`sanitized=True`——与凝练结算
  **完全同一筛选**（`review_scope.drop_stale_review(keep=0)`＋
  `enters_context`：评审消息/工具块/调用壳/留痕块正文一律略去，隔离块
  投 ctx_content 净化视图）；被略块换一行宿主状态行——保块号/型/
  发信者/**结构化 status**（失败工具如实写出），正文绝不回归拢模型；
  `revive=False` 不触发复活写入；单条按 page_chars 分摊、总产出封顶
  page_chars（**截断标记计入预算**）。
  显式 `memory_expand` 工具仍发逐字原文。
- **复活以写入结果为准**：`revived=bool(md.save_chain(...))`，落库
  失败如实报没复活。精确覆盖集走 `read_origin_seqs`（IN 查询），
  旧闭区间走 `read_origin_span`。

## "commit" 的含义

用户说 **"commit"＝同步到 GitHub**，不是只本地落账。完整动作链：

1. 本地 `git add -A && git commit`（排除 `D?Temp*`、`_devin_install.ps1` 等杂物）
2. 克隆到 `D:\Temp\prototype-pushN` → `git-filter-repo` 按既定规则清洗
   （剥 `config/wb_exec*.json`、`docs/workbuddy*`、`docs/opencode*`、
   `docs/*wb_exec*`——wb_exec 交接/路由内部文档不进开源仓、
   `docs/release-prep/宣发主导方案-v1.md`；`--replace-text` 抹机器指纹，
   规则存 `D:\Temp\prototype-push-replacements.txt`）
   ★ 规则一动，全部历史指纹即变 ⇒ 与远端分叉只能强推。规则要稳定。
3. 复核零残留后推 `CardColony/cardcolony-prototype`（dev→master）
4. Cardlo 仓同理：剥 `spike/*/data/`、`spike/*/calib/`，强推 `cardcolony-base`

界面类改动例外：**先本地给用户确认，再同步**（用户明确要求过）。

# 影子卡机制 · 执行回报（六节全项）

> 执行时间：2026-10-03 19:00–19:50
> 依据：THINK-ECO-XX-任务书终稿-20261003.md（5a41cf8）＋ 方案终稿（6b1714c）＋ SHADOW_CARD_RULINGS.md（f28b4e1）
> 执行环境：测试实例 D:\AI_station\推演agent_test（5701）先行全量；生产实例 D:\AI_station\推演agent（5700）单部署窗口
> 代码状态：两实例均**未提交**（生产工作区原有四处未提交改动原样保留于部署文件内）

---

## 零、执行中发现的阻断事故与处置（须先读）

**事故：data_snapshot.py 快照根硬编码，测试实例快照写入生产快照库。**

- 发现经过：V10 首跑 FAIL——`D:\AI_station\推演agent_test_data_snapshots` 不存在。前台探测证实 `data_snapshot.py:27` 将快照根硬编码为固定名 `推演agent_data_snapshots`（`BASE_DIR.parent / "推演agent_data_snapshots"`），任何非生产实例的快照全部落进生产快照库。测试实例此前**根本没有** data_snapshot.py（15:05 旧码），本轮同步该文件后缺陷首次被激活；补跑 34 次＋探测 1 次＝35 轮全树快照写入了生产根。
- 污染量化：生产根被写入 216 个测试批次（35 轮 × 6 剧本，其中 63 个含 shadows 标记先被清出，其余 153 个按时间窗清出——生产自有批次全部停在被测窗口 19:02:59 之前，窗内无生产写入，归属判定无歧义）。
- 生产损失评估：**零损失**。KEEP_BATCHES=200 未触发（污染峰值每剧本约 70 批 < 200，无修剪发生）；今日 daily_20261003 均为生产自写（窗内 daily 命中数＝0，未被抢写）。清扫后生产自有序恢复为 33/33/28/24/22/20＋daily×1。
- 修法：测试实例的 `data_snapshot.py:27` 改为按实例名派生 `SNAPSHOT_ROOT = BASE_DIR.parent / f"{BASE_DIR.name}_data_snapshots"`。对生产实例派生结果与原硬编码**逐字相同**（推演agent → 推演agent_data_snapshots），生产行为零变化；**生产实例的 data_snapshot.py 未动**（其硬编码路径对生产本就正确，部署面最小化）。
- 修复后测试实例快照落 `推演agent_test_data_snapshots`，V10 复验 PASS（批次 20261003_193137 含 23 个 shadows 路径文件）。

**附带发现（不影响验收，随书交出）**：测试实例 .gitignore 是 15:05 旧版（works/ 剥离之前），works/ 未被忽略且 26 个 works 文件已被暂存。按红线 12「无输出=停下报告」处置：为测试仓补上与生产逐字同款的 `works/` 规则（现同为 .gitignore:16），并以 `git rm -r --cached -f works` 撤出已暂存条目（仅动索引不动磁盘）。测试仓 index 中另有 `_sync_backup_202610031505/workspace.js.before` 为 AD 状态（暂存后已删），属同步期遗留，未动。

---

## 一、实读回对报告

### B 级 10 处（动手前逐条实读回对）

| # | 锚点 | 方案说什么 | 原文实读 | 一致? |
|---|---|---|---|---|
| B1 | card_store.py:118 | `cd.glob("card_*.md")` 只扫 cards/ | L114-122 `_next_seq`：L115 `cd = _cards_dir(work_id)`，L118 `for path in cd.glob("card_*.md")` | ✓ |
| B2 | card_store.py:238 | `_subtree_ids` 内同族 glob | L236 `cd = _cards_dir(work_id)`，L238 `for path in cd.glob("card_*.md")` | ✓ |
| B3 | canvas_store.py:73 | 同上 | L71 `cd = _cards_dir(work_id)`，L73 `for path in sorted(cd.glob("card_*.md"))` | ✓ |
| B4 | context_assembler.py:39 | 同上 | L38 `cd = _card_path(work_id,"card_000").parent`，L39 `for path in sorted(cd.glob("card_*.md"))` | ✓ |
| B5 | card_tools.py:162 | 同族 glob（A10 复核 cd 来源） | L158 `cd = card_store._cards_dir(work_id)`，L162 `for path in cd.glob("card_*.md")` | ✓ |
| B6 | work_store.py:67 | 只数 cards/ | L65 `cards_dir = p / "cards"`，L67 `len([f for f in cards_dir.glob("card_*.md")])` | ✓ |
| B7 | work_store.py:151 | `(_work_dir/"cards").glob` | L150-151 `work_data["card_count"] = len(list((_work_dir(work_id)/"cards").glob("card_*.md")))` | ✓ |
| B8 | data_snapshot.py:54 | rglob 全树＋PROTECTED_DIRS=("works",) | L54 `for p in root.rglob("*")`，L30 `PROTECTED_DIRS = ("works",)` | ✓ |
| B9 | progress.py:12 | `_store: Dict[Tuple[str,str],…]` | L12 `_store: Dict[Tuple[str, str], Dict[str, Any]] = {}` | ✓ |
| B10 | routes.py 全文 | 无 SSE／事件通道 | `grep -i "EventSource\|StreamingResponse\|text/event-stream\|sse"` 命中 4 行（L9/29/330/350）——**全部是 `context_a(sse)mbler` 的子串 sse**，真 SSE 模式零命中 | ✓（方案说法成立） |

**十处全部一致，无一处触发停手条款。**

### C 级 5 项闭合

| # | 闭合结果 |
|---|---|
| R1 | **已闭合：旧 git 调用已删净，无原位。** read L290-320：create_card 的 try 段内 L316-317 是注释（「[数据与代码分家 2026-10-03] 不再 git 提交运行数据……」），L318 是现役 `await data_snapshot.snapshot(...)` 调用行。任务书预警的「同行替换」误读风险确认存在且已规避——派生调用是**新增**，插在 L318 与 L319 `return` 之间 |
| R2 | **已闭合：card_store.py:114-122 `_next_seq(work_id)`**，扫描范围＝`_cards_dir(work_id)` → `cd.glob("card_*.md")`，只扫 cards/，方案 3-附加 的断言与原文一致，V3 可直接执行。（注意与 work_store.py:39 的 work 序号同名函数区分，后者扫 work_* 目录与卡片序号无关） |
| R3 | **已闭合：card_store.py:93-94** `def _cards_dir(work_id): return WORKS_DIR / work_id / "cards"`，WORKS_DIR = BASE_DIR/"works"（L30-31） |
| R4 | **已闭合：prompts.py L25** `- read_card(card_id)：读取指定卡（标题、父卡、连线、正文）。只能读取你有权访问的卡`——工具描述段为 L23 起「## 工具」，新工具描述按同族句式插入 L27 之后 |
| R5 | **已闭合：并入 B10**，routes.py 零 SSE，推送落为上级卡经 read_shadow 拉取（4.5 节定案） |

**五项全部闭合，无一项触发停改条款。**

---

## 二、改件清单

### 改件一 · 新建 `src/storage/shadow_store.py`（新文件，142 行）
1. `_shadows_dir(work_id)` → `card_store.WORKS_DIR / work_id / "shadows"`〔S-38〕
2. `shadow_path(work_id, owner_card_id)` → `works/{wid}/shadows/{owner_card_id}.md`〔S-39〕
3. `read_shadow_raw`（纯读：不校哈希不写回，影子无 [外部修改] 追加逻辑）/ `write_shadow_raw`（`card_store._get_lock` 路径锁＋`asyncio.to_thread` 文件 IO＋写后快照钩点）；front matter 走 `hash_utils.parse_front_matter`／`apply_hash`（与卡片同一套）〔S-40〕
4. 路径锁与 `update_card` 共用 `card_store._get_lock`／`_acquire`〔S-41〕
5. 写入后 `data_snapshot.snapshot(f"[snapshot][{wid}] shadow create/update/delete: …")`，与 card_store 四处同形〔S-42〕
6. 影子模板：front matter 七字段（card_id＝`shadow_{正卡id}`／card_type:"shadow"／parent＝正卡／shadow_of＝正卡／created_at／updated_at／content_hash）＋正文三节「## 进度／## 产物清单／## 迭代历史」〔S-60〕
7. 共用派生函数 `derive_shadow(work_id, owner_card_id)`：幂等（已存在跳过不覆盖）、正卡不存在返回 error、create_card 钩点与补跑脚本同调它〔S-59〕
8. 另含 `update_shadow`（覆盖写三节，created_at 保留 updated_at 刷新）、`remove_shadow`（无工具暴露）、`shadow_id_of`、`_meta_of`

### 改件二 · `src/storage/card_store.py` 两处
8. create_card：L318 快照调用之后、L319 return 之前**新增**（非替换，L316-317 是注释）：
   `from . import shadow_store`（局部 import 避循环，与 work_store 同族手法）→ `try: await shadow_store.derive_shadow(work_id, card_id) except: logger.warning("影子派生失败（不阻断建卡）work=%s card=%s: %s")`〔S-43＋R1〕
9. 失败处置：warning 日志含 work_id/card_id/异常原文，建卡照常走 L319 返回（routes 层 201 语义）〔S-61〕
10. delete_card：L507-512 session unlink 段之后、canvas 重建之前新增 `await shadow_store.remove_shadow(work_id, card_id)`（try/except warning 不中断，与 session unlink 同族纪律）〔S-45〕
11. L488/490 子卡判据**一行未动**；L238 `_subtree_ids`、L114-122 `_next_seq`、can_read/can_edit、update_card 全部未动〔S-47〕

### 改件三 · `src/api/card_tools.py` 三处
12. `_collect_card_meta`（L155-169）cards 循环之后追加 shadows 扫描循环：`shadow_store._meta_of` 取 meta，键＝`shadow_{正卡id}`，meta 含 card_type:"shadow"/parent＝正卡/shadow_of＝正卡，并入同一字典〔S-48〕
13. `can_read` 四判据（L172-231）**一行未改**〔S-49〕
14. `can_edit` 判据本体（L234-259）**一行未改**〔S-50〕
15. `edit_card` 执行体入口（存在性检查之后、can_edit 之前）新增机械拒写判据：`cards[target_id].get("card_type") == "shadow"` → 返回 error「影子卡只能由对应正卡经 update_shadow 修改」。该守卫必要：影子进字典后 can_edit 子树判据会放行正卡的上级〔S-51〕
16. 未提供删影子工具（delete_card 工具对 shadow_id 走「卡片不存在」路径，影子文件无删除口）〔S-56〕

### 改件四 · 两工具挂进分发链
17. `update_shadow(content)`：schema properties 仅 `content`，**无 card_id**；执行体 `shadow_store.update_shadow(work_id, current_id, content)`——写入目标恒为自己〔S-53〕
18. `read_shadow(card_id 可选,缺省自己)`：返回体三字段 `{owner_card_id, updated_at, content(影子文件全文)}`（代拍 2）；带 id 时先归一（容忍 shadow_ 前缀）再 `can_read` 校验（复用现有判据，不另写）；影子不存在返回 error〔S-54，对齐 A14 read_card 返回形态〕
19. 两工具 schema 挂进 `build_tool_schemas()`（delete_card 之后）；描述挂进 prompts.py「## 工具」段 delete_card 行之后〔S-55＋R4 闭合〕

### 改件五 · 存量补跑
20. 新建根目录 `backfill_shadows.py`（与 restore_from_snapshot.py 同族：`sys.path.insert(BASE_DIR)`）：扫 works/ 全部 work_*，逐 card 调 `shadow_store.derive_shadow`（同一共用函数）；单卡失败记录 work/card/异常后跳过；结束打印「新建/已存在跳过/失败」计数；无重试无超时〔S-59/S-63〕

### 改件六 · 推送（裁定 1）
21. 机制＝上级卡在自身调用中经 `read_shadow` 拉取（B10/R5 闭合定案）。落地物＝read_shadow 工具＋其描述（工具 schema 描述与 prompts 描述均写明「上级卡可借此查看任一子卡的影子（子卡影子的更新由此到达上级）」）。routes.py 零改动、无 SSE、不向上级正文写入、context_assembler 既有原则不破〔S-57/S-58〕

### 改件七 · 失败处理与打点
22. update_shadow 失败：异常上抛，execute_tool 外层既有 try 包裹转 error 字段原文给模型〔S-62〕
23. 补跑单卡失败：记录后跳过继续＋计数（本次实跑 34 新建/0 跳过/0 失败）〔S-63〕
24. 无重试、无超时、无轮次上限、无任何闸〔S-64〕
25. 三类动作日志：shadow_store 内 `logger.info("影子写入/影子删除 …")`＋派生失败 warning；工具调用走既有 tool_events〔S-66~68〕

### 部署面外改动（须报备）
- **测试实例专用**：data_snapshot.py SNAPSHOT_ROOT 派生修复（见第零节，生产行为等价故**未部署到生产**）；.gitignore 补 works/ 规则＋撤出已暂存 works；新建 start_test.bat／run_once.cmd（PORT=5701＋日志重定向）；restart_safe_5701.py（restart_safe.py 同判据副本，仅 PORT=5701＋start_test.bat 两处差异——原版硬编码 5700，在测试目录直跑会误杀生产）
- 测试实例代码同步（执行前置）：生产工作区 5 文件（card_store/card_tools/work_store/data_snapshot/workspace.js）→ 测试实例，使两实例代码一致后施工

---

## 三、验收实测（V1–V13 全部实跑）

| # | 判据 | 跑了什么 | 输出 | 结果 |
|---|---|---|---|---|
| V1 | shadows 条数＝正卡数、文件名＝正卡编号 | scripts/verify_shadow_acceptance.py 对 6 剧本对账集合 | work_001: 22==22 差集=[] … work_006: 1==1 | **PASS×6** |
| V2 | canvas.json 逐字节相同＋glob 条数不变 | 测试库 canvas.json 与生产镜像（无影子基准）逐字节 diff＋两侧 cards glob 计数 | 6/6 逐字节相同；22/22 7/7 2/2 1/1 1/1 1/1 | **PASS×6**（补跑后生产侧再验一次同样 6/6 相同） |
| V3 | 序号分配函数返回值不变 | `card_store._next_seq(wid)` vs 按 cards/ 独立计算的最大序号+1 | work_001: 25==25（shadows 目录 22 个在场）… 全对 | **PASS×6** |
| V4 | 删正卡不因影子报「存在子卡」 | 活服务(5701)：DELETE /works/work_006/cards/card_002 | `{"ok":true,"deleted":"card_002"}` http=200 | **PASS** |
| V5 | 删后影子 exists()==False 无残留 | 同上删除后 ls shadows/ | 仅 card_001.md；`test ! -f card_002.md` ✓ | **PASS** |
| V6 | 派生失败建卡仍 201＋warning | 活服务：`icacls shadows /deny *S-1-1-0:(OI)(CI)W` 后 POST 建卡，再解除 | http=201 card_id=card_002 error=None；日志行 `[WARNING] 影子派生失败（不阻断建卡）work=work_006 card=card_002: [Errno 13] Permission denied`；shadows 目录无 card_002.md | **PASS** |
| V7 | update_shadow schema 无 card_id | build_tool_schemas 检查（acceptance 脚本内） | props=['content']，required 无 card_id；read_shadow required=[] props=['card_id']；工具总数 16 | **PASS×3** |
| V8 | 四组样本调 read_shadow 记录 ok 与返回体 | **scripts/verify_shadow_read.py**（计划指定落点，未建 tests/）：临时剧本构造 根→A/B，B→C，A→B relation | ①子树(根读A)✓ ②血统链(A读根)✓ ③relations(A读B)✓ ④锚卡(C读根)✓；负样本 C读A→「越权：无权读取」✓；edit_card(shadow_A)→「影子卡只能由对应正卡经 update_shadow 修改」✓；返回体 keys=['content','owner_card_id','updated_at']；8/8 过后临时剧本已整体清理 | **PASS 8/8** |
| V9 | 写后连读 N 次历史行数不增 | update_shadow 后读 5 次，影子文件字节级比对＋查无「外部修改/修改历史」段 | bytes=350 前后全等；无追加段 | **PASS** |
| V10 | 最新快照批次含 shadows/ 路径 | 查 `推演agent_test_data_snapshots/work_001/` 最新非 daily 批次 | batch=20261003_193137 含 23 个 shadows 路径文件（生产侧最新批次同样含 22 个） | **PASS**（首跑 FAIL 引出第零节事故，修复后过） |
| V11 | 影子正文含三节对账三类 | 全部 34 个影子 grep 三节标题 | 34/34 含「## 进度／## 产物清单／## 迭代历史」，缺失=[] | **PASS** |
| V12 | 前端无影子编辑入口 | `grep -ri shadow static/ templates/` | 仅 4 处 CSS `box-shadow`，零功能入口 | **PASS** |
| V13 | 补跑后条数＝正卡数 | backfill_shadows.py 实跑（测试库 34 新建/0 失败；生产库 34 新建/0 失败） | 两实例 6 剧本 34/34 逐剧本相等 | **PASS** |

**补充**：离线预检（隔离临时目录）20/20 过——含「派生失败→建卡成功＋warning」「派生幂等」「编辑正卡影子一字节不动」「update_shadow 前后 canvas 逐字节相同」等前置等价判据。**生产部署窗口**：--check 首跑红灯（work_002/card_009:synthesize 在跑）→等待→绿灯（keep-alive 复核放行）→重启（2s 就绪）→探活 200→冒烟建卡 201＋影子派生＋删卡级联→补跑→终验 canvas 6/6＋34/34。

---

## 四、未做项（如实列）

1. **生产实例的 data_snapshot.py 未改**——SNAPSHOT_ROOT 派生修复只落在测试实例。理由：生产硬编码路径本就正确，部署面最小化。**遗留风险**：今后任何新实例复制生产代码都会复现跨实例污染，建议后续批改为派生式（对生产等价）。
2. **两实例代码均未提交**——生产工作区现为「原四处未提交改动＋prompts.py 新增改动＋shadow_store.py/backfill_shadows.py/scripts/ 三个未跟踪项」；提交归派发端。
3. **restart_safe.py 的 PORT 硬编码未修**——测试侧用 restart_safe_5701.py 副本规避；原版仍只认 5700。
4. **测试实例 test works 与生产 works 的持续同价**——本轮镜像后测试库又经历了 V8 临时剧本增删（已清理），当前两库 works 数据一致（34 卡＋34 影）；后续测试产生的数据漂移未做同步机制。
5. **生产冒烟未覆盖模型端到端**——冒烟走 HTTP API 直调 create/delete；「模型经 update_shadow 主动写影子」的端到端行为依赖 prompt 描述引导，未在生产发起真实模型对话验证（避免在生产制造对话数据）。
6. V10 测试侧批次内 shadows 文件数一次计数 23、复核 22——快照复制与统计时点的竞态观测，复核以 22==22 为准，未复现 23。

---

## 五、代拍复核栏

| 代拍 | 表态 |
|---|---|
| 代拍 1 影子 front matter 七字段 | **同意执行**。按七字段落地；补充一点执行层细化：`card_id` 字段取 `shadow_{正卡id}`（影子的可寻址 id），`parent`/`shadow_of` 取正卡 id——七字段名与代拍逐字一致，仅为 card_id 的取值定了语义，文件名仍＝正卡编号 |
| 代拍 2 read_shadow 返回体三字段 | **同意执行**。返回 `{owner_card_id, updated_at, content}`，content＝影子文件全文（含 front matter），逐字段对齐方案 4.4/R7 |
| 代拍 3 补跑脚本落点与命名 | **同意执行**。`backfill_shadows.py` 落项目根，sys.path 手法与 restore_from_snapshot.py 同族 |
| 代拍 4 三节标题用字 | **同意执行**。「## 进度／## 产物清单／## 迭代历史」，与用户 15:05 三类逐字对应，V11 全量核过 |
| 代拍 5 C 级处理粒度 | **同意执行**。R1–R5 全部按「实读→写文号→闭合」处理完毕，无一停手；B 级十处全部回对一致，无触发停改 |

（五条均未见与用户原意冲突之处，无需推翻。）

---

## 六、三笔悬账随书交出

1. **「对账三张表与红灯账 JSON 全盘不存在（确认未落盘）」——现已过时，实际已落盘。** `docs/exploration/` 下四件齐备：THINK-ECO-XX-对账-三张表-20261003.json、对账-方案条目清单、对账-上级条目清单、红灯账。文件内 meta 注明：p3 曾声明落盘而 p5 首轮读盘核实不在盘上，**由 p5 于 18:38 补落**，并随提交 5a41cf8 入库。三张表正向表结论：方案条目清单 68 条 → 68/68 全部有落点；红灯账：5/5 全消。
2. **「方案链条目清单 34/36 数字未查实」——已查实，与盘上不符。** 上级条目清单（chain=proposal）实有 **36 条**（R-01~R-22＝22、C-01~C-09＝9、B-01~B-05＝5），序号连续零缺口，即 36/36。「34/36」数字在盘上找不到对应物。
3. **工作区四处未提交改动保持原状——已核。** 生产 git status：TEMP_LEDGER.md／card_tools.py／card_store.py／work_store.py／workspace.js 修改态与执行前一致；部署文件逐字携带原未提交改动（card_store.py L19 无 git_auto_commit 的 import 退役、card_tools.py 四处 asyncio.to_thread 并发修复均在），备份存 `推演agent_backups/pre_shadow_deploy_20261003/`（含补跑前 6 剧本 canvas 基线）。

---

**终态一句话**：影子卡机制七改件全部落地；测试实例 V1–V13＋离线预检全绿；生产经判据绿灯重启、探活、真实冒烟、存量补跑后达到同构终态（34 卡／34 影／canvas 未动／快照含影）；执行中发现的快照根跨实例污染缺陷已修复并全量清扫，生产零损失。

# T-0009 阶段一 · 编排金样本 manifest

> 抓取命令：`.venv/Scripts/python.exe tests/_golden/capture_golden.py`
> 拆分前抓取时间：2026-10-08 16:37–16:38（拆分前 routes.py 999 行）
> 拆分后回放时间：2026-10-08 16:52–16:53（拆分后 routes.py 619 行 ＋ message_runner.py）

## T-0010 协议变更钉（2026-10-08 20:12 重抓）

**T-0010 协议变更钉：信封投影文本 + id 形态 + BASE_PROMPT t14-1。**

| 项 | 改前 | 改后 |
|---|---|---|
| 工具结果 | 成功＝裸 dict；失败＝`{"error": "文案"}` | 统一信封 `{ok, error, data}`，失败 `error={code,message}` |
| 回灌模型文本 | 两处各自 `json.dumps(tool_result)` | `card_tools.render_tool_result` **单一来源**：成功＝data 的 JSON；失败＝`[tool_error code=<码>] <说明>` |
| `tool_call_id` | `call_{i}` | `call_{run_tag}_{rounds}_{i}`（run_tag＝pair_id 前 8 位） |
| `BASE_PROMPT_VERSION` | `tc-2` | `t14-1`（新增「工具错误码」小节） |
| `TOOL_TABLE_VERSION` | `tools-tc-2` | **不动**（工具面零变化） |

**新增 S4 剧本**（工具错误轮）：`edit_card` 打不存在的卡 →
`side_effects.tool_projection[0].content` 钉死
`"[tool_error code=not_found] 卡片不存在: card_999"`，
同处钉死 id 形态 `call_<run_tag>_1_0`。

**S1/S2/S3 三剧本 12 件产物逐字节未变**（重抓前后 sha256 全部 SAME）——
信封化只改动了错误路径的文本形态与 id 形态，未扰动既有成功路径。

## 归一说明（为什么能逐字节比对）

原始响应含多处**天然逐次不同**的量，直接 diff 必然失败（与编排无关）。归一表：

| 字段 | 原值形态 | 归一为 | 为什么必须归一 |
|---|---|---|---|
| `pair_id` | uuid4().hex（32 位十六进制） | `<PAIR_ID>` | 每请求随机 |
| 事件 `ts` | `time.time()` | `<TS>` | 墙钟 |
| 事件 `seq` | 进程级单调计数器 | `<SEQ>`／行号改为本请求内相对序号 | 跨请求累计，非本请求序 |
| 卡`created_at`/`updated_at`、history 行时间戳 | ISO 时间串 | `<ISO>` | 墙钟 |
| `content_hash` | 对**含时间戳的全文**求值的 sha256 | `<CONTENT_HASH>` | 内容含时间戳⇒哈希逐次不同 |
| prompt 日志 `timestamp`、文件名 `%Y%m%d%H%M%S` | 时间戳 | `<ISO>`／`<STAMP>` | 同上 |

**归一只抹掉「逐次必然不同的量」，不抹任何编排语义**：事件类型与顺序、
payload 字段、最终 body 全字段、落盘文件路径与归一后内容哈希**全部参与比对**。
拆分前／拆分后用**同一套归一**（同一脚本同一函数）⇒ diff 全等即编排未变。

## 三剧本 × 三件套 hash清单

| 剧本 | 标题 | 三件套 sha256（sse / body / side_effects） |
|---|---|---|
| **S1** | 纯文本一轮交付（快车道·无工具·跳三席） | 见 `_t0009_golden_manifest.json` → `items[0].sha256` |
| **S2** | 工具循环（快车道＋read_card，含埋点与续跳） | 见 `_t0009_golden_manifest.json` → `items[1].sha256` |
| **S3** | 三席后置＋统合交付（chain 路径·含 viewpoints 与 T14 拼接） | 见 `_t0009_golden_manifest.json` → `items[2].sha256` |

（机读值以 `_t0009_golden_manifest.json` 为准；此处不重复抄写，避免两份真值漂移。）

## 分支覆盖（拆分后实测）

| 剧本 | 事件序列 | main 调用 | vp 调用 |
|---|---|---|---|
| S1 | `run_start → context → fastlane_route → round_start → deliver → proposal → done` | 1 | 0 |
| S2 | `run_start → context → fastlane_route → round_start → tool_call → card_op → round_start → deliver → proposal → done` | 2 | 0 |
| S3 | `run_start → context → fastlane_route → round_start → tool_call → tool_result → round_start → seats_enter → seat_start → seat_end → round_start → deliver → proposal → done` | 3 | 1 |

覆盖判据说明：
- S1＝快车道单模型直答（`mode==fastlane` ⇒ `break`，跳三席与统合官）。
- S2＝工具轮＋`card_op` 并表（快车道卡操作走 card_op 而非 tool_result）＋
  工具轮 user 位**留空**（2026-10-21 起不再有「工具结果已返回。」续跳占位） ＋ T14「normal」拼接。
- S3＝chain 路径：工具查证（tool_result）→ `seats_enter` → 三席（seat_start/seat_end）→
  统合轮（round_start phase=synthesize）→ deliver，含 viewpoints 追加。

## 确定性验证（§1.4）

每剧本**连跑 2 次**，三件套逐字节比对：S1/S2/S3 全部 `deterministic_2runs=True`。
（首轮曾因`content_hash` 与 `seq` 两处未归一而不稳定，已定位并归一，见报告 §7。）

## 阶段一闸门

**通过** —— 三剧本 × 三件套 × 2 轮全部逐字节一致，manifest 落盘，方进阶段二。
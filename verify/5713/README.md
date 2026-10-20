# verify/5713 · 通道试走（怎么复跑）

本目录是 5713 分支「卡间通信走通信线」的**真机试走**现场：一个 agent 自主完成
「发现另一张卡 → 建通信线 → 发消息 → 收到应答」。不是单测、不是打桩。

- `channel_walk.py` —— 驱动器（打真 HTTP、等真模型跑完、把证据落 JSON、机械判定）
- `evidence_channel_walk.json` —— 一次通过的实跑证据（含实例身份、卡片、通信线、卡间消息、两侧现状块、事件流原文、判定）

## 一、起隔离实例（不碰 5700）

```bat
cd /d D:\AI_station\推演agent\.worktrees\5713

:: .env 被 gitignore，worktree 里没有——从主仓复制一份（与 5711/lab5703 同做法）
copy /Y D:\AI_station\推演agent\.env .env

set ECO_TEST_SANDBOX=1
set ECO_SANDBOX_ROOT=D:\AI_station\推演agent\.worktrees\5713_sandbox
D:\AI_station\推演agent\.venv\Scripts\python.exe scripts\run_test_instance_5701.py 5713
```

`run_test_instance_5701.py` **不 import main**（main.py 在导入期会 `git add -A` + commit 并覆盖
`.server.pid`），所以起测试实例不会碰 git、不会抢生产进程身份。

**先验身再跑**：

```bat
curl http://127.0.0.1:5713/__instance__
```

回应里 `base_dir` 必须是 `…\.worktrees\5713`、`sandbox_root` 必须是 `…\5713_sandbox`、
`sandbox_enabled=true`。三者不对就**别继续**（`channel_walk.py` 自己也会先查这一关，不符直接退出）。

## 二、跑试走

```bat
cd /d D:\AI_station\推演agent\.worktrees\5713
D:\AI_station\推演agent\.venv\Scripts\python.exe verify\5713\channel_walk.py ^
    --base http://127.0.0.1:5713 --out verify\5713\evidence_channel_walk.json
```

它会：建项目 → 根卡开自动模式（子卡继承）→ 建应答方 B（auto、**无 goal**）→ 建发起方 A（auto + goal）
→ 轮询等链路自己走完 → 收证据 → 打印判定。

真模型多轮，通常 2～5 分钟；`--timeout` 可调（默认 900 秒）。

## 三、判定看什么（全 true 才叫通）

| 判据 | 含义 |
|---|---|
| `A_found_B` | A 从上下文「卡片清单」里真的找到了 B |
| `A_built_link_to_B` | A 的卡面 `links` 里有一条指向 B（线是它自己建的） |
| `A_sent_ask` | 卡间消息里有一条 `msg_ask` 从 A → B |
| `B_answered` | 有一条 `msg_answer` 从 B → A |
| `answer_reply_to_matches_ask` | 应答的头行 `reply_to` 正是 A 那条询问（从事件流原文核，不从推断） |
| `B_self_answered_wrote_status` | B 自己发的应答（不是系统代答）：B 现状块里落了**本次应答的事实锚点**——`msg_answer` 原文 / 应答自己的 msg_id / 被应答的询问 id，三者有其一即成立（判定里记 `B_self_answered_anchor` 备查）。原实现只找 ASCII 关键词，模型用中文写「回发应答」并抄上应答 msg_id 就误报 false（5713 三跑误报 2 次，2026-10-20 改为事实锚点） |
| `A_wrote_evidence_in_status` | A 收口时把四段结果写进了自己的现状块 |

## 四、清场

```bat
:: 停实例：任务管理器里结束监听 5713 的 python，或
for /f "tokens=5" %i in ('netstat -ano ^| findstr :5713 ^| findstr LISTENING') do taskkill /PID %i /F
```

沙箱根 `…\5713_sandbox` 可留着看数据，也可整个删掉（与生产数据无关）。

---

# 二、涌现实验场景台（exp_scenario.py）

## 它是什么
一条命令按 spec 起实验场景（work + N 个节点 + 可选观测角色），一条命令拉观测面并机械打分。
**内容归 CEO，接口归环境 owner**：节点使命全部写在 spec 里，工具里不硬编码
—— 替节点定使命等于替 CEO 定方向。

## 起场景
```bat
python verify\5713\exp_scenario.py up --spec my_exp.spec.json
python verify\5713\exp_scenario.py up --spec my_exp.spec.json --no-wait   :: 只建卡不点火
```
spec 格式见 `exp_scenario.spec.example.json`（模板里的使命是占位符，必须 CEO 本人填）。
建卡顺序是**先全部建成无 goal（不点火），最后统一点火** —— 否则先建的会立刻开跑，
而它需要的邻居还没建出来，观测到的是半成品场景。

## 拉观测（只读，可反复跑）
```bat
python verify\5713\exp_scenario.py observe --work work_030
```
输出：每张卡的卡面（parent／类型／auto／目标态／轮次／现状字数）、建线明细、
卡间信按边与 msg_type 统计、ask→answer 配对数（从事件流原文核 `reply_to`）、
双向往返数、越界被拒记录、观测角色台账（值班／待同步／判词全文），以及一句**判读**。

## 判读怎么读（最容易读错的地方）
- **「零建线零发信」先别当成"没有涌现"** —— 先看 `status_len` 与 `goal_states`：
  现状块接近空／目标未推进 = **回合根本没跑**，属仪器问题，不是行为结论。
- **「有 denied 记录」是好事**：说明节点**尝试过**接触但被拒。断路场景下
  「尝试了被拒」与「压根没试」的差别，就是全部结论的差别。
- **角色关注线不算自主建线**：组阁时系统自动写 links，工具已用 `is_role_edge`
  把它们排除在 `peer_link_count` 之外。把 CEO 组阁读成节点涌现，是最贵的一种错。

## 两处「仪器错误」的实证（都是本工具自己踩的，留档防复犯）
1. `read_events` 起初取顶层 `content/text`，而真实帧是 `{"type":"msg_answer",
   "data":{"content":"[[card-msg ... reply_to=X]]"}}` ⇒ 配对恒判 0。
   抓法：拿 **channel_walk 判 PASS 的同一跑次**当对照物，两边判不一致就是尺子错。
2. 起初把「角色关注线」算进建线数 ⇒ 会把 CEO 组阁读成节点自主建线。
   抓法：拿 `--no-wait` 未点火的自检场景对照（节点没跑却"建了线"，必是系统代建）。

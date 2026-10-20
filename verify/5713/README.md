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
| `B_self_answered_wrote_status` | B 自己的现状块里写了它发的 `msg_answer`（是它自己答的，不是系统代答） |
| `A_wrote_evidence_in_status` | A 收口时把四段结果写进了自己的现状块 |

## 四、清场

```bat
:: 停实例：任务管理器里结束监听 5713 的 python，或
for /f "tokens=5" %i in ('netstat -ano ^| findstr :5713 ^| findstr LISTENING') do taskkill /PID %i /F
```

沙箱根 `…\5713_sandbox` 可留着看数据，也可整个删掉（与生产数据无关）。
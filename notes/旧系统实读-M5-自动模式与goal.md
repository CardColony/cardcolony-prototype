# 旧系统实读 · M5 · 自动模式与 goal

- 实读范围（只读，未改任何文件）：`src/api/auto_driver.py`(667 行)、`src/goal/`(12 个 .py，共 1,748 行)、`src/thinkchain/`(6 个 .py，共 1,710 行)。
- 交叉核对（为回答「终止条件全集 / 与调度交互 / 上下文带入」而必须读的接线点）：`src/api/routes.py`、`src/api/serial_gate.py`、`src/api/scheduler.py`、`src/api/control.py`、`src/api/message_runner.py`、`src/api/context_regions.py`、`src/api/card_tools.py`、`src/storage/card_store.py`、`src/model_gateway/gateway.py`、`main.py`。
- 行号锚点格式：`文件:行号`（相对 `D:\AI_station\推演agent\src\`，`main.py` 相对仓库根）。
- 本文只登记事实，不下判断、不提改进。

---

## 0. 先说清一件事：仓库里有两套「自动续轮」实现，现行接线只用其中一套

这是本次实读最先必须钉住的事实，否则后面每一条都会读错。

| | A 套：`api/auto_driver.py` | B 套：`goal/driver.py` |
|---|---|---|
| 触发点 | 由调用方在回合末调 `auto_driver.after_turn()` | `routes._run_message` 的 finally 里调 `goal_driver.notify_idle()` |
| 现行是否被调 | **否**（无调用点，见下证据） | **是** |
| 判据 | `auto_mode` + `goal_state=="active"` + 有 goal + 无未终止子卡 | `auto_mode` + `goal.phase=="active"` + `activation=="armed"` + 有轮次预算 + 无未终止子卡 |
| 轮次计数 | `goal_rounds += 1`（写 `cards` 投影列） | 追加 `goal_round` 事件 → fold 推进 `roundsStarted` → 投影回 `cards.goal_rounds` |
| 上限 | 无 | `max_goal_rounds`（默认 256） |

证据（`after_turn` 在 `src/` 内只有定义与自身递归，无外部调用点）：

```
api/routes.py:2582-2584
    # [goal 域 2026-10-20] 旧 `auto_driver.after_turn` 的续轮职责已整体移交
    # `src/goal/driver.py`（idle 事件驱动，见上方 finally）；父卡唤醒改由
    # `notify_parent_idle` 承担。此处不再调 after_turn——两套并存会双起点火。
```

```
api/routes.py:2540-2547
        # [goal 域 2026-10-20 · 照抄 dsh] **idle 事件**：成功/异常/取消三条路径
        # 都报一次。这就是「自动模式会不断唤醒」的落点——不再依赖回合尾部的
        # 顺序调用（原版 ctx.on('agent/status', idle)）。
        try:
            goal_driver.notify_idle(work_id, card_id, outcome)
            goal_driver.notify_parent_idle(work_id, card_id)
        except Exception as exc:  # noqa: BLE001 驱动异常不影响回合返回
            logger.warning("[goal] idle 通知失败 card=%s: %s", card_id, exc)
```

后果（事实层面）：`auto_driver` 里的 `after_turn` / `wake_verdict` / `_resume_awaiting` / `_bump_rounds` / `_idle_stop`（空转止损）/ `_giveup_await_resume`（等子卡活锁兜底）在现行接线下**均无调用路径**。`auto_driver` 里仍然活着的是：`kick()`（起轮）、`will_continue()`（被 routes 用于执行锁 keep/release）、`resume_card()` / `reactivate_card()`（人工恢复端点）、`ensure_goal_from_message()`（首条消息建目标）、`has_active_child()`（被 goal 域反向调用）。

---

## 1. 自动模式

### 1.1 怎么开启：开关字段名

- 开关字段：`cards.auto_mode`（INTEGER 列）。读判据 `bool(meta.get("auto_mode"))`。
  - `api/auto_driver.py:201-202`
    ```
    def is_auto(meta: dict[str, Any]) -> bool:
        return bool((meta or {}).get("auto_mode"))
    ```
  - `goal/driver.py:196-201`
    ```
        try:
            from ..storage import card_store
            return bool((card_store._meta_of(work_id, card_id) or {}).get("auto_mode"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[goal] 读 auto_mode 失败，按关闭处理 card=%s: %s", card_id, exc)
            return False
    ```
- 谁能改这个字段：PATCH 白名单里的用户可写键，`api/routes.py:59-64`
  ```
    # [自动模式 2026-10-17 · 阶段2冒烟实测补漏] auto_mode 是**用户开关**（U1：
    # 任何卡随时可切），前端 toggleAutoMode 经 PATCH {updates:{auto_mode}} 落库。
    # 本集合漏了它 ⇒ 点击开关被 400「updates 含不允许的键」，开关整体不可用
    # （移植自 Cardlo 时漏抄，冒烟抓到）。goal/goal_state/goal_rounds **不放**
    # ——目标由消息/update_goal 设，不给用户直改通道。
    "auto_mode",
  ```
- 开启后还差一个「目标」。auto 卡收到**首条消息**时，该消息正文成为 goal：
  `api/routes.py:1687-1694` 调 `auto_driver.ensure_goal_from_message`；实现走 goal 域 `create`：
  ```
  api/auto_driver.py:633-643
      try:
          from ..goal.runtime import GoalError
          from ..goal.service import get_service
          from ..goal.types import CreateGoalRequest
          _svc = get_service()
          _cur = await _svc.get(work_id, card_id)
          if _cur is None or _cur.phase == "complete":
              await _svc.create(work_id, card_id, CreateGoalRequest(objective=goal))
          else:
              await _svc.resume(work_id, card_id, _cur.ref())
          return True
  ```
  注意：`auto_driver.py:650-661` 之后那段直接 `update_card({goal, goal_state, goal_await_wakes})` 的代码位于 `return True` 之后，**不可达**。
- 建卡即起跑：`card_store.create_card` → `scheduler.enqueue` → `auto_driver.kick`
  （`storage/card_store.py:1071-1079`、`api/scheduler.py:55-78`）。

### 1.2 一轮结束后怎么触发下一轮（现行驱动逻辑）

调用链（全部在 `routes._run_message` 的 finally 内，互斥体与执行锁处理之后）：

1. `routes.py:2544` `goal_driver.notify_idle(work_id, card_id, outcome)`
2. `goal/driver.py:99-119` `notify_idle`：`outcome in ("error","max-tokens")` ⇒ `disarm` 并返回；`"aborted"` ⇒ 视 attempt 阶段 `pause` 或 `disarm`；否则 `request_drive`
3. `goal/driver.py:142-153` `request_drive`：置 `requested`，同卡只跑一个驱动协程
4. `goal/driver.py:156-168` `_run_loop` → `drive()`
5. `goal/driver.py:204-241` `drive()` 逐条判据：

```
goal/driver.py:204-241
async def drive(work_id: str, card_id: str, st: Optional[DriverState] = None) -> None:
    """空闲检查点：有 active+armed 目标且还有预算 ⇒ 预留下一轮。"""
    st = st or _state_for(work_id, card_id)
    if st.stopping or st.competing_queued:
        return
    if not _auto_enabled(work_id, card_id):
        logger.debug("[goal] auto_mode 已关，不驱动 card=%s", card_id)
        return
    service = get_service()
    goal = await service.get(work_id, card_id)
    if goal is None or goal.phase != "active" or goal.activation != "armed":
        ...
        return
    if goal.rounds_started >= goal.max_goal_rounds:
        await service.block(work_id, card_id, goal.ref(), "round-limit",
                            f"Goal reached its configured limit of {goal.max_goal_rounds} rounds.")
        return
    if _yield_to_children(work_id, card_id):
        logger.debug("[goal] drive 让位给子卡 card=%s", card_id)
        return
```

6. `goal/driver.py:228-234` 组 `RoundAttempt`（`goal_id/revision/round`）→ `_spawn_goal_round`
7. `goal/driver.py:244-264` `_spawn_goal_round`：**先**追加一条 `goal_round` 事件（几十字节）**再**起轮：

```
goal/driver.py:251-263
    # [轮次脱钩 2026-10-20] 先把这一轮记进事件流（几十字节），再起轮。
    # 会话里**一个字都不写**：目标/目标态/已跑轮次已由 system prompt 的
    # 「自动模式·目标」区每轮渲染，塞进会话只会逐轮累积同一份文案
    # （实测 card_005 41 轮 = 30KB 重复目标）。
    await event_log.append(work_id, card_id, {
        "run_id": "", "round": 0, "type": "goal_round",
        "data": {"goalId": source.goal_id, "revision": source.revision,
                 "round": source.round},
    })
    message_runner._spawn_tracked(  # noqa: SLF001 与 auto_driver 同一条起轮路径
        _routes._run_message(work_id, card_id, content, None,
                             append_user=False, source=source.as_json())
    )
```

8. `routes.py:2625-2630` 回合进入模型**之前**再校验预留（goal id/revision/round 未变、未 stale/cancelled），失效则不调模型直接结束。
9. 下一轮提示词 = `goal/prompt.py:13-33` 的 `<goal_round>` 模板（含 `Round: {round}/{max_rounds}`）。

另外存在第二条起轮路径 `auto_driver.kick()`（`api/auto_driver.py:503-557`），调用点：
- `api/scheduler.py:64`、`api/scheduler.py:92`（建卡点火 / worker 出队）
- `api/routes.py:1932`（`/goal` 命令 create|edit 之后）
- `api/auto_driver.py:384`（`resume_card`）、`api/auto_driver.py:423`（`reactivate_card`）
- `api/auto_driver.py:665-667`（`kick_if_ready`，建卡即起跑）

`kick` 的前置判据只有四条，**不读 max_goal_rounds**：

```
api/auto_driver.py:514-523
    # consume-once：先取走，再走任何前置判据（早退也不残留）
    resume_note = _take_resume_note()
    meta = _meta(work_id, card_id)
    if not is_auto(meta):
        return None
    if str(meta.get("goal_state") or "") != GOAL_ACTIVE:
        return None
    goal = str(meta.get("goal") or "").strip()
    if not goal:
        return None
```

并且 `kick` 起轮时**不传 `source`**（`auto_driver.py:553`：`_run_message(work_id, card_id, content, None, append_user=False)`），而 `routes.py:2491` 是 `turn_source = dict(source) if source else {"kind": "user"}` ⇒ **kick 起的回合被标记为「直接人类回合」**，drive 起的回合被标记为 `{"kind":"goal",...}`。这直接决定 `update_goal` 的权威判定（见 3.4）。

### 1.3 终止条件全集（逐项带行号）

> 「停」分三层：**目标态终止**（不再起轮）、**本轮起轮被拒**（让位/闸）、**回合内中止**。下面全列。

**A. 模型显式声明（goal 域三态）**

| # | 条件 | 落点 | 行号 |
|---|---|---|---|
| A1 | `update_goal(action=complete)` → `phase=complete`，`drive` 首判即返回 | `goal/tools.py:280`、`goal/service.py:311-313`、`goal/driver.py:216` | 见下 |
| A2 | `update_goal(action=blocked, blocked_reason=...)` → `phase=blocked` | `goal/tools.py:281`、`goal/service.py:315-325` | — |
| A3 | `update_goal(action=pause)` → `phase=paused` | `goal/tools.py:246-247`、`goal/service.py:289-290` | — |
| A4 | `blocked` 的最低轮次门槛：goal 轮里 `rounds_started < 3` 直接拒（**不是刹车，是门槛**） | `goal/tools.py:269-274` | 见下 |

```
goal/tools.py:266-274
        reason = arguments.get("blocked_reason")
        if action == "blocked" and not _has_text(reason):
            raise GoalError("blocked_reason is required with action blocked", "GOAL_TOOL_INVALID_UPDATE")
        if (action == "blocked" and granted["kind"] == "goal-round"
                and granted["goal"].rounds_started < DEFAULT_BLOCKED_AFTER_ROUNDS):
            raise GoalError(
                f"blocked requires at least {DEFAULT_BLOCKED_AFTER_ROUNDS} consecutive goal rounds; "
                f"current round is {granted['goal'].rounds_started}",
                "GOAL_TOOL_BLOCK_THRESHOLD")
```

```
goal/tools.py:235-247
        if action in ("pause", "resume"):
            # pause 仍要求直接人类回合（停是人的决定）；resume 放开——
            # 模型判断「还有后续要干」时自己就能重新激活，不必等人来点。
            if action == "pause":
                authority.require_direct_human(source)
            ...
            if action == "pause":
                view = await service.pause(work_id, current_id, ref)
```

**B. 轮次上限（唯一一道累积性刹车）**：`goal/driver.py:220-223` → `block(code="round-limit")`（详见第 2 节）。

**C. 排队失败**：`goal/driver.py:235-241`

```
goal/driver.py:233-241
    try:
        await _spawn_goal_round(work_id, card_id, content, source)
    except Exception as exc:  # noqa: BLE001 排队失败 ⇒ block(queue-failed)
        st.attempt = None
        logger.warning("[goal] 续轮排队失败 card=%s round=%s: %s", card_id, round_no, exc)
        latest = await service.get(work_id, card_id)
        if latest is not None and latest.phase == "active" and latest.id == goal.id:
            await service.block(work_id, card_id, latest.ref(), "queue-failed",
                                f"Could not queue goal round {round_no}: {exc}")
```

**D. 回合异常 / 截断 ⇒ 撤销续轮（不自动重试）**：`goal/driver.py:104-110`

```
goal/driver.py:104-110
    st = _state_for(work_id, card_id)
    st.competing_queued = False
    if outcome in ("error", "max-tokens"):
        # 原版：agent/error 与 turn/end(max-tokens) 都 ⇒ disarm（异常结果不自动重试）
        logger.warning("[goal] 回合异常/截断，撤销续轮 card=%s outcome=%s", card_id, outcome)
        disarm(work_id, card_id)
        return
```
`outcome` 的取值来自 `routes.py:2496-2511`（`ok` / `aborted` / `error`）。

**E. 回合被取消 ⇒ pause 目标（绝不自动重启）**：`goal/driver.py:111-118` + `122-138`

```
goal/driver.py:111-118
    if outcome == "aborted":
        attempt = st.attempt
        if attempt is not None and attempt.phase in ("claimed", "admitted"):
            attempt.cancelled = True
            asyncio.get_running_loop().create_task(_pause_cancelled(work_id, card_id))
        else:
            disarm(work_id, card_id)
        return
```

**F. 父卡让位（不是终止，是不起轮）**：`goal/driver.py:171-182` + `224-226`；子卡终止后由 `notify_parent_idle` 叫醒父卡：`goal/driver.py:303-333`。

**G. 用户把 auto_mode 关掉 ⇒ 不再驱动**：`goal/driver.py:209-213`（注释写明这是 2026-10-24「停不下来」修复点）。

**H. 人工停止 / 强制停止**
- `control.stop_auto`：`api/control.py:263-...`，对 auto 子树逐个走 `goal.service.pause()`（`control.py:296-307`），并 `serial_gate.release_work`（`control.py:339`）。
- `control.halt`：置 `_HALTED` 标记 + 杀当轮（`api/control.py:46-55`）；被 halt 的卡**拒绝一切内部唤醒**：`routes.py:2404-2437`（`source_kind != "human"` ⇒ 返回 `{"error":{"code":"halted"}}`）。goal 驱动起轮不传 `source_kind`（默认 `"internal"`，`routes.py:2403-2408`），故受此闸约束。
- 沉默卡同样拒内部唤醒：`routes.py:2438-2449`。

**I. 进程重启 ⇒ 全部 disarmed（等于自动续轮全停）**
- `goal/service.py:8-11` 文档 + `goal/service.py:137`（`_seed` 一律 `activation="disarmed"`）
- `main.py:194`：「activation 永不落盘 ⇒ 进程重启后全部 disarmed，必须人显式 /goal resume」
- `drive` 判据含 `goal.activation != "armed"`（`driver.py:216`）

**J. auto_driver 层的停（现行接线**无调用路径**，仅登记）**
- 空转止损 `_IDLE_PAUSE_THRESHOLD = 3`：`api/auto_driver.py:64`、实现 `157-191`（连续 N 轮零工具 ⇒ 写 `goal_state=paused`）
- 等子卡活锁兜底 `_AWAIT_RESUME_DEADLOCK_GIVEUP = 3`：`api/auto_driver.py:123`、判定 `322-324`、留痕 `257-279`
- 卡树防环 `_CARD_TREE_CYCLE_GUARD = 32`：`api/auto_driver.py:51`、使用 `607-610`
- 轮次告警线 `_ROUNDS_WARN = 200`：`api/auto_driver.py:65`、`497-499`（只 warning，不拦）

**K. 串行执行锁的「停」＝没有停（饿死）**：`api/serial_gate.py:17-19`
```
★ **死锁代价**（用户已知并接受，迭代方案 §8）：某卡永久不终止（模型不声明完成）⇒
  队列后面全部饿死。这是「无闸 ＋ 串行」的固有代价；兜底＝手动停止（停止释放锁）。
★ 本模块**不设任何超时**——超时即是一种闸（拆闸铁律）。
```

**L. 回合内的中止（不是轮次停）**
- 工具循环无步数上限：`api/message_runner.py:20`「轮次上限：**无**（`[无闸改造 2026-10-02]` 循环不设封顶，模型停即收）」
- 人工停止在步边界消费：`api/message_runner.py:211-233`（`control.check` → STOP）
- 模型调用失败重发上限 `_RESUME_MAX_ATTEMPTS = 3`：`api/message_runner.py:635`（同一调用的重发，不是轮次）

**M. 其他起轮点（不受 goal 驱动约束，见 6.2，非穷举）**：`card_tools.py:1160`、`message_runner.py:1837/1946`、`function_room.py:685/743`、`role_room.py:1065/1103`。

---

## 2. 轮次控制（重点：是否存在累积性刹车）

### 2.1 结论（明确）

1. **存在且只存在一道「累积性轮次刹车」**：`goal.max_goal_rounds`，默认值 `256`。
   - 常量：`goal/runtime.py:17-18`
     ```
     #: 部署缺省轮次上限（原版 ``defaultMaxGoalRounds: 256``）。
     DEFAULT_MAX_GOAL_ROUNDS: Final[int] = 256
     ```
   - 落地判据（唯一读取点）：`goal/driver.py:220-223` → `service.block(..., "round-limit", ...)`
   - 二次校验（fold 层，非法轮次直接拒）：`goal/fold.py:311-319`
     ```
             current = state.goal
             if (current is None or current.phase != "active"
                     or gid != current.id or rev != current.revision
                     or rnd != state.rounds_started + 1
                     or rnd > current.max_goal_rounds):
                 raise GoalStreamError(
                     f"goal round at session event {event.get('seq')} is not the next admitted round of the active goal")
             state.rounds_started = rnd
     ```
   - `resume` 时的预算校验：`goal/service.py:300-305`；fold 同判 `goal/fold.py:225-226`。
2. **墙钟：不存在。**
   - 唯一相关的流静默超时已被显式设为 `None`：`model_gateway/gateway.py:792`
     ```
     STREAM_IDLE_TIMEOUT: Optional[float] = None
     ```
     其上方注释（`gateway.py:776-791`）明确写「值改 **None**（`asyncio.wait_for(timeout=None)` = 不设超时）……这与被否掉的"墙钟闸"是两回事」。
   - 串行执行锁无超时：`serial_gate.py:19`。
   - `thinkchain/board.py:202` 的 `LOCK_TIMEOUT = 1.0` 只是**板文件锁**，不是轮次墙钟。
3. **token 累计上限：不存在。**
   - 全仓 `goal/`、`api/auto_driver.py`、`thinkchain/` 内检索 `token` 仅命中：注释文字（`auto_driver.py:14/49/59/321`）、`goal/driver.py:102/106` 的 `outcome == "max-tokens"`（这是**单次模型调用**被截断的事件名，不是累计预算）、`goal/authority.py:39-40` 的 `ContextVar` token 变量名。无任何 token 计数器/阈值。
4. **auto_driver 自身零刹车**：`auto_driver.py:14-16` 自述「不设任何上限（U4/U5）：轮次 / 墙钟 / token 全不设」，代码核对一致（`_CARD_TREE_CYCLE_GUARD=32` 仅用于 parent 链防环，`_ROUNDS_WARN=200` 仅告警，`_IDLE_PAUSE_THRESHOLD=3` 治「零工具轮」，均与「能跑多少轮」无关；且这些路径现行无调用点，见 0 节）。
5. **刹车覆盖面有限（机械事实）**：`rounds_started` 只由 `goal_round` 事件推进（`goal/fold.py:299-319`），而该事件只在 `goal/driver.py:255-259` 一处产生。因此：
   - `auto_driver.kick` 起的轮**不计入** `rounds_started`（`kick` 内无任何计轮代码，`auto_driver.py:503-557`）；
   - 其余内部唤醒点（6.2）起的轮也不计入；
   - ⇒ `max_goal_rounds` 实际只约束「goal 驱动起的那部分轮」。

### 2.2 检索过程（关键词 → 命中）

检索命令：`grep -rn --include=*.py -i "<kw>" api/auto_driver.py goal/ thinkchain/`

| 关键词 | 命中情况 |
|---|---|
| `max_rounds` | 11 处，全在 goal 域（`prompt.py:16/32`、`service.py:53-97/261/282`、`runtime.py:18`）；`auto_driver.py`、`thinkchain/` **0 命中** |
| `maxGoalRounds` / `max_goal_rounds` | 全在 goal 域类型/服务/fold/驱动（见 2.1 表） |
| `budget` | 2 处，均为 fold 错误文案（`fold.py:224/226`） |
| `limit` | `driver.py:14/221/222`（round-limit）、`step_region.py:37/153`（展示截断 800 字符） |
| `timeout` | 仅 `thinkchain/board.py:202/253/254`（板文件锁 1.0s） |
| `wall_clock` / `wallclock` / `deadline` | **0 命中** |
| `token` | 见 2.1 第 3 条（无计数/阈值） |
| `cost_` | 0 命中 |

补充检索：`grep -rn "goal_rounds"` 的写入点只有两处 —— `api/auto_driver.py:491`（`_bump_rounds`，现行无调用点）与 `goal/service.py:418`（`_project`，把 fold 的 `rounds_started` 投影进列）。故活着的轮次计数**只有** goal 域那一条。

---

## 3. goal

### 3.1 数据结构（字段全集）

- 纯类型 `goal/types.py`：
  - `GoalRef{id, revision}`：`types.py:20-28`（比较并交换身份）
  - `GoalBlockReason{code, message}`：`types.py:31-39`
  - `GoalSnapshot{id, revision, objective, phase, max_goal_rounds, blocked_reason}`：`types.py:42-66`；JSON 键为 camelCase（`maxGoalRounds`、`blockedReason`），且 `blockedReason` **只在 phase=blocked 时出现**（`types.py:61-65`）
  - `GoalView{id, revision, objective, phase, max_goal_rounds, rounds_started, created_at, updated_at, activation, blocked_reason}`：`types.py:72-95`
  - `CreateGoalRequest{objective, max_goal_rounds?}` / `EditGoalRequest{objective?, max_goal_rounds?}`：`types.py:98-107`
  - `GoalMessageSource{goal_id, revision, round}` → `{"kind":"goal","goalId","revision","round"}`：`types.py:110-119`
  - `GoalChanged{operation, ref, goal?}`：`types.py:122-128`
- fold 累加器 `GoalFoldState{goal, rounds_started, created_at, updated_at, last_ref, seen_goal_ids}`：`goal/fold.py:47-56`
- 持久化形状（一条 `goal/change` 事件，全量快照）：
  ```
  goal/service.py:383-388
          change = {
              "kind": GOAL_CHANGE_EVENT, "version": GOAL_CHANGE_VERSION, "operation": operation,
              "goal": snap.as_json(), "roundsStarted": cache.state.rounds_started,
              "createdAt": created, "updatedAt": self._next_mutation_time(cache),
          }
          self._commit(work_id, card_id, cache, change, activation)
  ```
  键集合被 fold 精确校验：`fold.py:35-44`（`_CLEAR_KEYS` / `_SNAPSHOT_KEYS` / `_GOAL_KEYS_BLOCKED` / `_GOAL_KEYS_PLAIN` / `_REF_KEYS` / `_REASON_KEYS`）。
- 投影到 `cards` 表的列（`goal/service.py:413-434`）：`goal`、`goal_state`、`goal_rounds`、`goal_id`、`goal_revision`、`goal_max_rounds`、`goal_created_at`、`goal_updated_at`、`goal_blocked_code`、`goal_blocked_message`。
- 另有 legacy 列 `goal_await_children`、`goal_await_wakes`（`storage/card_db.py:72`），现行读取点只在 `auto_driver`（`_await_children_flag` 229-243、`_await_wakes` 246-254），而这两个函数只被已死的 `wake_verdict` 使用。

### 3.2 状态值全集

`PHASES = {"active", "paused", "blocked", "complete"}`：`goal/types.py:17`；`auto_driver` 侧同名常量 `GOAL_ACTIVE/PAUSED/BLOCKED/COMPLETE`：`api/auto_driver.py:34-39`。
`activation ∈ {"armed","disarmed"}`：`goal/types.py:14`（进程内观测值，**永不落盘**）。

### 3.3 生命周期转移（代码为准）

| 操作 | 允许来源态 | 目标态 | activation | 行号 |
|---|---|---|---|---|
| `create` | 无 goal，或现有 goal 为 `complete` | `active`，revision=1，roundsStarted=0 | `armed` | `service.py:246-268`（`250-254` 拒非 complete 的已存在） |
| `edit` | 任意（不改 phase） | 同 phase | 不变 | `service.py:270-287` |
| `pause` | `active` | `paused` | `disarmed` | `service.py:289-290` |
| `resume` | `active`/`paused`/`blocked` 且 `rounds_started < max_goal_rounds` | `active` | `armed` | `service.py:292-309` |
| `complete` | `active`/`paused`/`blocked` | `complete` | `disarmed` | `service.py:311-313` |
| `block` | `active` | `blocked`（带 `blockedReason`） | `disarmed` | `service.py:315-325` |
| `clear` | 任意（须有 goal） | 无 goal（tombstone） | `disarmed` | `service.py:327-337` |

fold 层同样逐条硬校验（`goal/fold.py:204-234`），任何非法转移 `fail-loud` 抛 `GoalStreamError`。

补充事实（入口约束，非状态机约束）：
- `create_goal` 工具要求**直接人类回合**：`goal/tools.py:167`（`authority.require_direct_human`）。
- `update_goal(pause)` 要求直接人类回合：`goal/tools.py:238-239` ⇒ **goal 驱动起的轮（source.kind=="goal"）里，模型调 pause 会被拒**（`GOAL_TOOL_AUTHORITY_REQUIRED`，`goal/authority.py:70-74`）。而 `kick` 起的轮（source 缺省 → `{"kind":"user"}`，`routes.py:2491`）里 pause 是被允许的。
- `update_goal(complete|blocked)` 允许「直接人类回合」或「当前那一轮 goal round」：`goal/authority.py:90-99`、`goal/tools.py:260`。
- `edit` 在 complete 态会走 `create`（新生命周期）：`goal/tools.py:222-226`；`resume` 在 complete 态同样走 `create`：`goal/tools.py:248-252`。

### 3.4 版本 / revision 机制

- 每次变更 `revision = 当前 + 1`，`id` 不变：`goal/fold.py:199-201`；`edit` 里显式 `current.revision + 1`：`service.py:279`。
- 写入必须带精确 `GoalRef{id, revision}`，过期即拒：
  ```
  goal/service.py:352-362
      def _expect_current(self, cache: _GoalCache, ref: GoalRef) -> GoalSnapshot:
          current = cache.state.goal
          if current is None:
              raise GoalError("no current goal", "GOAL_NOT_FOUND")
          if ref.id != current.id or ref.revision != current.revision:
              raise GoalError(
                  f'stale goal ref "{ref.id}" revision {ref.revision}; '
                  f'current is "{current.id}" revision {current.revision}',
                  "GOAL_STALE_REVISION",
              )
          return current
  ```
- 模型侧必须先 `get_goal` 再抄 `goal_id`/`revision`（`goal/tools.py:365-373` 的工具描述明确要求）。
- `activation` 与事件 seq 绑定：只有**本进程刚写**的那条 `goal/change` 才保留 armed，其余一律 disarmed：
  ```
  goal/service.py:152-160
          for ev in events:
              fold_mod.apply_goal_event(cache.state, ev)
              if ev.get("type") == GOAL_CHANGE_EVENT:
                  seq = ev.get("seq")
                  if cache.pending_activation is not None and cache.pending_activation[0] == seq:
                      cache.activation = cache.pending_activation[1]
                  else:
                      cache.activation = "disarmed"
              cache.observed_seq = int(ev.get("seq") or cache.observed_seq) or cache.observed_seq
  ```

### 3.5 恢复（resume / pause）实现

四条恢复路径，各不相同：

1. **机制自动恢复（子卡完工叫醒父卡）**：`goal/driver.py:303-333` `notify_parent_idle` → `_notify_parent` → `request_drive(parent)`。前提是父卡 `goal.phase == "active"` 且 `armed`（`driver.py:322-324` 判「本卡还没终止 ⇒ 父卡继续让位」）。父卡若是 `paused`，此路径**不会**把它拉回 active。
2. **人工恢复端点** `POST /works/{w}/cards/{c}/auto/resume`：`routes.py:1871-1910` → `auto_driver.reactivate_card`（`auto_driver.py:388-424`）：`paused/blocked` 走 `resume_card`（清 await 位与 wakes 计数，`354-385`）；`complete` 走 `goal.service.create`（新生命周期，轮次归零）。
3. **模型自助恢复**：`update_goal(action=resume)`（`goal/tools.py:235-255`）——paused/blocked/complete 都可拉回；complete 走 `create`。
4. **父卡恢复子卡**：`resume_child(card_id)`（`goal/tools.py:295-361`）——须在自身子树内（`policy._is_in_subtree`）、目标卡须 `auto_mode=1`；先 `control.unhalt`（`320-324`），complete 走 `create`，active+armed 幂等返回，其余 `resume`；最后 `request_drive`。
5. **旧数据回填**：`goal/service.py:172-213` `_backfill_legacy`——DB 有 `goal_state` 但事件流无 `goal/change` ⇒ 补一条 create（+ 一条状态迁移），保证历史卡不「没有目标」。

---

## 4. 计划板（thinkchain）

### 4.1 是否有独立计划板数据结构

**有**，但**当前生产不启用**。

- 数据结构：`thinkchain/board.py:324-604` `PlanBoard`；落盘形状（`board.py:8-13` 文档 + `board.py:669-676` 建板）：
  ```
  thinkchain/board.py:669-676
          data = {
              "card_id": card_id,
              "name": name if isinstance(name, str) and name.strip() else "",
              "steps": norm_steps,
              "current": 0,
              "revisions": [],
              "created_at": card_store._now_iso(),
          }
  ```
- 步结构：`{id, prompt, criteria, deliverable, type, status, result}`（`board.py:650-652` 归一化；`board.py:509-511` insert 同形）。
- 落盘路径：`works/{work_id}/plans/plan_{card_id}.json`，tmp→`os.replace` 原子替换（`board.py:237-239`、`306-319`）。
- 内存注册表双键 `(work_id, card_id)`：`board.py:207`。
- 恢复序：内存 → 盘上重建 → `None`；JSON 坏则 `BoardRestoreError`（`board.py:708-731`）。
- 收口清理：删板文件 + 内存条目 + `.tmp`（`board.py:579-604`），由 `thinkchain/finalize.py:50-94` 在末步触发。

### 4.2 工具名（以代码为准：六件）

`thinkchain/plan_tools.py:303-466` `PLAN_TOOLS` 清单，逐件名字与行号：

| # | 工具名 | schema 起始行 | handler |
|---|---|---|---|
| 1 | `create_plan` | `plan_tools.py:304` | `_h_create_plan`（100-128） |
| 2 | `plan_reorder` | `plan_tools.py:361` | `_h_plan_reorder`（131-135） |
| 3 | `plan_insert` | `plan_tools.py:378` | `_h_plan_insert`（138-148） |
| 4 | `plan_remove` | `plan_tools.py:406` | `_h_plan_remove`（151-155） |
| 5 | `plan_modify` | `plan_tools.py:422` | `_h_plan_modify`（158-165） |
| 6 | `step_complete` | `plan_tools.py:446` | `_h_step_complete`（205-282） |

**注册受总开关控制，当前为关**：

```
thinkchain/plan_tools.py:475-480
    # [纯单模型模式 2026-10-17] 总开关关时**不注册**——防线放在被调方，
    # 即使调用方（card_tools）被并行作业重写，只要还调本函数就拦得住。
    from . import enabled as _tc_enabled
    if not _tc_enabled():
        logger.info("plan_tools 注册跳过：纯单模型模式（thinkchain.ENABLED=False）")
        return 0
```

```
thinkchain/__init__.py:30-31
#: 思维链总开关。False = 纯单模型模式（当前生产口径）；True = 原思维链全链路。
ENABLED: bool = False
```

⇒ 现行工具面上**不存在**这六件（注册数 0）。另有执行层关口 `_plan_gate`（`plan_tools.py:60-73`）要求 `policy.has_perm(..., SWITCH_PLAN)`。

补充事实：`plan_tools.py:1-6` 文档与 `plan_tools.py:347-348` 的 schema 里，`type` 的 enum 仍写 `["execute","think"]`，而代码值域 `VALID_STEP_TYPES = ["execute","think","delegate"]`（`board.py:91`）——schema 与代码值域不一致（仅登记，不评价）。

### 4.3 步骤标识怎么表示

- `id`：字符串，**计划内唯一**（建板校验 `board.py:630-633`；insert 校验 `board.py:485-489`）。无全局唯一约束、无前缀规则。
- 顺序即数组下标；「当前步」是**整数指针** `current`（`board.py:355-360` `current_step()`；越界＝全部推进完）。
- 步状态五态 `VALID_STEP_STATUSES = ["pending","doing","done","blocked","dropped"]`（`board.py:97`）；建板一律 `pending`（`board.py:652`），`step_complete` 置 `done`（`board.py:429`），其余三态由 `plan_modify` 写入（`board.py:544-549`）。

### 4.4 进度怎么存

- 指针 `current` + 每步 `status`/`result`；**推进时同步落盘**（`board.py:429-432`）：
  ```
  thinkchain/board.py:425-432
              st = steps[cur]
              if st["status"] == "done":
                  return {"ok": True, "advanced": False, "idempotent": True,
                          "prompt": st["prompt"], "all_steps_done": False}
              st["status"] = "done"
              st["result"] = result_summary  # 步成果落板（跨回合可恢复、提交轮可查）
              self._data["current"] = cur + 1
              await self._save_locked()  # 指针同步落盘（锁内原子，返回工具结果之前完成）
  ```
- 改板动作（reorder/insert/remove/modify）走**置脏异步合并落盘** + `revisions[]` 追加（`board.py:561-574`）。
- 指针重定位：改板后按 id 找回原当前步，找不到则 `min(old_cur, len-1)`（`board.py:555-559`）。

---

## 5. 上下文怎么带入下一轮

1. **整段会话历史每轮重放**：每回合开始 `prior = await session_facade.read(work_id, card_id)`（`routes.py:2667-2668`），交给 `context_assembler.build_context_bundle(..., history_source=state.prior)`（`message_runner.py:263-274`）。⇒ 上一轮的 assistant 正文、工具调用与工具结果都在同一个会话里，天然进下一轮。
2. **续轮提示词不落会话**：两条起轮路径都用 `append_user=False`（`auto_driver.py:551-554`、`goal/driver.py:260-263`）⇒ 提示词只进当轮 `user_prompt`，**不写档案**。
3. **事件流里只留「第几轮」**：`goal_round` 事件只带 `{goalId, revision, round}`（`driver.py:255-259`），不含结果正文。
4. **每轮 system prompt 重渲染目标事实**：152 区（`context_regions.py:1251-1271`）输出目标 / 目标状态 / 已跑轮次；非 auto 卡返回空串。
   ```
   api/context_regions.py:1257-1271
       m = meta or {}
       if not bool(m.get("auto_mode")):
           return ""
       goal = str(m.get("goal") or "").strip()
       state = str(m.get("goal_state") or "").strip()
       try:
           rounds = int(m.get("goal_rounds") or 0)
       except (TypeError, ValueError):
           rounds = 0
       return "\n".join([
           "## 自动模式 · 目标",
           f"- 目标：{goal or '（尚未设定——本卡收到的首条消息即成为目标）'}",
           f"- 目标状态：{state or '（未设定）'}",
           f"- 已跑轮次：{rounds}",
       ])
   ```
5. **是否有压缩/截断**：在 `api/context_assembler.py`、`api/message_runner.py` 内检索 `compact|压缩|裁剪|truncat|max_history|window` **0 命中**；历史按会话全量喂。另有 order 200「凝练记忆」区（`context_regions.py:556+`，`condense_fold`）——本轮未展开其是否压缩历史，见「未解」。
6. **有没有累积**：会话历史**逐轮累积**（每轮全量重放同一会话）；续轮提示词与目标文案**不累积**（`append_user=False` + 152 区每轮重算，注释 `driver.py:252-254` 记录过改前 41 轮 30KB 重复目标的问题）。

---

## 6. 与串行 / 调度模块的交互点

### 6.1 串行执行闸（`api/serial_gate.py`）

- 每 work 一把 `asyncio.Lock`，默认模式 `serial`（`serial_gate.py:42-58`；配置文件 `config/exec_mode.json`，缺失 fail-open 到 parallel）。
- 取锁：`routes.py:2477-2484`，位置在**卡级互斥体之后**：
  ```
  api/routes.py:2473-2478
      # [自动模式 2026-10-17 · 回灌 5700 块0.3 · 方案 §3.8] 起回合前经**执行闸**：
      # 串行模式下拿不到锁＝**排队等待**（await，不是 409——这是与上面卡互斥体的
      # 关键差别）；并行模式直接过；同卡已持有（＝本卡目标生命周期内继续跑）⇒
      # no-op。取在互斥体之后：同卡重入被互斥体挡在前面，闸上不会出现同卡双取。
      try:
          await serial_gate.acquire(work_id, card_id)
  ```
- 卡级互斥体（同卡 409，不排队）：`routes.py:2458-2467`。
- 放锁三处：
  - 回合失败：`routes.py:2538-2539`
  - 回合末按 `auto_driver.will_continue` 判定 keep：`routes.py:2574-2581`
    ```
    api/routes.py:2572-2581
        # 执行锁：本卡还要继续跑目标 ⇒ **保留**（「一张卡的整个目标生命周期」持有，
        # 满足 U6/U8「一张子卡跑完再下一张」）；否则释放，让排队者前进。
        try:
            keep = auto_driver.will_continue(work_id, card_id)
        except Exception as exc:  # noqa: BLE001 判据异常不得吃掉回合结果
            logger.warning("[auto] 回合末执行锁判定异常 work=%s card=%s: %s",
                           work_id, card_id, exc)
            keep = False
        if not keep:
            serial_gate.release(work_id, card_id)
    ```
  - 人工停止：`control.py:339` `serial_gate.release_work`
  - （另有一处 `card_tools.py:926` 在已退役的 `_h_update_goal` 里，见 6.3）
- 无超时 ⇒ 单卡不终止则整条 work 队列饿死（`serial_gate.py:17-19`）。

### 6.2 点火调度器（`api/scheduler.py`）

- 只有**建卡**走调度器：`card_store.create_card` → `scheduler.enqueue`（`storage/card_store.py:1071-1079`）；失败退回 `auto_driver.kick`。
- worker 取卡 → `auto_driver.kick` → **await 整个回合**再取下一个（`scheduler.py:81-102`）；并发上限 `max_turns` 默认 3（`scheduler.py:44-45`、`105-114`）。
- **goal 续轮不走调度器**：`goal/driver.py:260-263` 直接 `message_runner._spawn_tracked(_routes._run_message(...))`；`auto_driver.kick` 同样是 `_spawn_tracked`（`auto_driver.py:552-554`）。
- 其余起轮点（同样不经调度器、不推进 `rounds_started`；`grep "_run_message("` 结果，非穷举）：`card_tools.py:1160`（建卡首跑）、`message_runner.py:1837/1946`（卡间消息/未回应信重调起）、`function_room.py:685/743`（职能续跑）、`role_room.py:1065/1103`（角色回合）。

### 6.3 目标终止与锁的耦合（历史残留）

`api/card_tools.py:700-930` 里还有一个 `_h_update_goal`（旧简化版 `update_goal(action, reason, await_children)`），它直接写 `cards.goal_state`（`card_tools.py:889`）并在末尾释放执行锁（`card_tools.py:926`）。但工具注册表用的是 goal 域版本：

```
api/card_tools.py:3447-3459
    "create_goal": ToolDef(
        schema=goal_tools.CREATE_GOAL_SCHEMA,
        handler=goal_tools.h_create_goal,
        readonly=False,
    ),
    "update_goal": ToolDef(
        schema=goal_tools.UPDATE_GOAL_SCHEMA,
        handler=goal_tools.h_update_goal,
        readonly=False,
    ),
    # [恢复子卡 2026-10-20] 父卡把停下的子卡拉回自主推进的唯一手段。
    "resume_child": ToolDef(
        schema=goal_tools.RESUME_CHILD_SCHEMA,
        handler=goal_tools.h_resume_child,
        readonly=False,
    ),
```

⇒ `card_tools._h_update_goal` 在 `src/` 内无调用点（`grep "h_update_goal"` 仅命中定义 700 与注册 3453 的 `goal_tools.h_update_goal`），其 `goal_await_children` / `goal_await_wakes` 写入逻辑随之失效。

---

## 7. 未解（不猜）

1. **order 200「凝练记忆」区是否对历史做压缩**：`context_regions.py:556+` 的 `condense_fold` 只读到定义行，未读其取数与截断口径。故「历史是否被压缩」只能确定到「`context_assembler` / `message_runner` 两层无截断」，更上游未核。
2. **模型网关侧是否有 token 级请求裁剪**：本次只核到 `STREAM_IDLE_TIMEOUT = None`（`gateway.py:792`）与「无降级/无重试」注释；请求体组装阶段是否按 token 截断未读。
3. **`goal_await_children` / `goal_await_wakes` 两列在现行接线下是否还有其他读取方**：`grep` 只找到 `auto_driver` 的三个读函数（`_await_children_flag` / `_await_wakes`）与已退役的 `card_tools._h_update_goal`；未逐文件核前端/诊断接口。
4. **`config/exec_mode.json` 当前实际取值**：`serial_gate.exec_mode()` 读该文件，本次未打开该配置文件，故「当前是 serial 还是 parallel」未确认（代码默认路径是：文件缺失 → parallel；注释称默认 serial 由配置文件提供）。

---

## 8. 关键发现（3-5 条，均为机械事实）

1. **两套续轮实现并存，旧的那套已整体断线**：`auto_driver.after_turn` 及其下游（空转止损 `_IDLE_PAUSE_THRESHOLD=3`、等子卡死锁兜底 `_AWAIT_RESUME_DEADLOCK_GIVEUP=3`、`_bump_rounds`、`wake_verdict`）在 `src/` 内**无调用点**（`routes.py:2582-2584` 明写「此处不再调 after_turn」）。现行续轮全部由 `goal/driver.py` 的 idle 事件承担。
2. **累积性刹车只有一道：`max_goal_rounds`，默认 256**（`goal/runtime.py:18` → `goal/driver.py:220-223` 落为 `block("round-limit")`）。**墙钟不存在**（`STREAM_IDLE_TIMEOUT = None`，`gateway.py:792`；执行锁无超时 `serial_gate.py:19`），**token 累计上限不存在**（全量检索无计数器/阈值）。
3. **这道刹车只覆盖 goal 驱动起的轮**：`rounds_started` 仅由 `goal_round` 事件推进（`fold.py:299-319`，唯一产生点 `driver.py:255-259`）；`auto_driver.kick`（建卡点火、`/goal` create/edit 后、人工恢复）与 6.2 列举的多处内部唤醒点（`card_tools.py:1160`、`message_runner.py:1837/1946`、`function_room.py:685/743`、`role_room.py:1065/1103` 等，非穷举）起的轮**既不校验也不推进**这个计数。
4. **`update_goal(pause)` 在 goal 续轮里不可用**：`goal/tools.py:238-239` 要求直接人类回合；而 goal 驱动起轮带 `source={"kind":"goal"}`（`driver.py:262`）⇒ 模型在自动续轮中只能 `complete` / `blocked`（后者还要求 `rounds_started >= 3`，`tools.py:269-274`）。相反 `kick` 起轮不传 source，`routes.py:2491` 回落成 `{"kind":"user"}` ⇒ 同一张卡在不同起轮路径下，`update_goal` 的权限判定不同。
5. **计划板（六工具）在现行生产里不注册**：`thinkchain.ENABLED = False`（`__init__.py:31`），`plan_tools.register_into` 直接返回 0（`plan_tools.py:475-480`）。板子数据结构本身完整（`board.py:324-604`，步 `{id,prompt,criteria,deliverable,type,status,result}`、五态状态、`current` 指针、`works/{w}/plans/plan_{card}.json` 原子落盘），只是没有工具入口。

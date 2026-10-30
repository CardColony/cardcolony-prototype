# -*- coding: utf-8 -*-
"""自动模式「停不下来」——2026-10-24 用户实测的回归钉。

用户原话：「在开了自动模式启动后，就停止不了了，哪怕是切回手动模式，也无法再停止了」

**现场证据**（work_001/card_001 的 events + ops）：
```
15:04:21 turn_end → run_start（chars 571，没有 user_message）← 自动续轮
15:05:13 turn_end → run_start（chars 571）   ← 用户点了停止、auto_stop 也 paused 了
15:05:27 turn_end → run_start（chars 571）
15:05:41 turn_end → run_start（chars 571）
15:05:52 turn_end → run_start（chars 571）
15:06:13 turn_end → run_start（chars 571）
```
连停 5 次，每次停完立刻又起一轮，续轮 prompt 一模一样。

**两个独立缺口，缺一都停不住**：

① **`goal.driver.drive` 不看 `auto_mode`**（迁到 goal 包时丢了这道闸）——
   原 `auto_driver.will_continue` 第一句就是 `if not is_auto(meta): return False`。
   现在只看 `goal.phase`/`goal.activation` ⇒ **切回手动模式照旧续轮**。

② **`stop_auto` 写的是投影列 `cards.goal_state`**，而权威是事件日志 fold 出来的
   `goal.phase`。运行中进程缓存仍是 active ⇒ 照旧续轮，下一轮 `_project` 又把
   `goal_state` 写回 active ⇒ **点了停止也停不下来**。现在走 `service.pause()`。

判据：
S1 `auto_mode=0` ＋ 目标 active+armed ⇒ `drive` **不**续轮
S2 `auto_mode=1` ＋ 目标 active+armed ⇒ `drive` 续轮（不能修坏正常路径）
S3 `stop_auto` 之后，**权威 fold** 里 phase 变 paused（不是只改了列）
S4 `stop_auto` 在 `auto_mode` 已关时**也要**停目标（用户先关开关的正是故障现场）
S5 直接写 `goal_state=paused` 列**不算**停住（证明投影列不是权威——这是①的根因）
"""
from __future__ import annotations

import pytest

from src.storage import card_store


@pytest.fixture(autouse=True)
def _clean_goal_state():
    """goal 服务是进程单例，跨用例会带着上一个沙箱的事件 seq / 缓存 ⇒ 必须清。"""
    from src.api import message_runner, serial_gate
    from src.goal.service import get_service

    def _reset():
        get_service()._caches.clear()
        serial_gate.reset()
        message_runner._TRIGGER_TASKS.clear()

    _reset()
    yield
    _reset()


@pytest.fixture
def spawned(monkeypatch):
    """拦掉真实起轮，只记录「drive 真的排了一轮」。"""
    from src.goal import driver

    calls: list[tuple[str, str]] = []

    async def _fake_spawn(work_id, card_id, content, source):
        calls.append((work_id, card_id))

    monkeypatch.setattr(driver, "_spawn_goal_round", _fake_spawn)
    return calls


async def _mk_auto_card(work, *, auto_mode: bool = True, objective: str = "把 X 做完"):
    """建一张 auto 卡并给它一个 active+armed 的目标。"""
    from src.goal.service import get_service
    from src.goal.types import CreateGoalRequest

    wid = work["work_id"]
    c = await card_store.create_card(wid, "自动卡", "模型生成",
                                     parent=work["work_card_id"])
    cid = c["meta"]["card_id"]
    await card_store.update_card(wid, cid, {"auto_mode": 1 if auto_mode else 0},
                                 "测试：设自动模式")
    goal = await get_service().create(wid, cid, CreateGoalRequest(objective=objective))
    assert goal.phase == "active"
    return wid, cid, goal


# ---------------------------------------------------------------- ① auto_mode 闸


@pytest.mark.asyncio
async def test_drive_does_not_run_when_auto_mode_off(work, spawned):
    """S1：切回手动模式 ⇒ 即使目标 active+armed，也不再续轮。

    改前这条必挂（drive 压根不看 auto_mode）。
    """
    from src.goal import driver

    wid, cid, _goal = await _mk_auto_card(work, auto_mode=False)
    await driver.drive(wid, cid)
    assert spawned == [], f"auto_mode 已关却仍续轮：{spawned}"


@pytest.mark.asyncio
async def test_drive_runs_when_auto_mode_on(work, spawned):
    """S2：正常路径不能被修坏——auto_mode=1 时照旧续轮。"""
    from src.goal import driver

    wid, cid, _goal = await _mk_auto_card(work, auto_mode=True)
    await driver.drive(wid, cid)
    assert spawned == [(wid, cid)], f"auto_mode 开着却没续轮：{spawned}"


# ---------------------------------------------------------------- ② 走权威命令


@pytest.mark.asyncio
async def test_stop_auto_pauses_goal_authoritatively(work, spawned):
    """S3：`stop_auto` 之后**权威 fold** 里 phase 必须是 paused。"""
    from src.api import control
    from src.goal.service import get_service

    wid, cid, goal = await _mk_auto_card(work, auto_mode=True)
    await control.stop_auto(wid, cid)

    view = await get_service().get(wid, cid)
    assert view is not None
    assert view.phase == "paused", f"权威状态仍是 {view.phase!r}——停止没落到事件日志"
    assert view.activation == "disarmed", f"activation 仍是 {view.activation!r}"

    # 停住之后再 drive 也不该续轮
    await driver_drive(wid, cid)
    assert spawned == [], "停住之后仍然续轮"


@pytest.mark.asyncio
async def test_stop_auto_noop_for_non_auto_card(work, spawned):
    """S4（红线 2）：**非 auto 卡 ⇒ stop_auto 全 no-op**，返回形状逐字不变。

    这是既有契约，不许被「顺手也停一下」破坏。「切回手动模式停不住」由
    `drive` 的 auto_mode 闸解决（S1），不在停止端点里越界动非 auto 卡。
    """
    from src.api import control
    from src.goal.service import get_service

    wid, cid, _goal = await _mk_auto_card(work, auto_mode=False)
    r = await control.stop_auto(wid, cid)
    assert r == {"paused": [], "lock_released": False}, f"返回形状变了：{r}"

    view = await get_service().get(wid, cid)
    assert view is not None and view.phase == "active", \
        "非 auto 卡的目标不该被停止端点改动（红线 2）"


@pytest.mark.asyncio
async def test_writing_goal_state_column_does_not_stop(work, spawned):
    """S5：**证明根因**——只写 `cards.goal_state` 这个投影列，权威仍是 active。

    这是改前 `stop_auto` 的做法；这条断言立在这里，防止以后有人「优化」成写列。
    """
    from src.goal import driver
    from src.goal.service import get_service

    wid, cid, _goal = await _mk_auto_card(work, auto_mode=True)
    await card_store.update_card(wid, cid, {"goal_state": "paused"}, "测试：只写投影列")

    view = await get_service().get(wid, cid)
    assert view is not None and view.phase == "active", \
        "若这里变 paused，说明投影列成了权威——那 S5 的前提变了，要重审 ① 的理由"

    await driver.drive(wid, cid)
    assert spawned == [(wid, cid)], "投影列写着 paused，驱动却仍按权威 active 续了轮"


async def driver_drive(work_id: str, card_id: str) -> None:
    from src.goal import driver
    await driver.drive(work_id, card_id)

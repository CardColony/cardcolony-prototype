"""goal 域自检（照抄 dsh-goal 后的机械验证）。

跑法：
    .venv/Scripts/python.exe scripts/check_goal_domain.py

只碰临时沙箱（tempfile.mkdtemp），不碰任何真实 works。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


async def main() -> int:
    from src.storage import card_store, session_store, card_db, event_log, work_store
    from src.goal import service as goal_service, fold, driver, tools, authority, prompt
    from src.goal.types import CreateGoalRequest, EditGoalRequest, GoalRef
    from src.goal.runtime import GoalError

    base = Path(tempfile.mkdtemp(prefix="goal_check_"))
    works = base / "works"
    works.mkdir(parents=True)
    card_store.WORKS_DIR = works
    session_store.WORKS_DIR = works
    card_db.WORKS_DIR = works

    # 建项目 + 一张 auto 卡
    work = await work_store.create_work("自检")
    work_id = work["work_id"]
    created = await card_store.create_card(work_id, "子卡", "模型生成", card_type="sub_project")
    cid = created["meta"]["card_id"]
    await card_store.update_card(work_id, cid, {"auto_mode": 1, "goal": ""}, "自检")

    svc = goal_service.get_service()
    svc._caches.clear()

    print("== 1. create ==")
    view = await svc.create(work_id, cid, CreateGoalRequest(objective="把 A 做完", max_goal_rounds=5))
    check("revision=1", view.revision == 1, str(view.revision))
    check("phase=active", view.phase == "active")
    check("activation=armed", view.activation == "armed")
    check("rounds=0", view.rounds_started == 0)
    _row = card_db.read_sync(work_id, lambda c: card_db.get_card_row(c, cid), None)
    row = dict(_row) if _row is not None else {}
    check("投影 goal_state=active", row.get("goal_state") == "active", str(row.get("goal_state")))
    check("投影 goal_id 非空", bool(row.get("goal_id")))
    check("投影 goal_max_rounds=5", row.get("goal_max_rounds") == 5, str(row.get("goal_max_rounds")))

    print("== 2. 事件溯源 ==")
    events = event_log.read(work_id, cid)
    changes = [e for e in events if e.get("type") == "goal/change"]
    check("落了一条 goal/change", len(changes) == 1, str(len(changes)))
    folded = fold.fold_goal(events)
    check("fold 出 goal", folded.goal is not None and folded.goal.objective == "把 A 做完")

    print("== 3. CAS 版本校验 ==")
    try:
        await svc.pause(work_id, cid, GoalRef(view.id, 99))
        check("过期 revision 应被拒", False)
    except GoalError as exc:
        check("过期 revision 被拒", exc.code == "GOAL_STALE_REVISION", exc.code)

    print("== 4. 状态转移 ==")
    paused = await svc.pause(work_id, cid, view.ref())
    check("pause→paused", paused.phase == "paused" and paused.revision == 2)
    check("pause 后 disarmed", paused.activation == "disarmed")
    try:
        await svc.pause(work_id, cid, paused.ref())
        check("paused 再 pause 应被拒", False)
    except GoalError as exc:
        check("非法转移被拒", exc.code == "GOAL_INVALID_TRANSITION", exc.code)
    resumed = await svc.resume(work_id, cid, paused.ref())
    check("resume→active+armed", resumed.phase == "active" and resumed.activation == "armed")

    print("== 5. edit 只能改目标/上限 ==")
    edited = await svc.edit(work_id, cid, resumed.ref(), EditGoalRequest(objective="把 B 做完"))
    check("edit 生效", edited.objective == "把 B 做完" and edited.revision == 4)
    check("edit 保持 phase", edited.phase == "active")

    print("== 6. 续轮：roundsStarted 只由 goal 消息推进 ==")
    src = {"kind": "goal", "goalId": edited.id, "revision": edited.revision, "round": 1}
    from src.storage import session_facade
    await session_facade.append(work_id, cid, {"role": "user", "content": "第1轮", "source": src})
    await svc.disarm(work_id, cid)
    got = await svc.get(work_id, cid)
    check("rounds_started=1", got.rounds_started == 1, str(got.rounds_started))
    check("外部写入后 disarmed", got.activation == "disarmed", got.activation)

    # 跳号（round=3）必须被 fold 拒
    bad = {"kind": "goal", "goalId": got.id, "revision": got.revision, "round": 3}
    try:
        fold.apply_goal_event(fold.empty_goal_fold_state(), {
            "seq": 1, "type": "user_message", "data": {"content": "x", "source": bad}})
        check("跳号应被拒", False)
    except Exception:
        check("跳号被拒", True)

    print("== 7. 轮次上限 ==")
    svc._caches[(work_id, cid)].state.rounds_started = 5   # 打到上限
    spawned: list = []

    async def fake_spawn(work_id, card_id, content, source):
        spawned.append(source)

    driver._spawn_goal_round = fake_spawn  # type: ignore[assignment]
    await svc.resume(work_id, cid, (await svc.get(work_id, cid)).ref()) if False else None
    # 直接构造 armed + 满预算，看 drive 是否 block
    cache = svc._caches[(work_id, cid)]
    cache.activation = "armed"
    await driver.drive(work_id, cid)
    after = await svc.get(work_id, cid)
    check("满预算 ⇒ blocked(round-limit)",
          after.phase == "blocked" and after.blocked_reason and after.blocked_reason.code == "round-limit",
          f"{after.phase}/{after.blocked_reason}")

    print("== 8. 续轮提示词逐字 ==")
    text = prompt.render_goal_round_prompt(edited, 2)
    check("含 <goal_round>", text.startswith("<goal_round>\nObjective: "))
    check("含 Round: 2/5", "Round: 2/5" in text, text[:80])
    check("objective 被 JSON 引用", json.dumps("把 B 做完", ensure_ascii=False) in text)

    print("== 9. 权威校验 ==")
    authority.set_turn_source({"kind": "goal", "goalId": "g", "revision": 1, "round": 1})
    try:
        authority.require_direct_human(authority.current_turn_source())
        check("goal 回合不能 edit/pause", False)
    except GoalError:
        check("goal 回合不能 edit/pause", True)
    authority.set_turn_source({"kind": "user"})
    authority.require_direct_human(authority.current_turn_source())
    check("人类回合可以 edit/pause", True)

    print("== 10. fold 严格性 ==")
    st = fold.empty_goal_fold_state()
    fold.apply_goal_event(st, changes[0])
    bad_change = dict(changes[0]["data"])
    bad_change["operation"] = "complete"
    bad_change["goal"] = dict(bad_change["goal"], revision=1)   # 没 +1
    try:
        fold.apply_goal_change(st, fold.decode_goal_change(bad_change))
        check("revision 不 +1 应被拒", False)
    except Exception:
        check("revision 不 +1 被拒", True)

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    print(f"沙箱：{base}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

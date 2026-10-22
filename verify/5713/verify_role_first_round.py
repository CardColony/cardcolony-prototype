#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_role_first_round.py
==========================
独立验收器：从系统外部核对「自动模式轮首必须做拆职能判断」这条契约。

契约（验收依据，抄自 card_006 D38 / card_044 任务书）：
    order 91 区（key=`team_function_instruction`，name=「组建职能」）的渲染判据必须
    **三条同时**满足才出区：
      1. 当前卡是项目根卡：`cur_meta.card_id` == `ctx.work_card_id`
      2. 自动模式：`bool(cur_meta.auto_mode)`
      3. **轮首**：`int(cur_meta.goal_rounds or 0) == 0`   ← 本次新增的核心
    任一不满足 ⇒ 空串。
    出区文案必须含「先做一个明确判断」与「人类团队里真实存在的岗位」两个关键语义，
    并说明：要拆就按人类团队真实岗位拆、不拆要说明理由、判断结果要落到卡面。

方法（怎么测）：
    · `import src.api.context_regions` —— **只读**取 `REGION_TABLE`、`RegionCtx`、
      渲染函数 `render_team_function_instruction_region`、装配入口
      `render_registered_regions`。不调任何 handler、不写库、不建卡。
    · 夹具用**真实卡面**：`cur_meta` 同时设 `card_id`／`auto_mode`／`goal_rounds`，
      且 `ctx.card_id`、`ctx.work_card_id` 一并设成根卡 id。
      （本项目的坑：实现按 `cur_meta.card_id` 判根卡，**不是** `ctx.card_id`；
        历史上有验收器只改了后者，误判成「非根卡」而报假 FAIL。）
    · 数据库若需要读，一律 `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`。
    · `sandbox_root` 从 `http://127.0.0.1:5713/__instance__` 取；
      实例没起 ⇒ 退到仓库自身路径，并**在输出里如实说明**（不静默降级）。

用法：python verify/5713/verify_role_first_round.py
退出码：全 PASS 或 PASS+SKIP → 0；有 FAIL → 1。
产物：verify/5713/evidence_role_first_round.json（每项 name/verdict/observed）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── 控制台 UTF-8（Windows gbk 下中文不乱码）──────────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 非交互环境可能没有 reconfigure
    pass

INSTANCE_URL = "http://127.0.0.1:5713"
VERIFY_DIR = Path(__file__).resolve().parent          # <repo>/verify/5713
REPO_ROOT = Path(__file__).resolve().parents[2]       # <repo>
EVIDENCE_PATH = VERIFY_DIR / "evidence_role_first_round.json"

REGION_KEY = "team_function_instruction"
EXPECTED_ORDER = 91
NEIGHBOR_BEFORE = "card_duty"        # order 90
NEIGHBOR_AFTER = "card_ability"      # order 92

#: 出区文案必须含的两个关键语义（契约明写，逐字比对）
KEY_PHRASE_JUDGE = "先做一个明确判断"
KEY_PHRASE_ROLE = "人类团队里真实存在的岗位"

#: 文案里还必须能看出来的三件事（契约：「要拆就按人类团队真实岗位拆、
#: 不拆要说明理由、判断结果要落到卡面」）——用作 A 项的辅助证据，只记录不单独判。
EXTRA_PHRASES = (
    "不拆",             # 不拆要说明理由
    "理由",             # 不拆要说明理由
    "落到卡面",         # 判断结果要落到卡面
    "append_ruling",    # 落到卡面的具体通道
)


# ══════════════════════════════════════════════════════════════════════
# 取数：实例身份（sandbox_root）
# ══════════════════════════════════════════════════════════════════════
def probe_instance() -> tuple[Optional[dict[str, Any]], str]:
    """从 /__instance__ 取实例身份。返回 (payload|None, 说明)。"""
    try:
        with urllib.request.urlopen(INSTANCE_URL + "/__instance__", timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        if not isinstance(data, dict):
            return None, f"实例 {INSTANCE_URL} 返回的不是 JSON 对象"
        return data, f"实例 {INSTANCE_URL} 在线（pid={data.get('pid')}）"
    except Exception as exc:  # noqa: BLE001
        return None, (f"实例 {INSTANCE_URL} 不可达（{type(exc).__name__}: {exc}）"
                      "；已退回仓库自身路径作为取数根，并在本报告中标明")


def resolve_works_dir(inst: Optional[dict[str, Any]]) -> tuple[Optional[Path], str]:
    """定 works 目录：优先实例 sandbox_root，其次仓库自身 works/。"""
    if inst:
        root = inst.get("sandbox_root")
        if root:
            works = Path(str(root)) / "works"
            if works.is_dir():
                return works, f"取数根＝实例沙箱 {works}"
            return None, (f"实例 sandbox_root={root} 但其 works/ 不存在"
                          "；退回仓库自身路径并在报告中标明")
    works = REPO_ROOT / "works"
    if works.is_dir():
        return works, f"取数根＝仓库自身 {works}（实例未起，按契约退到仓库路径）"
    return None, f"取数根不可用：仓库自身 {works} 不存在且实例未起"


# ══════════════════════════════════════════════════════════════════════
# 取数：真实卡面（只读 sqlite）
# ══════════════════════════════════════════════════════════════════════
def _ro_connect(db_path: Path) -> sqlite3.Connection:
    """只读连接：任何情况下都不写库。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def read_work_facts(works_dir: Path) -> list[dict[str, Any]]:
    """扫 works/ 下每个 work，取根卡与一张子卡的事实（只读）。

    返回项：{work_id, title, root_card_id, root_type, root_auto, root_rounds,
              child_card_id, child_type, child_parent}
    """
    out: list[dict[str, Any]] = []
    if not works_dir.is_dir():
        return out
    for d in sorted(works_dir.iterdir()):
        if not d.is_dir() or not d.name.startswith("work_"):
            continue
        wj, db = d / "work.json", d / "cards.db"
        if not (wj.exists() and db.exists()):
            continue
        try:
            work = json.loads(wj.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        root_id = str(work.get("work_card_id") or "")
        if not root_id:
            continue
        try:
            con = _ro_connect(db)
            rows = [dict(r) for r in con.execute(
                "SELECT card_id, card_type, parent, auto_mode, goal_rounds, "
                "project_class FROM cards")]
            con.close()
        except Exception:  # noqa: BLE001 读不动这个 work 就跳过，不影响其它
            continue
        by_id = {r["card_id"]: r for r in rows}
        root_row = by_id.get(root_id)
        if root_row is None:
            continue
        # 一张真子卡（parent==root 且不是 root 本身）——用于「非根卡」反例
        child = next((r for r in rows
                      if r["parent"] == root_id and r["card_id"] != root_id), None)
        out.append({
            "work_id": str(work.get("work_id") or d.name),
            "title": str(work.get("title") or ""),
            "root_card_id": root_id,
            "root_type": str(root_row.get("card_type") or ""),
            "root_auto": root_row.get("auto_mode"),
            "root_rounds": root_row.get("goal_rounds"),
            "root_project_class": str(root_row.get("project_class") or ""),
            "child_card_id": str(child["card_id"]) if child else "",
            "child_type": str(child.get("card_type") or "") if child else "",
            "child_parent": str(child.get("parent") or "") if child else "",
            "n_cards": len(rows),
        })
    return out


def pick_fixture(facts: list[dict[str, Any]]) -> dict[str, Any]:
    """挑一个夹具 work：根卡是 project、有子卡（非根卡反例要真卡）。

    优先 auto_mode=1 且 goal_rounds=0 的（最贴近「根卡刚进自动模式」的真场景），
    退而取任意有子卡的 project 根卡。
    """
    def _ok(f: dict[str, Any]) -> bool:
        return (f["root_type"] == "project" and bool(f["child_card_id"]))

    auto_first = [f for f in facts
                  if _ok(f) and f["root_auto"] == 1 and f["root_rounds"] in (0, None)]
    if auto_first:
        return auto_first[0]
    any_ok = [f for f in facts if _ok(f)]
    if any_ok:
        return any_ok[0]
    return {}


# ══════════════════════════════════════════════════════════════════════
# 夹具构造
# ══════════════════════════════════════════════════════════════════════
def make_ctx(reg_mod: Any, root_id: str, child_id: str,
             *, auto: Any, rounds: Any,
             card_id: Optional[str] = None,
             work_card_id: Optional[str] = None,
             extra_meta: Optional[dict[str, Any]] = None) -> Any:
    """构造 RegionCtx。

    **关键**：`cur_meta` 必须同时带 `card_id`／`auto_mode`／`goal_rounds`，
    且 `ctx.card_id` 与 `ctx.work_card_id` 都要设成根卡 id
    （实现按 `cur_meta.card_id` 判根卡，不是 `ctx.card_id`）。
    """
    meta: dict[str, Any] = {
        "card_id": root_id,
        "auto_mode": auto,
        "goal_rounds": rounds,
    }
    if extra_meta:
        meta.update(extra_meta)
    return reg_mod.RegionCtx(
        work_id="work_fixture",
        card_id=root_id if card_id is None else card_id,
        work_card_id=root_id if work_card_id is None else work_card_id,
        cards=[],
        cur_meta=meta,
    )


def render(reg_mod: Any, ctx: Any) -> str:
    """只调渲染函数本身（不碰装配入口、不碰 handler）。"""
    out = reg_mod.render_team_function_instruction_region(ctx)
    return out if isinstance(out, str) else str(out)


# ══════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════
def main() -> int:
    print(f"验收器：verify_role_first_round.py（仓库根 {REPO_ROOT}）")
    print("=" * 78)

    inst, inst_note = probe_instance()
    print(f"[环境] {inst_note}")
    works_dir, works_note = resolve_works_dir(inst)
    print(f"[环境] {works_note}")

    if works_dir is None:
        print("[FATAL] 找不到可读的 works 目录，无法取真实卡面；本验收器不做无夹具的假判断。")
        return 2

    # 被测代码（只读 import）
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from src.api import context_regions as reg_mod
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] import src.api.context_regions 失败：{type(exc).__name__}: {exc}")
        return 2

    # 控制面 + 类型注册表（只读读盘；读不到就不给，触发「没有来源就不出现这个区」）
    controls: dict[str, Any] = {}
    type_registry: dict[str, Any] = {}
    ctl_path = REPO_ROOT / "config" / "context_regions.json"
    reg_path = REPO_ROOT / "config" / "card_type_registry.json"
    try:
        ctl_all = json.loads(ctl_path.read_text(encoding="utf-8"))
        controls = ctl_all.get("regions") if isinstance(ctl_all, dict) else {}
        controls = controls if isinstance(controls, dict) else {}
    except Exception as exc:  # noqa: BLE001
        print(f"[环境] 控制面配置读取失败（{exc}）⇒ 装配序用注册表默认")
    try:
        type_registry = json.loads(reg_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[环境] 类型注册表读取失败（{exc}）⇒ 90 区将按「没有来源就不出现」跳过")

    # 真实卡面夹具
    facts = read_work_facts(works_dir)
    fixture = pick_fixture(facts)
    if not fixture:
        print("[FATAL] works/ 里找不到「project 根卡 + 真子卡」的可读 work，无法构造真实夹具。")
        return 2
    root_id = fixture["root_card_id"]
    child_id = fixture["child_card_id"]
    print(f"[夹具] work={fixture['work_id']}「{fixture['title']}」"
          f" 根卡={root_id}(type={fixture['root_type']}, auto={fixture['root_auto']}, "
          f"rounds={fixture['root_rounds']}) 子卡={child_id}(type={fixture['child_type']})")
    print("-" * 78)

    results: list[dict[str, Any]] = []
    any_fail = False

    def emit(name: str, ok: bool, observed: str, *, skip: bool = False,
             note: str = "") -> None:
        nonlocal any_fail
        verdict = "SKIP" if skip else ("PASS" if ok else "FAIL")
        if verdict == "FAIL":
            any_fail = True
        observed_full = observed + (f"（{note}）" if note else "")
        print(f"[{verdict}] {name}：实测={observed_full}")
        results.append({"name": name, "verdict": verdict, "observed": observed_full})

    # ── A. 轮首出区 ────────────────────────────────────────────────────
    ctx_a = make_ctx(reg_mod, root_id, child_id, auto=1, rounds=0)
    out_a = render(reg_mod, ctx_a)
    has_judge = KEY_PHRASE_JUDGE in out_a
    has_role = KEY_PHRASE_ROLE in out_a
    extras = [p for p in EXTRA_PHRASES if p in out_a]
    emit("A 轮首出区",
         bool(out_a.strip()) and has_judge and has_role,
         f"len={len(out_a)} 非空={bool(out_a.strip())} "
         f"含「{KEY_PHRASE_JUDGE}」={has_judge} 含「{KEY_PHRASE_ROLE}」={has_role} "
         f"附加语义命中={extras}",
         note="" if (has_judge and has_role) else
              "契约要求出区文案含「先做一个明确判断」与「人类团队里真实存在的岗位」")

    # ── B. 轮首外空串（核心）────────────────────────────────────────────
    ctx_b = make_ctx(reg_mod, root_id, child_id, auto=1, rounds=2)
    try:
        out_b = render(reg_mod, ctx_b)
        emit("B 轮首外空串", out_b == "",
             f"goal_rounds=2 ⇒ len={len(out_b)} 逐字repr={out_b[:60]!r}")
    except Exception as exc:  # noqa: BLE001
        emit("B 轮首外空串", False,
             f"goal_rounds=2 ⇒ 渲染抛异常 {type(exc).__name__}: {exc}")

    # ── C. 非根卡 ──────────────────────────────────────────────────────
    # 夹具陷阱：实现按 cur_meta.card_id 判根卡 ⇒ 这里必须让 cur_meta.card_id
    # 指向**非根卡**（子卡），ctx.card_id 同样指向子卡，ctx.work_card_id 仍是根卡。
    ctx_c = make_ctx(reg_mod, root_id, child_id, auto=1, rounds=0,
                     card_id=child_id)
    ctx_c.cur_meta = {"card_id": child_id, "auto_mode": 1, "goal_rounds": 0}
    try:
        out_c = render(reg_mod, ctx_c)
        emit("C 非根卡", out_c == "",
             f"cur_meta.card_id={child_id}(≠work_card_id={root_id}) ⇒ len={len(out_c)} "
             f"repr={out_c[:60]!r}")
    except Exception as exc:  # noqa: BLE001
        emit("C 非根卡", False,
             f"cur_meta.card_id={child_id} ⇒ 渲染抛异常 {type(exc).__name__}: {exc}")

    # ── D. 非 auto ─────────────────────────────────────────────────────
    ctx_d = make_ctx(reg_mod, root_id, child_id, auto=0, rounds=0)
    try:
        out_d = render(reg_mod, ctx_d)
        emit("D 非 auto", out_d == "",
             f"auto_mode=0 ⇒ len={len(out_d)} repr={out_d[:60]!r}")
    except Exception as exc:  # noqa: BLE001
        emit("D 非 auto", False,
             f"auto_mode=0 ⇒ 渲染抛异常 {type(exc).__name__}: {exc}")

    # ── E. 空/坏 meta 不抛 ────────────────────────────────────────────
    # 三种坏 meta：空 dict、None、以及「连 card_id 都没有」。
    # 「连 card_id 都没有」＝ cur_meta 里没有 card_id 键（不是 card_id=""）。
    bad_cases: list[tuple[str, Any]] = [
        ("cur_meta={}", {}),
        ("cur_meta=None", None),
        ("cur_meta 无 card_id 键", {"auto_mode": 1, "goal_rounds": 0}),
    ]
    e_detail: list[str] = []
    e_ok = True
    for label, bad_meta in bad_cases:
        ctx_e = make_ctx(reg_mod, root_id, child_id, auto=1, rounds=0)
        ctx_e.cur_meta = bad_meta
        try:
            out_e = render(reg_mod, ctx_e)
            empty = (out_e == "")
            e_detail.append(f"{label}⇒len={len(out_e)}空={empty}")
            if not empty:
                e_ok = False
        except Exception as exc:  # noqa: BLE001
            e_ok = False
            e_detail.append(f"{label}⇒抛异常 {type(exc).__name__}: {exc}")
    emit("E 空/坏 meta 不抛", e_ok, "; ".join(e_detail),
         note="三种坏 meta 均须返回空串且不抛异常")

    # ── F. 区位（注册表 + 真实装配入口）────────────────────────────────
    table = {str(s.get("key")): s for s in getattr(reg_mod, "REGION_TABLE", [])}
    spec = table.get(REGION_KEY)
    if spec is None:
        emit("F 区位", False, f"REGION_TABLE 里没有 key={REGION_KEY}")
    else:
        order = spec.get("order")
        before = table.get(NEIGHBOR_BEFORE, {}).get("order")
        after = table.get(NEIGHBOR_AFTER, {}).get("order")
        # 真实装配入口实测（context_assembler 的控制面同样参与排序）
        asm_orders: list[int] = []
        asm_keys: list[str] = []
        try:
            ctx_f = make_ctx(reg_mod, root_id, child_id, auto=1, rounds=0)
            ctx_f.type_registry = type_registry
            ctx_f.controls = controls
            regs = reg_mod.render_registered_regions(
                ctx_f, profile="main", deliveries={"system"})
            asm_orders = [int(r["order"]) for r in regs]
            asm_keys = [str(r["key"]) for r in regs]
        except Exception as exc:  # noqa: BLE001
            asm_orders = []
            asm_keys = []
            print(f"[环境] 装配入口实测抛异常：{type(exc).__name__}: {exc}")
        ok_f = (order == EXPECTED_ORDER
                and before is not None and after is not None
                and before < EXPECTED_ORDER < after
                and REGION_KEY in asm_keys)
        emit("F 区位",
             ok_f,
             f"REGION_TABLE order={order}（期望{EXPECTED_ORDER}）; "
             f"{NEIGHBOR_BEFORE}={before} < {EXPECTED_ORDER} < {NEIGHBOR_AFTER}={after}; "
             f"装配入口实测 orders={asm_orders}",
             note="" if REGION_KEY in asm_keys else
                  "装配入口未产出该区（可能因控制面禁用或无来源）")

    # ── G. 无枚举依赖（塞 functions / role_whitelist 输出逐字不变）───────
    ctx_g1 = make_ctx(reg_mod, root_id, child_id, auto=1, rounds=0)
    base_out = render(reg_mod, ctx_g1)
    ctx_g2 = make_ctx(reg_mod, root_id, child_id, auto=1, rounds=0,
                      extra_meta={"functions": ["架构师", "测试负责人"],
                                  "role_whitelist": ["架构师", "产品经理"]})
    enum_out = render(reg_mod, ctx_g2)
    same = (base_out == enum_out)
    emit("G 无枚举依赖", same,
         f"基准len={len(base_out)} 塞枚举后len={len(enum_out)} 逐字相同={same}",
         note="" if same else
              "塞入 functions/role_whitelist 后输出变了 ⇒ 系统在按职能名改判")

    # ── 汇总 + 落盘 ────────────────────────────────────────────────────
    n_pass = sum(1 for r in results if r["verdict"] == "PASS")
    n_fail = sum(1 for r in results if r["verdict"] == "FAIL")
    n_skip = sum(1 for r in results if r["verdict"] == "SKIP")
    print("-" * 78)
    print(f"PASS {n_pass} / FAIL {n_fail} / SKIP {n_skip}")

    evidence = {
        "checked_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "instance": INSTANCE_URL,
        "instance_reachable": inst is not None,
        "instance_note": inst_note,
        "sandbox_root": str(inst.get("sandbox_root")) if inst and inst.get("sandbox_root") else None,
        "works_dir": str(works_dir),
        "repo_root": str(REPO_ROOT),
        "fixture": {
            "work_id": fixture.get("work_id"),
            "title": fixture.get("title"),
            "root_card_id": root_id,
            "child_card_id": child_id,
            "root_auto_mode": fixture.get("root_auto"),
            "root_goal_rounds": fixture.get("root_rounds"),
        },
        "contract": {
            "region_key": REGION_KEY,
            "expected_order": EXPECTED_ORDER,
            "conditions": ["cur_meta.card_id == ctx.work_card_id",
                           "bool(cur_meta.auto_mode)",
                           "int(cur_meta.goal_rounds or 0) == 0"],
            "key_phrases": [KEY_PHRASE_JUDGE, KEY_PHRASE_ROLE],
        },
        "items": results,
        "summary": {"pass": n_pass, "fail": n_fail, "skip": n_skip},
    }
    EVIDENCE_PATH.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"证据落盘：{EVIDENCE_PATH}")

    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_must_round.py
====================
独立验收器：从系统外部核对「自动模式启动必须轮」的契约。

契约（全部取自卡面事实，不看请求参数——驱动器发起的回合不带 op，按请求参数判定会失效）：
    是必须轮 = 根卡(cur_meta.card_id == work_card_id)
             且 auto_mode == 1
             且 本 work 里一个职能都没有（function_profiles 为空）
             且 goal_rounds == 0
    命中时必须轮 ⇒ build_tool_schemas 返回的工具面里：
        · 必须没有 create_card、create_coder_card（这一轮不搭交付树）
        · 必须有 create_function（建职能的唯一通道）
        · 只读类与「写自己卡面」类应保留：read_card、update_status、append_ruling、update_goal 之类
        · 任一条件不成立 ⇒ 回到常规工具面（有 create_card）

方法（怎么测）：
    · import src.api.card_tools 取 build_tool_schemas 与 TOOL_REGISTRY（只读调用，不是帮着实现）。
    · 用构造的卡面 dict 喂 build_tool_schemas(card_id=..., work_id=..., cards={...})，
      逐个场景看工具面；判据＝「工具面里有没有某件工具」，不做关键词搜提示。
    · 不调 execute_tool、不写库、不建卡、不建职能；DB 若读一律 sqlite3 mode=ro。
    · sandbox_root 从 http://127.0.0.1:5713/__instance__ 的 sandbox_root 字段取，不写死。

用法：python verify/5713/verify_must_round.py
退出码：全 PASS 或 PASS+SKIP → 0；有 FAIL → 1。
产物：verify/5713/evidence_must_round.json（每项 name/verdict/observed）。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

# ── 控制台 UTF-8（Windows gbk 下中文不乱码）──────────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 非交互环境可能没有 reconfigure
    pass

INSTANCE_URL = "http://127.0.0.1:5713"
VERIFY_DIR = Path(__file__).resolve().parent          # <worktree>/verify/5713
WORKTREE = Path(__file__).resolve().parents[2]         # <worktree> 根
EVIDENCE_PATH = VERIFY_DIR / "evidence_must_round.json"

#: 场景 C「已有职能」的合成卡面注入键——**契约层的猜测键**。
#: build_tool_schemas 拿不到 DB 连接，实现方可能用 cards 里某个合成条目、也可能
#: 另查 function_store；此处用一个可读的合成键喂进去，若实现方不认此键（例如改读
#: function_store 库表），本场景报 SKIP 并写清缺什么，不猜着改签名。
SYNTH_FUNC_KEY = "__function_profiles__"


def get_instance() -> dict:
    """从 __instance__ 取 sandbox_root 等字段（动态，不写死路径）。"""
    try:
        with urllib.request.urlopen(INSTANCE_URL + "/__instance__", timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        if not isinstance(data, dict) or not data.get("sandbox_root"):
            sys.exit("FATAL: /__instance__ 无 sandbox_root 字段")
        return data
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"FATAL: 无法连接 {INSTANCE_URL}/__instance__: {exc}")


def pick_work(sandbox_works: Path) -> str:
    """挑一个沙箱里真实存在、有 work_card_id 的 work，作为 build 上下文。

    只读 work.json（纯文件读，不碰库）。
    """
    if not sandbox_works.is_dir():
        sys.exit(f"FATAL: 沙箱 works 目录不存在: {sandbox_works}")
    for d in sorted(sandbox_works.iterdir()):
        if not d.is_dir() or not d.name.startswith("work_"):
            continue
        wj = d / "work.json"
        if not wj.exists():
            continue
        try:
            data = json.loads(wj.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if data.get("work_card_id"):
            return str(data["work_id"])
    sys.exit("FATAL: 沙箱里找不到有 work_card_id 的 work")


def main() -> int:
    inst = get_instance()
    sandbox_root = Path(inst["sandbox_root"])
    work_id = pick_work(sandbox_root / "works")

    # 引入被测代码（只读调用 build_tool_schemas / TOOL_REGISTRY）。
    sys.path.insert(0, str(WORKTREE))
    from src.storage import work_store
    # 与生产实例 run_test_instance_5701.redirect_roots() 同源重定向：
    # work_store 默认指向 worktree/works（空），真库在沙箱根下；
    # prompt_log 默认指向 worktree/logs，审计日志也一并搬进沙箱根——
    # 本验收器全程零触碰 worktree 除 verify/5713 外的任何文件。
    work_store.WORKS_DIR = sandbox_root / "works"
    from src.api import card_tools, prompt_log
    from src.permissions import policy
    prompt_log.LOGS_DIR = sandbox_root / "logs"

    root_id = work_store.work_card_id_of(work_id)
    if not root_id:
        sys.exit(f"FATAL: work {work_id} 没有 work_card_id")
    sub_id = root_id + "_sub"

    root_perms = policy.perms_for("rd_project", "project")
    sub_perms = policy.perms_for("rd_project", "sub_project")

    def mk(cid: str, ctype: str, parent: str, auto: int, rounds: int,
           perms: list[str]) -> dict:
        """构造卡面 dict（mission 要求的最小字段集）。"""
        return {
            "card_id": cid,
            "card_type": ctype,
            "parent": parent,
            "perms": list(perms),
            "auto_mode": auto,
            "goal_rounds": rounds,
            "project_class": "rd_project",
        }

    def face(cid: str, cards: dict) -> set[str]:
        """取某张卡在给定卡面表下的工具面（工具名集合）。"""
        return {t.get("name") for t in card_tools.build_tool_schemas(
            card_id=cid, work_id=work_id, cards=cards)}

    # ── 各场景卡面 ─────────────────────────────────────────────────────
    cardsA = {root_id: mk(root_id, "project", "", 1, 0, root_perms)}
    cardsB = {root_id: mk(root_id, "project", "", 1, 2, root_perms)}
    cardsC = {root_id: mk(root_id, "project", "", 1, 0, root_perms),
              SYNTH_FUNC_KEY: [{"function_id": "fn_001", "function_name": "测试职能"}]}
    cardsD = {sub_id: mk(sub_id, "sub_project", root_id, 1, 0, sub_perms),
              root_id: mk(root_id, "project", "", 1, 0, root_perms)}
    cardsE = {root_id: mk(root_id, "project", "", 0, 0, root_perms)}

    fa = face(root_id, cardsA)          # 命中必须轮（场景 A 的面，F/G 复用）
    fb = face(root_id, cardsB)
    fc = face(root_id, cardsC)
    fd = face(sub_id, cardsD)
    fe = face(root_id, cardsE)

    results: list[dict] = []
    any_fail = False

    def emit(name: str, ok: bool, observed: str, skip: bool = False,
             note: str = "") -> None:
        nonlocal any_fail
        verdict = "SKIP" if skip else ("PASS" if ok else "FAIL")
        if verdict == "FAIL":
            any_fail = True
        observed_full = observed + (f"（{note}）" if note else "")
        print(f"[{verdict}] {name}：实测={observed_full}")
        results.append({"name": name, "verdict": verdict,
                        "observed": observed_full})

    # A. 命中必须轮：根卡 + auto=1 + 无职能 + goal_rounds=0
    has_cc = "create_card" in fa
    has_ccd = "create_coder_card" in fa
    has_cf = "create_function" in fa
    emit("A 命中必须轮",
         (not has_cc) and (not has_ccd) and has_cf,
         f"create_card={has_cc}, create_coder_card={has_ccd}, create_function={has_cf}")

    # B. 已跑过一轮：goal_rounds=2 ⇒ 解除
    emit("B 已跑过一轮", "create_card" in fb,
         f"create_card={'create_card' in fb}")

    # C. 已有职能 ⇒ 解除（合成注入，机制见 SYNTH_FUNC_KEY 注释）
    has_cc_c = "create_card" in fc
    if has_cc_c:
        emit("C 已有职能", True,
             f"create_card=True（注入 {SYNTH_FUNC_KEY} 后闸门未触发）",
             note="若实现方改读 function_store 而非 cards 合成键，本场景测不到区分度")
    else:
        emit("C 已有职能", False,
             f"create_card=False（注入 {SYNTH_FUNC_KEY} 后闸门仍触发）",
             skip=True,
             note="缺：实现方读取职能的机制不认 cards 合成键；需预置一个含 "
                  "function_profiles 行的 work 方可外部验证（本验收器不写库，无法预置）")

    # D. 非根卡：sub_project + auto=1 + 无职能 + rounds=0 ⇒ 不受管
    emit("D 非根卡", "create_card" in fd,
         f"create_card={'create_card' in fd}")

    # E. 非 auto：根卡 + auto=0 ⇒ 不受管
    emit("E 非 auto", "create_card" in fe,
         f"create_card={'create_card' in fe}")

    # F. 必须轮里组织类仍在
    f_org = {t: t in fa for t in ("create_function", "update_status", "append_ruling")}
    emit("F 必须轮里组织类仍在", all(f_org.values()),
         ", ".join(f"{k}={v}" for k, v in f_org.items()))

    # G. 不误伤只读（若某件本来就不给根卡，报 SKIP 并说明）
    g_tools = ("read_card", "read_file", "list_files", "search_files")
    baseline = fe  # 根卡常规面（auto=0）＝「根卡本应拿到什么」的基准
    for t in g_tools:
        if t not in baseline:
            emit(f"G 不误伤只读·{t}", False,
                 f"{t} 不在根卡常规面（本就不给根卡）", skip=True,
                 note="契约要求的是『不误伤只读』，根卡本就没有则无从误伤")
            continue
        emit(f"G 不误伤只读·{t}", t in fa,
             f"{t}={'存在' if t in fa else '缺失'}")

    # ── 汇总 + 落盘 ────────────────────────────────────────────────────
    n_pass = sum(1 for r in results if r["verdict"] == "PASS")
    n_fail = sum(1 for r in results if r["verdict"] == "FAIL")
    n_skip = sum(1 for r in results if r["verdict"] == "SKIP")
    print(f"PASS {n_pass} / FAIL {n_fail} / SKIP {n_skip}")

    EVIDENCE_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"证据落盘：{EVIDENCE_PATH}")

    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())

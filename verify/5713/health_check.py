# -*- coding: utf-8 -*-
"""5713 环境健康自检（一条命令回答「现在能不能放实验」）。

设计原则
--------
1. **只读**：不建卡、不跑回合、不写沙箱数据、不动 git 分支。除了可选的 --with-tests 会起
   pytest（各用例自带 tmp 沙箱），其它检查全是读。
2. **不碰 5700**：只对 5713 端口发一个 GET /__instance__；不读 5700 目录、不连 5700 端口。
3. **机器判词**：每条检查给 ok / warn / fail，最后给一个总判词与退出码（0=通，1=不通）。
   「warn」不进判词（例如工作区有别人的在途改动，是常态不是故障）。
4. **口径与本仓判定书一致**：认「相对声明基点的用例集合差」，不认绝对失败数
   （见 card_005 定论 D03；两侧计数会因 Windows 文件锁/收件箱时序竞争漂移）。

用法
----
    # 轻量自检（秒级，不跑测试）
    python verify/5713/health_check.py

    # 连验收网一起跑（180 项，约 1.5 分钟；需要 --baseline-worktree 才能按集合比）
    python verify/5713/health_check.py --with-tests --baseline-worktree ..\\_base11348

想让「落后 dev 多少」这项不判 fail，用 --expected-base 显式声明本环境的基点：
    python verify/5713/health_check.py --expected-base 11348ff
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime

SANDBOX_MARK = "5713"           # 沙箱根与本 worktree 必须都带这个记号
# 已知偶发用例（本仓实测：两侧都会偶发，单跑基本都绿）。判定时不直接判失败，
# 而是**自动单跑复核**——绿则记 warn（不是回归），红才判 fail。名单只放已实测过的，
# 且每次都要靠复核说话，不做"默认豁免"。
KNOWN_FLAKY = {
    "tests/test_card_msg.py::test_k3_live_a_to_b_invokes_receiver",
    "tests/test_coder_card.py::test_create_coder_card_builds_and_starts",
    "tests/test_coder_card.py::test_report_done_calls_owner_and_coder_stays_silent",
    "tests/test_board.py::test_apply_revision_dual_ledger_and_dirty_flush",
    "tests/test_role_cards.py::test_after_turn_syncs_by_signal_and_advances_cursor",
}

NET180 = [
    "tests/test_card_msg.py",
    "tests/test_dep_relations.py",
    "tests/test_perm_gateway_acceptance.py",
    "tests/test_cardperm_readopen.py",
    "tests/test_create_card_tool.py",
    "tests/test_auto_mode.py",
    "tests/test_coder_card.py",
]


def _sh(args: list[str], cwd: str) -> tuple[int, str]:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _port_listening(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _get_json(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Report:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, name: str, level: str, detail: str) -> None:
        self.rows.append({"check": name, "level": level, "detail": detail})

    def ok(self, n: str, d: str) -> None:
        self.add(n, "ok", d)

    def warn(self, n: str, d: str) -> None:
        self.add(n, "warn", d)

    def fail(self, n: str, d: str) -> None:
        self.add(n, "fail", d)

    @property
    def verdict(self) -> str:
        return "READY" if not any(r["level"] == "fail" for r in self.rows) else "NOT_READY"


def check_instance(rep: Report, base: str) -> dict:
    """实例验身：三个字段必须都指向 5713 与其沙箱。"""
    try:
        inst = _get_json(base.rstrip("/") + "/__instance__")
    except (urllib.error.URLError, OSError, ValueError) as e:
        rep.fail("instance_reachable", f"GET {base}/__instance__ 失败：{e}")
        return {}
    rep.ok("instance_reachable", f"GET /__instance__ ok（pid={inst.get('pid')}）")

    bd = str(inst.get("base_dir") or "").replace("/", "\\")
    sr = str(inst.get("sandbox_root") or "").replace("/", "\\")
    if inst.get("instance") == "test-instance":
        rep.ok("instance_is_test", "instance=test-instance（不是生产进程）")
    else:
        rep.fail("instance_is_test", f"instance={inst.get('instance')!r}，不是测试实例")

    if SANDBOX_MARK in bd and ".worktrees" in bd:
        rep.ok("base_dir_is_5713", f"base_dir={bd}")
    else:
        rep.fail("base_dir_is_5713", f"base_dir={bd}（不是 5713 worktree）")

    if SANDBOX_MARK in sr and "sandbox" in sr.lower():
        rep.ok("sandbox_root_is_5713", f"sandbox_root={sr}")
    else:
        rep.fail("sandbox_root_is_5713", f"sandbox_root={sr}（不是 5713 沙箱）")

    if inst.get("sandbox_enabled") is True:
        rep.ok("sandbox_enabled", "sandbox_enabled=true")
    else:
        rep.fail("sandbox_enabled", f"sandbox_enabled={inst.get('sandbox_enabled')!r}")
    return inst


def check_isolation(rep: Report, base: str) -> None:
    """隔离：5713 端口在听；5700 端口的存在只作事实记录，不参与判词。"""
    host, port = "127.0.0.1", 5713
    m = re.search(r"//([^:/]+):(\d+)", base)
    if m:
        host, port = m.group(1), int(m.group(2))
    if _port_listening(host, port):
        rep.ok("port_5713_listening", f"{host}:{port} 可连")
    else:
        rep.fail("port_5713_listening", f"{host}:{port} 连不上")
    rep.add("port_5700_note",
            "ok" if _port_listening("127.0.0.1", 5700, timeout=1.0) else "warn",
            "5700 在听（本检查只探测端口，未读写其目录/数据/进程）"
            if _port_listening("127.0.0.1", 5700, timeout=1.0) else "5700 未在听（可能是没起，不代表隔离破损）")


def check_git(rep: Report, repo: str, expected_base: str | None) -> dict:
    """分支、HEAD、声明基点是否为 HEAD 祖先、落后 dev 多少。"""
    out = {}
    rc, head = _sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
    if rc != 0:
        rep.fail("git_repo", f"{repo} 不是 git 仓库？{head.strip()[:200]}")
        return out
    out["branch"] = head.strip()
    rep.ok("git_branch", f"分支 {out['branch']}")

    rc, sha = _sh(["git", "rev-parse", "HEAD"], repo)
    out["head"] = sha.strip()
    rep.ok("git_head", f"HEAD {out['head'][:9]}")

    rc, dirty = _sh(["git", "status", "--porcelain"], repo)
    lines = [x for x in dirty.splitlines() if x.strip()]
    if lines:
        rep.warn("worktree_dirty",
                 f"工作区有 {len(lines)} 项未提交改动（可能是别人在途的活，不判失败）")
    else:
        rep.ok("worktree_dirty", "工作区干净")

    if expected_base:
        rc, _ = _sh(["git", "merge-base", "--is-ancestor", expected_base, "HEAD"], repo)
        if rc == 0:
            rep.ok("declared_base_contained", f"声明基点 {expected_base} 是 HEAD 的祖先")
        else:
            rep.fail("declared_base_contained", f"声明基点 {expected_base} **不在** HEAD 的祖先链上")
        out["expected_base"] = expected_base

    rc, behind = _sh(["git", "rev-list", "--count", "HEAD..dev"], repo)
    if rc == 0:
        n = int(behind.strip() or 0)
        out["behind_dev"] = n
        msg = f"比 dev 落后 {n} 个提交"
        if n == 0:
            rep.ok("align_dev", msg)
        else:
            rep.fail("align_dev", msg + "（基点跟进是未就绪项，见 D04；有预演时可接受但必须记账）")
    rc, ahead = _sh(["git", "rev-list", "--count", "dev..HEAD"], repo)
    if rc == 0:
        out["ahead_dev"] = int(ahead.strip() or 0)
    return out


def check_evidence(rep: Report, repo: str) -> dict:
    """试走证据在位、判 PASS、且用的是事实锚点判据（不是老的关键词探针）。"""
    out = {}
    ev_dir = os.path.join(repo, "verify", "5713")
    # 取最新的那份试走证据（历史留档命名 evidence_channel_walk*.json），避免拿旧跑次当现况
    cands = []
    if os.path.isdir(ev_dir):
        for fn in os.listdir(ev_dir):
            if fn.startswith("evidence_channel_walk") and fn.endswith(".json"):
                cands.append(os.path.join(ev_dir, fn))
    if not cands:
        rep.fail("evidence_channel_walk", f"缺 {ev_dir}\\evidence_channel_walk*.json")
        return out
    fp = max(cands, key=lambda x: os.path.getmtime(x))
    out["evidence_file"] = os.path.basename(fp)
    try:
        d = json.load(open(fp, encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001
        rep.fail("evidence_channel_walk", f"{fp} 读不动：{e}")
        return out
    det = d.get("verdict_detail") or {}
    out["verdict"] = d.get("verdict")
    out["finished_at"] = d.get("finished_at")
    if d.get("verdict") == "PASS":
        rep.ok("evidence_verdict",
               f"verdict=PASS（{os.path.basename(fp)} @ {d.get('finished_at')}）")
    else:
        rep.fail("evidence_verdict", f"verdict={d.get('verdict')!r}")
    if "B_self_answered_anchor" in det:
        rep.ok("evidence_probe_is_anchor", f"判据为事实锚点（anchor={det.get('B_self_answered_anchor')}）")
    else:
        rep.warn("evidence_probe_is_anchor",
                 "这份证据是老判据（ASCII 关键词）跑的；本轮之后的重跑会带 B_self_answered_anchor")
    return out


def run_net180(repo: str) -> list[str]:
    """跑 180 项，返回失败用例集合（去掉 - 之后的说明）。"""
    env = dict(os.environ)
    env["MOCK_LLM"] = "1"
    py = sys.executable
    p = subprocess.run([py, "-m", "pytest", *NET180, "-q", "--tb=no", "-rf",
                        "-p", "no:cacheprovider"],
                       cwd=repo, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    fails = set()
    for line in ((p.stdout or "") + (p.stderr or "")).splitlines():
        if line.startswith("FAILED "):
            fails.add(line.split(" - ")[0].strip())
    return sorted(fails)


def rerun_one(repo: str, nodeid: str) -> bool:
    """单跑一个用例复核（串行、干净进程）。用于把「已知偶发」和「真回归」分开。"""
    env = dict(os.environ)
    env["MOCK_LLM"] = "1"
    p = subprocess.run([sys.executable, "-m", "pytest", nodeid, "-q", "--tb=no",
                        "-p", "no:cacheprovider"],
                       cwd=repo, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    return p.returncode == 0


def check_tests(rep: Report, repo: str, baseline_wt: str | None) -> dict:
    out = {}
    cur = run_net180(repo)
    out["net180_failed"] = len(cur)
    rep.ok("net180_ran", f"验收网 180 项：失败 {len(cur)} 项（绝对数不作判据，见 D03）")
    if not baseline_wt:
        rep.warn("net180_set_diff", "未给 --baseline-worktree，无法按用例集合比 ⇒ 新增红不可判")
        return out
    base = run_net180(baseline_wt)
    out["baseline_failed"] = len(base)
    only_cur = sorted(set(cur) - set(base))
    only_base = sorted(set(base) - set(cur))
    out["only_5713_raw"] = only_cur
    out["only_baseline"] = only_base

    # 只在我们这侧失败的：已知偶发的先单跑复核，绿了就是噪声、红的才算新增回归。
    still_red: list[str] = []
    flaky_cleared: list[str] = []
    for item in only_cur:
        nid = item.replace("FAILED ", "", 1).strip()
        if nid in KNOWN_FLAKY and rerun_one(repo, nid):
            flaky_cleared.append(nid)
        else:
            still_red.append(item)
    out["only_5713"] = still_red
    out["flaky_cleared_by_rerun"] = flaky_cleared

    if not still_red:
        rep.ok("net180_set_diff",
               f"单侧新增红 0（基线 {len(base)} 项失败 / 5713 {len(cur)} 项；"
               f"集合差 {len(only_cur)} 项中 {len(flaky_cleared)} 项经单跑复核为偶发已排除）")
        if flaky_cleared:
            rep.warn("net180_flaky_cleared",
                     "整批里命中已知偶发、单跑复核全绿（不是回归）：" + "; ".join(flaky_cleared))
    else:
        rep.fail("net180_set_diff",
                 f"5713 单侧新增红 {len(still_red)} 项（含单跑复核仍红）：" + "; ".join(still_red[:5]))
    if only_base:
        rep.warn("net180_only_baseline", f"只在基线失败 {len(only_base)} 项（环境偶发，供参考）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5713")
    ap.add_argument("--repo", default=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    ap.add_argument("--expected-base", default=None,
                    help="本环境声明的 dev 基点（如 11348ff）；给了就校验它是否在 HEAD 祖先链上")
    ap.add_argument("--with-tests", action="store_true", help="连 180 项验收网一起跑（约 1.5 分钟）")
    ap.add_argument("--baseline-worktree", default=None, help="基线 worktree 路径，用于按集合比")
    ap.add_argument("--out", default=None, help="把结果写成 JSON")
    a = ap.parse_args()

    rep = Report()
    info: dict = {"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                  "base": a.base, "repo": a.repo}
    info["instance"] = check_instance(rep, a.base)
    check_isolation(rep, a.base)
    info["git"] = check_git(rep, a.repo, a.expected_base)
    info["evidence"] = check_evidence(rep, a.repo)
    if a.with_tests:
        info["tests"] = check_tests(rep, a.repo, a.baseline_worktree)

    info["checks"] = rep.rows
    info["verdict"] = rep.verdict

    print("=" * 72)
    print(f"5713 环境自检  {info['at']}")
    print("=" * 72)
    icon = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]"}
    for r in rep.rows:
        print(f"{icon.get(r['level'], '[?]   ')} {r['check']:<28} {r['detail']}")
    print("-" * 72)
    print(f"判词：{info['verdict']}"
          + ("　可以放实验" if info["verdict"] == "READY" else "　不可放实验（见上面 [FAIL] 项）"))
    print("=" * 72)

    if a.out:
        json.dump(info, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"JSON：{a.out}")
    return 0 if info["verdict"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
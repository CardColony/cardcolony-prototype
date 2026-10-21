# -*- coding: utf-8 -*-
"""5713 测试基线增量闸（裁决 A7／A8，质量域 M1）。

一次跑批只回答一个问题：**这次的红，比基线多了哪些？**
——「这次比上次红多了几条」不可用的根因是基线不稳（另有 M2 在修），
但即使基线稳，人眼比对整段 traceback 也不可靠：同一处失败换个机器换了行号
就会被读成「不一样」。故本工具按 **nodeid ＋ 失败签名** 比对。

用法
────
采基线（同一 commit 跑一到多轮，取**每轮都红**的为 known-red，天然的
「稳定红」定义；只出现一轮的红＝噪声，不进基线）：

    python verify/5713/baseline_diff.py --collect \\
        --report _r1.xml --report _r2.xml --report _r3.xml

跑闸（缺省自己跑 pytest 并现采现比；也可喂现成报告省一次 5 分钟）：

    python verify/5713/baseline_diff.py                    # 自己跑 pytest tests -q
    python verify/5713/baseline_diff.py --report _r4.xml   # 用现成 junit xml

退出码
──────
0 = 无「新增红」（有「已清红」或「剩余」都仍算 0）
1 = 有「新增红」
2 = 工具自身出错（报告解析不了／基线不存在等）

两条硬规矩
──────────
1. **HEAD 变了就喊基线过期**（裁决 A7：HEAD 一变即重生）。基线绑「采集 commit」
   ＋「命令作用域」，二者任一不符都要显式警告，不许静默比对。
2. **签名参与判定**：同一 nodeid 但在**不同位置／不同异常**上失败，算「新增红」
   （否则「修好一个旧红、顺手弄坏同一用例的另一处」会被看成没变化）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = Path(__file__).resolve().with_name("baseline_known_red.json")


# ─────────────────────────────── 解释器：钉到**本项目自己的** venv（card_022）
#
# 旧写法是 ``sys.executable``。在本机，「谁启动了本工具」的那个 python 并**不是**
# 本项目的 venv（实测：它是隔壁项目的 .venv，两份 venv 的 fastapi/uvicorn 版本
# 不同）。于是同一 HEAD、同一条命令，红名单会随**启动者**而变——基线就绑不住
# 命令作用域了（A7 的「基线绑采集 commit ＋ 命令作用域」要求落空）。
#
# 故改成：从**本文件位置**向上找含 ``.venv`` 的那层，取它下面的解释器。
#   · 不硬编码项目名（B-03「路径无单一注入点」正在修，再写死一个项目名
#     等于新增一处债）；推导只依赖 __file__ 与 .venv 这一约定；
#   · 上界由 git 定（主仓根），不会一路走到盘根去捡**别的项目**的 venv；
#   · 找不到 ⇒ 回退 sys.executable，但**显式告警**并写进基线的
#     command_scope.note，让「结果可能不可复现」留痕，而不是静默发生。
def _venv_python_under(base: Path) -> Path | None:
    """base 下若有 .venv，返回其解释器（Windows 布局优先，其次 POSIX）。"""
    for rel in (("Scripts", "python.exe"), ("bin", "python")):
        cand = base.joinpath(".venv", *rel)
        if cand.is_file():
            return cand
    return None


def _main_repo_root(cwd: Path) -> Path | None:
    """主仓根＝``git rev-parse --git-common-dir`` 的父目录；取不到返回 None。

    worktree（``.worktrees/<x>``）里也指回主仓：本项目自己的 .venv 挂在主仓根，
    worktree 下通常没有副本。没有 git 也不致命——那只是少了上界。
    """
    for extra in (["--path-format=absolute"], []):
        try:
            r = subprocess.run(["git", "rev-parse", *extra, "--git-common-dir"],
                               cwd=str(cwd), capture_output=True, text=True)
        except OSError:  # pragma: no cover  没有 git 二进制
            return None
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            p = Path(out)
            if not p.is_absolute():
                p = (cwd / p).resolve()
            return p.parent
    return None


def _bounded_ancestors(start: Path) -> list[Path]:
    """start 及其祖辈，**截到主仓根为止**（含主仓根）。

    截断的意义：宁可回退 sys.executable 并告警，也不能越界去捡父项目/盘根下
    别的项目的 .venv——悄悄用错环境正是本卡的病灶。
    """
    chain = [start, *start.parents]
    root = _main_repo_root(start)
    if root is None:
        return chain
    kept = [c for c in chain if c == root or root in c.parents]
    return kept or chain


def resolve_project_python(start: Path | None = None) -> tuple[Path, str, Path | None]:
    """→ (解释器绝对路径, 来源说明, 项目根或 None)。从本文件位置向上推导。"""
    here = (start or Path(__file__)).resolve()
    for base in _bounded_ancestors(here.parent):
        exe = _venv_python_under(base)
        if exe is not None:
            return (exe.resolve(),
                    "项目自己的 .venv：由本文件位置向上找到 %s" % base,
                    base)
    return (Path(sys.executable),
            "⚠ 未找到项目 venv，回退 sys.executable，结果可能不可复现",
            None)


PROJECT_PYTHON, PROJECT_PYTHON_SOURCE, PROJECT_VENV_ROOT = resolve_project_python()
DEFAULT_CMD = [str(PROJECT_PYTHON), "-m", "pytest", "tests", "-q", "--junitxml={xml}"]

# [5713 · M1] 打印必须不可能把工具搞崩：控制台若是 GBK（本机 cp936），失败签名里的
# 中文与「⇒」之类字符会抛 UnicodeEncodeError——那会让闸以**非零码**退出，而闸的
# 退出码语义是「有没有新增红」，一个编码异常就会被读成「有新红」。故降级为
# unreplaceable→'?'，绝不因打印而死。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except (AttributeError, OSError):  # pragma: no cover  老解释器/被重定向接管
    pass

# 采集时记录的环境变量：影响用例判定的开关都在这儿，照抄即可复现
ENV_KEYS = ("MOCK_LLM", "PYTHONHASHSEED", "PYTHONIOENCODING", "TZ", "PYTEST_ADDOPTS")


# ────────────────────────────────────────────── 报告解析

_FILE_LINE = re.compile(r'File "(?P<path>[^"]+?\.py)", line (?P<line>\d+)')
_SHORT_LOC = re.compile(r"^(?P<path>[^\s:]+\.py):(?P<line>\d+): \S+$", re.M)
_ARROW = re.compile(r"^>\s*(?P<code>.+)$", re.M)


def _nodeid(case: ET.Element) -> str:
    """junit 的 classname/name → pytest nodeid（tests/test_x.py::test_y）。

    坑：pytest 的 ``classname`` 是**点分模块路径**（``tests.test_x``），不是文件路径；
    而模块名本身常以 ``test_`` 开头，若拿「末段是否像类名」当判据，会把
    ``tests.test_x`` 误拆成 ``tests.py``。故判据换成**盘上到底有没有这个文件**：
    类内方法时才退回去掉末段的那条路径（本仓库当前没有类内用例，分支留着但不猜）。
    """
    cls, name = case.get("classname", ""), case.get("name", "")
    parts = [p for p in cls.split(".") if p]
    if not parts:
        return "?::%s" % name
    p_all = "/".join(parts) + ".py"
    p_cls = "/".join(parts[:-1]) + ".py" if len(parts) > 1 else ""
    if (p_cls and parts[-1].startswith(("Test", "test"))
            and (REPO / p_cls).exists() and not (REPO / p_all).exists()):
        path = p_cls
    else:
        path = p_all
    return "%s::%s" % (path, name)


def _signature(case: ET.Element, nodeid: str) -> dict:
    """从 junit 失败节点抽「能区分同一处失败与不同失败」的最小签名。

    取三样，都是**机制性**的、不随机器路径漂移的：
      • exc_type —— 异常类型（AssertionError / PermissionError / TimeoutError…）
      • loc      —— 首个指向用例文件的 location（`tests/xx.py:NN`，只留相对路径）
      • snippet  —— 失败点那一行源码（pytest 用 `> ` 标出），压缩空白后截断

    不取整段 traceback 原文：行号会随无关改动漂移，正文太长且含机器路径。
    """
    node = None
    stage = "call"
    for tag in ("failure", "error"):
        node = case.find(tag)
        if node is not None:
            stage = "call" if tag == "failure" else "error"
            break
    if node is None:
        return {"exc_type": "", "loc": "", "snippet": "", "stage": "none", "sig": ""}

    exc_type = (case.find("failure") if stage == "call" else case.find("error")).get("type", "") or ""
    text = node.text or ""
    message = case.find("failure" if stage == "call" else "error").get("message", "") or ""

    # loc：优先取指向用例文件的最后一帧（那才是断言落点）
    loc = ""
    hits = [(m.group("path"), int(m.group("line"))) for m in _FILE_LINE.finditer(text)]
    test_hits = [h for h in hits if "test" in Path(h[0]).name or Path(h[0]).name == "conftest.py"]
    if test_hits:
        p, ln = test_hits[-1]
        rel = p.replace("\\", "/")
        idx = rel.find("tests/")
        loc = "%s:%d" % (rel[idx:] if idx >= 0 else Path(p).name, ln)
    else:
        m2 = _SHORT_LOC.search(text)
        if m2:
            loc = "%s:%s" % (m2.group("path").replace("\\", "/"), m2.group("line"))

    # snippet：pytest 的 `>` 标注行
    snippet = ""
    m3 = _ARROW.search(text)
    if m3:
        snippet = re.sub(r"\s+", " ", m3.group("code")).strip()[:160]
    elif message:
        snippet = re.sub(r"\s+", " ", message).strip()[:160]

    if not exc_type:
        m4 = re.search(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception|Exit|Timeout|Interrupt))",
                       text, re.M)
        if m4:
            exc_type = m4.group(1).rsplit(".", 1)[-1]

    raw = "|".join((exc_type, loc, snippet))
    return {"exc_type": exc_type, "loc": loc, "snippet": snippet,
            "stage": stage, "sig": hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]}


def parse_report(path: Path) -> dict:
    """junit xml → {nodeid: 签名}。只收真有红（failure/error）的用例。"""
    root = ET.parse(str(path)).getroot()
    cases = root.iter("testcase")
    red, total = {}, 0
    for case in cases:
        total += 1
        if case.find("failure") is None and case.find("error") is None:
            continue
        if case.find("skipped") is not None:
            continue
        nid = _nodeid(case)
        red[nid] = _signature(case, nid)
    return {"red": red, "total": total}


# ────────────────────────────────────────────── 跑 pytest

def interpreter_source(exe) -> str:
    """这个解释器是谁——项目自己的 venv／回退的 sys.executable／--cmd 自定义。

    打印与基线都走这一个判据，避免「打印说 A、基线记 B」。
    """
    try:
        p = Path(str(exe)).resolve()
        if PROJECT_VENV_ROOT is not None and p == PROJECT_PYTHON.resolve():
            return PROJECT_PYTHON_SOURCE
        if p == Path(sys.executable).resolve():
            return "⚠ 非项目 venv（回退 sys.executable），结果可能不可复现"
    except OSError:  # pragma: no cover
        pass
    return "自定义解释器（--cmd 指定）"


def announce_interpreter() -> None:
    """每次开跑先把「用的哪个解释器」摊开：验收要能一眼核对指向哪个 venv。

    旧打印把解释器缩写成了 ``python``——两个项目各有一份 ``python``，那行等于
    没说。改成打**完整绝对路径**＋来源。
    """
    print("[gate] 实际使用的解释器：%s" % PROJECT_PYTHON)
    print("[gate] 解释器来源：%s" % PROJECT_PYTHON_SOURCE)
    if PROJECT_VENV_ROOT is None:
        print("!" * 78)
        print("警告：未找到项目 venv，改用 sys.executable，结果可能不可复现")
        print("      基线的 command_scope.note 会记下这次回退；正式重采前请先备好 <项目根>/.venv")
        print("!" * 78)
    sys.stdout.flush()


def run_pytest(xml_path: Path, cmd: list[str] | None, cwd: Path) -> tuple[int, str]:
    cmd = list(cmd or DEFAULT_CMD)
    args = [a.format(xml=str(xml_path)) for a in cmd]
    print("[gate] 跑批：%s" % " ".join(args))
    print("[gate] 实际使用的解释器：%s" % args[0])
    print("[gate] 解释器来源：%s" % interpreter_source(args[0]))
    print("[gate] cwd=%s" % cwd)
    sys.stdout.flush()
    p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode, (tail[-1] if tail else "")


def git_head(cwd: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(cwd),
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def env_snapshot() -> dict:
    return {k: os.environ.get(k) for k in ENV_KEYS}


def code_fingerprint_at(commit: str) -> dict:
    """被测代码面的指纹＝该 commit 下 ``src/`` 与 ``tests/`` 两棵树的 sha。

    为什么不能只绑 commit：**基线文件自己入册那次提交**就会把 HEAD 往前推一笔
    （改的是 verify/，零 src）。若只看 commit，闸会在每次入册后立刻喊「过期」，
    而重采又会再推一笔 HEAD——警告天天响，就等于没有警告。

    故补这一层：commit 变了但 src/tests 树一个字节没变 ⇒ 被测代码面未变，三表仍
    有效。A7 的「基线过期」提示照旧打出（保守），但同时把**判断依据**摊开，
    让人一眼看出是真过期还是只是 docs/verify 前进。
    """
    out = {}
    for path in ("src", "tests"):
        try:
            r = subprocess.run(["git", "rev-parse", "%s:%s" % (commit, path)],
                               cwd=str(REPO), capture_output=True, text=True)
            out[path] = r.stdout.strip() if r.returncode == 0 else ""
        except OSError:  # pragma: no cover  没有 git 二进制
            out[path] = ""
    return out


# ────────────────────────────────────────────── 采集 / 比对

def collect(args) -> int:
    reports = [Path(r) for r in args.report]
    if not reports:
        xml = Path("_baseline_run.xml")
        rc, tail = run_pytest(xml, args.cmd, REPO)
        print("[gate] pytest 退出码 %d  %s" % (rc, tail))
        reports = [xml]

    parsed = []
    for rp in reports:
        if not rp.exists():
            print("[gate] 报告不存在：%s" % rp)
            return 2
        parsed.append(parse_report(rp))
    rounds = len(parsed)

    # 只把**每轮都红**的收进 known-red：只红一轮的是噪声，进基线就等于把噪声
    # 洗成「已知」，从此再也不会被报出来。
    counts = {}
    for p in parsed:
        for nid in p["red"]:
            counts[nid] = counts.get(nid, 0) + 1
    stable = sorted(nid for nid, n in counts.items() if n == rounds)

    known = []
    for nid in stable:
        candidates = [p["red"][nid] for p in parsed if nid in p["red"]]
        # 签名取出现次数最多的那份（多数票），并记下是否各轮一致
        sigs = [c["sig"] for c in candidates]
        agree = len(set(sigs)) == 1
        pick = max(candidates, key=lambda c: sigs.count(c["sig"]))
        known.append({"nodeid": nid, "signature": pick, "signature_stable": agree,
                      "red_in_rounds": "%d/%d" % (len(candidates), rounds)})

    baseline = {
        "schema": 1,
        "what": "5713 测试基线（known-red）。比对口径：nodeid ＋ 失败签名。",
        "collected_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "commit": git_head(REPO),
        "branch": subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(REPO),
                                 capture_output=True, text=True).stdout.strip(),
        "code_fingerprint": code_fingerprint_at(git_head(REPO)),
        "command_scope": {
            "cmd": [a.format(xml="<tmp>.xml") for a in (args.cmd or DEFAULT_CMD)],
            "cwd": str(REPO),
            "interpreter": str((args.cmd or DEFAULT_CMD)[0]),
            "interpreter_source": interpreter_source((args.cmd or DEFAULT_CMD)[0]),
            "project_venv_root": (str(PROJECT_VENV_ROOT) if PROJECT_VENV_ROOT else ""),
            "note": ("在仓库根（本 worktree）执行；<tmp>.xml 为当次 junit 输出位置，不影响判定；"
                     + ("解释器＝项目自己的 .venv（%s）" % PROJECT_VENV_ROOT
                        if PROJECT_VENV_ROOT else
                        "警告：未找到项目 venv，改用 sys.executable，结果可能不可复现")),
        },
        "env": env_snapshot(),
        "platform": {"python": platform.python_version(), "os": platform.platform()},
        "rounds": rounds,
        "counts": {"collected_cases": parsed[0]["total"],
                   "red_per_round": [len(p["red"]) for p in parsed],
                   "known_red": len(known)},
        "known_red": known,
    }
    out = Path(args.baseline)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[gate] 基线已写：%s" % out)
    print("[gate] commit=%s rounds=%d 每轮红数=%s known-red=%d"
          % (baseline["commit"][:12], rounds, baseline["counts"]["red_per_round"], len(known)))
    noisy = sorted((nid for nid, n in counts.items() if n < rounds), key=str)
    if noisy:
        print("[gate] 未入基线（不是每轮都红 ⇒ 噪声）：%d 条" % len(noisy))
        for nid in noisy:
            print("        - %s（红 %d/%d）" % (nid, counts[nid], rounds))
    return 0


def diff(args) -> int:
    bp = Path(args.baseline)
    if not bp.exists():
        print("[gate] 基线不存在：%s（先 --collect 采一次）" % bp)
        return 2
    baseline = json.loads(bp.read_text(encoding="utf-8"))

    # ① 过期检查（裁决 A7）
    head = git_head(REPO)
    base_commit = baseline.get("commit", "")
    stale = head != base_commit
    if stale:
        print("=" * 78)
        print("⚠ 基线过期，请重采")
        print("  基线采集 commit = %s" % base_commit)
        print("  当前 HEAD      = %s" % head)
        print("  裁决 A7：基线绑「采集 commit ＋ 命令作用域」，HEAD 一变即重生。")
        print("  下面三表仅供参照，**不得**当作「本批新红」的验收结论。")
        cur_fp = code_fingerprint_at(head or "HEAD")
        base_fp = baseline.get("code_fingerprint") or {}
        if base_fp.get("src") and base_fp.get("tests"):
            same = (base_fp.get("src") == cur_fp.get("src")
                    and base_fp.get("tests") == cur_fp.get("tests"))
            print("  被测代码面指纹：基线 src=%s tests=%s ／ 当前 src=%s tests=%s"
                  % ((base_fp.get("src") or "-")[:10], (base_fp.get("tests") or "-")[:10],
                     (cur_fp.get("src") or "-")[:10], (cur_fp.get("tests") or "-")[:10]))
            if same:
                print("  ⇒ 指纹一致：src/ 与 tests/ 一字未变（只是 docs/verify 之类提交前进），"
                      "本次三表仍然有效。")
            else:
                print("  ⇒ 指纹已变：被测代码面真的变了，必须重采后再据以判「新增红」。")
        print("=" * 78)
    else:
        print("[gate] 基线未过期：HEAD = %s" % head[:12])

    # ② 本次红
    if args.report:
        cur_path = Path(args.report[0])
        if not cur_path.exists():
            print("[gate] 报告不存在：%s" % cur_path)
            return 2
        print("[gate] 用现成报告：%s" % cur_path)
    else:
        cur_path = Path("_gate_run.xml")
        rc, tail = run_pytest(cur_path, args.cmd, REPO)
        print("[gate] pytest 退出码 %d  %s" % (rc, tail))
        if not cur_path.exists():
            print("[gate] 没拿到报告，pytest 可能收集失败")
            return 2
    cur = parse_report(cur_path)

    # 作用域一致性提醒：本工具按 nodeid 比对，无法知道喂进来的报告跑了多大范围。
    # 若本次收集例数明显少于基线，则「已清红」多半不是修好了，而是**这次没跑到**。
    base_total = (baseline.get("counts") or {}).get("collected_cases") or 0
    if base_total and cur["total"] < base_total * 0.8:
        print("⚠ 作用域疑似不一致：本次只收集到 %d 例，基线是 %d 例。"
              % (cur["total"], base_total))
        print("  ⇒ 下面的「已清红」很可能只是本次没跑到那些用例，不是真修好；")
        print("    要判「已清红」请用与基线同作用域的报告（缺省即 pytest tests -q）。")

    base = {k["nodeid"]: k for k in baseline.get("known_red", [])}
    cur_red = cur["red"]

    # ③ 三表
    new_red, cleared, remain, moved = [], [], [], []
    for nid, sig in sorted(cur_red.items()):
        if nid not in base:
            new_red.append((nid, sig))
        elif base[nid]["signature"].get("sig") != sig["sig"]:
            # 同一用例换了个失败面 —— 不是「同一处失败」，按新增红处理
            moved.append((nid, base[nid]["signature"], sig))
        else:
            remain.append((nid, sig))
    for nid, info in sorted(base.items()):
        if nid not in cur_red:
            cleared.append((nid, info["signature"]))

    def _fmt(sig):
        return "%-18s %-34s %s" % (sig.get("exc_type", "?"), sig.get("loc", "?") or "-",
                                   (sig.get("snippet", "") or "")[:70])

    print()
    print("=" * 78)
    print("【新增红】= 本次红 - 基线 known-red ：%d 条  ← 非 0 即判本次改动有问题" % (len(new_red) + len(moved)))
    for nid, sig in new_red:
        print("  + %s\n      %s" % (nid, _fmt(sig)))
    for nid, old, sig in moved:
        print("  + %s   （同一用例、**换了失败面**，不算同一处失败）" % nid)
        print("      旧：%s" % _fmt(old))
        print("      新：%s" % _fmt(sig))
    print()
    print("【已清红】= 基线 known-red - 本次红 ：%d 条  ← 修好了" % len(cleared))
    for nid, sig in cleared:
        print("  - %s\n      %s" % (nid, _fmt(sig)))
    print()
    print("【剩余 known-red】                    ：%d 条  ← 老账，不影响判定" % len(remain))
    for nid, sig in remain:
        print("  = %s\n      %s" % (nid, _fmt(sig)))
    print("=" * 78)
    print("[gate] 汇总：本次红 %d 条（跑批共 %d 例）｜基线 known-red %d 条｜"
          "新增 %d｜已清 %d｜剩余 %d"
          % (len(cur_red), cur["total"], len(base), len(new_red) + len(moved),
             len(cleared), len(remain)))

    if stale:
        print("[gate] 结论：⚠ 基线过期，请重采 —— 本次退出码只反映「相对过期基线」的差异")
    verdict = 1 if (new_red or moved) else 0
    print("[gate] 退出码 %d（0=无新增红，1=有新增红）" % verdict)
    return verdict


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="5713 测试基线增量闸（nodeid＋失败签名）")
    ap.add_argument("--collect", action="store_true", help="采集/重采基线")
    ap.add_argument("--report", action="append", default=[],
                    help="现成 junit xml（可多个；--collect 时多个＝取每轮都红者为 known-red）")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE), help="基线文件路径")
    ap.add_argument("--cmd", nargs=argparse.REMAINDER,
                    help="自定义 pytest 命令（须含 {xml} 占位，例：--cmd python -m pytest tests -q --junitxml={xml}）")
    args = ap.parse_args(argv)
    announce_interpreter()
    return collect(args) if args.collect else diff(args)


if __name__ == "__main__":
    sys.exit(main())
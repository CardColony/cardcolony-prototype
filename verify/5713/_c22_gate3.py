# -*- coding: utf-8 -*-
"""card_022 · 改后闸三态复验。

用**真实的整套报告**（_c22_rounds/r3.xml，1215 例 / 76 红）派生三份报告，逐一喂闸：

  ① 干净       ＝ r3.xml 原样                     → 期望「新增红 0」且 exit 0
  ② 临时新红   ＝ r3.xml ＋ 一条自造失败用例       → 期望 exit 1 且把该 nodeid 报出来
  ③ 已清一条红 ＝ r3.xml 去掉一条基线 known-red   → 期望落「已清红 1」、**不**落「新增红」

刻意用**隔壁项目的 python**（本脚本的 sys.executable）去启动闸：这样「闸自己的
解释器钉法」也一起被验到——它必须自己打印出项目 venv 的路径。
"""
import copy
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "_c22_gate"
GATE = HERE / "baseline_diff.py"
BASELINE = HERE / "baseline_known_red.json"

NEW_CLASS = "tests.test_c22_synthetic"
NEW_NAME = "test_temp_new_red"


def _suite(root):
    return root.find("testsuite") if root.tag == "testsuites" else root


def _bump(suite, dfail=0, dtests=0):
    for k, d in (("failures", dfail), ("tests", dtests)):
        try:
            suite.set(k, str(int(suite.get(k, "0")) + d))
        except (TypeError, ValueError):
            pass


def _drop_nodeids(root, nodeids):
    """把 nodeid 对应的 testcase 摘掉（用于造「已清红」）。返回摘掉几个。"""
    suite = _suite(root)
    n = 0
    for case in list(suite.findall("testcase")):
        cls, name = case.get("classname", ""), case.get("name", "")
        nid = "%s::%s" % ("/".join(cls.split(".")) + ".py", name)
        if nid in nodeids:
            suite.remove(case)
            n += 1
    _bump(suite, dfail=-n)
    return n


def build() -> dict:
    OUT.mkdir(exist_ok=True)
    src = HERE / "_c22_rounds" / "r3.xml"
    if not src.exists():
        raise SystemExit("缺 %s —— 先跑 _c22_collect.py 采三轮" % src)

    # ① 干净
    clean = OUT / "gate1_clean.xml"
    shutil.copyfile(src, clean)

    # ② 加一条自造新红
    tree = ET.parse(src)
    root = tree.getroot()
    suite = _suite(root)
    case = ET.SubElement(suite, "testcase", {"classname": NEW_CLASS, "name": NEW_NAME,
                                             "time": "0.001"})
    fail = ET.SubElement(case, "failure", {
        "message": "AssertionError: card_022 造的临时新红（验闸用，不入基线）"})
    fail.text = (
        "def test_temp_new_red():\n"
        '        """card_022 三态复验用的假用例，用来验「新增红」会不会被报出来。"""\n'
        ">       assert 1 == 2\n"
        "E       assert 1 == 2\n"
        "\n"
        'tests/test_c22_synthetic.py:12: AssertionError\n'
        'File "tests/test_c22_synthetic.py", line 12, in test_temp_new_red\n'
        ">       assert 1 == 2\n"
    )
    _bump(suite, dfail=1, dtests=1)
    newred = OUT / "gate2_new_red.xml"
    tree.write(str(newred), encoding="utf-8", xml_declaration=True)

    # ③ 摘掉一条基线 known-red
    import json
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    victim = sorted(k["nodeid"] for k in base["known_red"])[0]
    tree = ET.parse(src)
    dropped = _drop_nodeids(tree.getroot(), {victim})
    cleared = OUT / "gate3_cleared.xml"
    tree.write(str(cleared), encoding="utf-8", xml_declaration=True)
    return {"clean": clean, "newred": newred, "cleared": cleared,
            "victim": victim, "dropped": dropped}


def main() -> int:
    paths = build()
    print("[3态] 派生报告：%s" % paths)
    plan = [
        ("① 干净（r3 原样）", paths["clean"]),
        ("② 临时新红（r3 ＋ 自造 %s::%s）" % (NEW_CLASS.replace(".", "/") + ".py", NEW_NAME),
         paths["newred"]),
        ("③ 已清一条（r3 摘掉 %s，摘到 %d 条）" % (paths["victim"], paths["dropped"]),
         paths["cleared"]),
    ]
    rc_all = []
    for i, (label, xml) in enumerate(plan, 1):
        log = OUT / ("gate%d.out.txt" % i)
        with log.open("wb") as f:
            p = subprocess.run([sys.executable, str(GATE), "--report", str(xml)],
                               stdout=f, stderr=subprocess.STDOUT, cwd=str(HERE.parents[1]))
        rc_all.append(p.returncode)
        print("[3态] %s  →  exit=%d  日志 %s" % (label, p.returncode, log.name))
    print("[3态] 期望：exit 0 / 1 / 0；实得：%s" % rc_all)
    print("[3态] 启动本脚本的解释器（刻意用隔壁项目的）：%s" % sys.executable)
    return 0 if rc_all == [0, 1, 0] else 1


if __name__ == "__main__":
    sys.exit(main())
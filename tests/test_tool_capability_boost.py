# -*- coding: utf-8 -*-
"""[基础工具找回 2026-10-17] 七件基础工具 ＋ search_files 正则的直调回归。

搬运自 .worktrees/toolfold 的 tests/test_tool_capability_boost.py（10-12 封存、
未合并），按 tc-8/tc-9 基线的注册表/开关断言适配：

1. 七件进 TOOL_REGISTRY（bash / python / glob_files / delete_file / move_file /
   rename_file / edit_file），且**全部挂 tools_file**（裁定 1：不再有占位未接线
   件；授 tools_file 的卡即得全套）；
2. 执行器：成功 / 非零退出码（不是错误）/ 超时（locked，服务不挂死）/
   超长输出截断 / utf-8 replace 解码兜底；
3. search_files：正则命中、正则非法、keyword+pattern 两空，三处 validation；
4. 文件管理三件：删成功、删目录拒、删不存在 not_found、移成功、移不存在
   not_found、改名成功、new_name 带分隔符拒；
5. edit_file：三态匹配、备份落点、越权 denied、版本号 tc-9；
6. 说明书＝工具面投影：prompts.BASE_PROMPT 含七行（含 edit_file 与 search_files
   的 pattern 参数）。

零网络、零真实数据（conftest autouse 沙箱把 BASE_DIR/ALLOWED_ROOTS 搬进
tmp_path）；子进程执行走系统自带解释器与 shell。
"""
from __future__ import annotations

import hashlib
import re
import sys
import time
from pathlib import Path

from src.api import card_tools, file_tools, prompts
from src.permissions import policy

SEVEN = frozenset({
    "bash", "python", "glob_files", "delete_file", "move_file", "rename_file",
    "edit_file",
})

# 有 tools_file 的卡（check_file_access 的正门），文件检索类 handler 直调用。
PERM_CARDS = {
    "c1": {"card_id": "c1", "card_type": "sub_project",
           "perms": ["read", "edit", "tools_file"]},
}


def _code(result: dict) -> str:
    return (result.get("error") or {}).get("code", "")


def _msg(result: dict) -> str:
    return (result.get("error") or {}).get("message", "")


def _schema(name: str) -> dict:
    """取件 schema（file_tools 四件是延迟求值 Callable，须现取）。"""
    return card_tools._schema_of(card_tools.TOOL_REGISTRY[name])


# ---------------------------------------------------------------- 注册与下发面

def test_seven_new_tools_registered():
    assert SEVEN <= set(card_tools.TOOL_REGISTRY)
    # tc-8 基线 32 件（22 字面量 ＋ 4 文件 ＋ 6 思维链）＋ 本批 7 件 ＝ 39 件
    # [角色机制 5710] 38→42（+4 件角色工具）
    # [换父 2026-10-20 · 5718] +1（reparent_card）⇒ 52。
    # ★ 本条在 5718 之前就已陈旧（断言 50、实际 51）——一并对齐，恢复指纹作用。
    # [职能实体化 2026-10-21] role 五件改名+新增 set_child_mission ⇒ 52 → 53
    assert len(card_tools.TOOL_REGISTRY) == 53


def test_seven_schemas_have_required_params():
    p = {n: _schema(n)["parameters"] for n in SEVEN}
    assert p["bash"]["required"] == ["command"]
    assert p["python"]["required"] == ["code"]
    assert p["glob_files"]["required"] == ["pattern"]
    assert p["delete_file"]["required"] == ["path"]
    assert p["move_file"]["required"] == ["src", "dst"]
    assert p["rename_file"]["required"] == ["path", "new_name"]
    assert p["edit_file"]["required"] == ["path", "old_string", "new_string"]
    # 既有的 search_files 只「加 pattern 键、放宽 required」，其余不动
    sp = _schema("search_files")["parameters"]
    assert sp["required"] == []
    assert set(sp["properties"]) == {"keyword", "pattern", "path", "limit"}


def test_seven_are_wired_to_tools_file():
    """裁定 1：七件全挂现有 SWITCH_TOOLS_FILE——授 tools_file 的卡即得全套。"""
    for name in SEVEN:
        assert card_tools.TOOL_SWITCH[name] == policy.SWITCH_TOOLS_FILE, name
        assert card_tools.TOOL_SWITCH[name] in policy.SWITCHES, name

    full = policy.perms_for("rd_project", "sub_project")
    card = {"card_id": "c1", "card_type": "sub_project", "perms": full}
    tools = [{"name": n} for n in card_tools.TOOL_REGISTRY]
    visible = {t["name"] for t in
               card_tools._filter_by_category(tools, "c1", "", {"c1": card})}
    assert SEVEN <= visible, visible - SEVEN

    # 无 tools_file 的卡（tool 类型）⇒ 七件一件不可见
    tool_card = {"card_id": "c2", "card_type": "tool",
                 "perms": policy.perms_for("rd_project", "tool")}
    hidden = {t["name"] for t in
              card_tools._filter_by_category(tools, "c2", "", {"c2": tool_card})}
    assert not (SEVEN & hidden), SEVEN & hidden


def test_manual_has_seven_lines_and_search_pattern():
    """说明书＝工具面投影：七行全在；search_files 行含 pattern 参数。"""
    manual = prompts.BASE_PROMPT
    for name in SEVEN:
        assert f"- {name}(" in manual, name
    # 契约必须写进说明书行（裁定 2）
    assert "恰好出现一次" in manual       # edit_file 恰好一次匹配契约
    assert "120" in manual               # bash/python 120s 超时契约
    assert "非零退出码" in manual         # 命令失败不是错误契约
    assert "- search_files(keyword, pattern, path, limit)：" in manual


# ---------------------------------------------------------------- 执行器

async def test_run_subprocess_ok_and_nonzero_exit():
    r = await card_tools._run_subprocess(argv=[sys.executable, "-c", "print('ok')"])
    assert r["exit_code"] == 0 and "ok" in r["stdout"] and r["truncated"] is False

    # 命令失败不是错误：非零退出码原样返回（无 error 信封）
    r = await card_tools._run_subprocess(argv=[sys.executable, "-c",
                                               "import sys; sys.exit(3)"])
    assert r["exit_code"] == 3 and "error" not in r


async def test_run_subprocess_timeout_is_locked_not_hang():
    # argv 模式：杀的正是解释器本体，管子随即关掉 ⇒ 几乎立刻收场
    t0 = time.monotonic()
    r = await card_tools._run_subprocess(
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=1.0,
    )
    elapsed = time.monotonic() - t0
    assert _code(r) == "locked" and "超时" in _msg(r), r
    assert elapsed < 15, elapsed


async def test_run_subprocess_shell_timeout_is_locked():
    """shell 模式：杀 cmd.exe 杀不到孙进程（它还攥着管道句柄）——

    靠 _EXEC_DRAIN_TIMEOUT_SECONDS 的有界清管兜住「服务不挂死」：
    总耗时＝超时档 ＋ 清管上限，而非等孙进程自己退完。
    """
    t0 = time.monotonic()
    r = await card_tools._run_subprocess(
        shell_cmd="ping -n 30 127.0.0.1 >nul", timeout=1.0)
    assert _code(r) == "locked" and "超时" in _msg(r), r
    elapsed = time.monotonic() - t0
    assert elapsed < 10, elapsed


async def test_run_subprocess_truncates_long_output():
    r = await card_tools._run_subprocess(
        argv=[sys.executable, "-c", "print('x' * 70000)"],
        timeout=60.0,
    )
    assert r["exit_code"] == 0 and r["truncated"] is True
    assert r["stdout"].startswith("x" * 100)
    # 字节数按实际落盘算（Windows 文本模式把 \n 写成 \r\n，故不钉死常数）
    m = re.search(r"（输出已截断，共 (\d+) 字节）", r["stdout"])
    assert m, r["stdout"][-80:]
    assert int(m.group(1)) >= 70001


async def test_run_subprocess_decodes_invalid_utf8_with_replace():
    r = await card_tools._run_subprocess(
        argv=[sys.executable, "-c",
              r"import sys; sys.stdout.buffer.write(b'\xffab\n')"],
        timeout=60.0,
    )
    assert r["exit_code"] == 0 and "\ufffd" in r["stdout"] and "ab" in r["stdout"]


# ---------------------------------------------------------------- bash / python

async def test_bash_echo_ok():
    r = await card_tools._h_bash("w", "c1", {"command": "echo ok"}, {})
    assert r["exit_code"] == 0 and "ok" in r["stdout"] and r["truncated"] is False
    assert "error" not in r


async def test_bash_empty_command_is_validation():
    r = await card_tools._h_bash("w", "c1", {"command": "   "}, {})
    assert _code(r) == "validation" and _msg(r) == "command 不能为空"


async def test_python_print_2():
    r = await card_tools._h_python("w", "c1", {"code": "print(1+1)"}, {})
    assert r["exit_code"] == 0 and "2" in r["stdout"]
    assert "error" not in r


async def test_python_empty_code_is_validation():
    r = await card_tools._h_python("w", "c1", {"code": "  \n "}, {})
    assert _code(r) == "validation" and _msg(r) == "code 不能为空"


async def test_python_temp_file_removed_after_run():
    r = await card_tools._h_python(
        "w", "c1", {"code": "import sys; print(sys.argv[0])"}, {})
    assert r["exit_code"] == 0, r
    tmp = r["stdout"].strip()
    assert tmp.endswith(".py")
    assert not Path(tmp).exists(), tmp


# ---------------------------------------------------------------- search_files 正则

async def test_search_files_pattern_matches_lines(sandbox):
    ws = sandbox.base / "trading-workspace"
    (ws / "m.py").write_text("import os\nimport sys\nfoo = 1\n", encoding="utf-8")

    r = await card_tools._h_search_files(
        "w", "c1", {"pattern": "^import", "path": "trading-workspace"}, PERM_CARDS)
    assert [h["line"] for h in r["hits"]] == [1, 2], r

    # pattern 非空时优先于 keyword（keyword 给个命不中的词也照样按正则命中）
    r = await card_tools._h_search_files(
        "w", "c1", {"pattern": "^import", "keyword": "不存在的词",
                    "path": "trading-workspace"}, PERM_CARDS)
    assert len(r["hits"]) == 2, r

    # keyword 老路（不区分大小写子串）逐字不变
    r = await card_tools._h_search_files(
        "w", "c1", {"keyword": "FOO", "path": "trading-workspace"}, PERM_CARDS)
    assert [h["line"] for h in r["hits"]] == [3], r


async def test_search_files_invalid_regex_is_validation(sandbox):
    r = await card_tools._h_search_files(
        "w", "c1", {"pattern": "((", "path": "trading-workspace"}, PERM_CARDS)
    assert _code(r) == "validation" and "正则非法" in _msg(r), r


async def test_search_files_needs_keyword_or_pattern(sandbox):
    r = await card_tools._h_search_files(
        "w", "c1", {"path": "trading-workspace"}, PERM_CARDS)
    assert _code(r) == "validation"
    assert _msg(r) == "keyword 与 pattern 至少给一个"


def test_search_files_pattern_direct_call(sandbox):
    """file_tools 直调同样吃 pattern（编译一次、忽略大小写、逐行 re.search）。"""
    ws = sandbox.base / "trading-workspace"
    (ws / "m.py").write_text("Import Os\nnothing here\n", encoding="utf-8")
    r = file_tools.search_files("", "trading-workspace", pattern="^import")
    assert [h["line"] for h in r["hits"]] == [1], r
    assert "error" not in r

    bad = file_tools.search_files("", "trading-workspace", pattern="((")
    assert "正则非法" in bad["error"]

    # 两空：兜底消息与改前逐字一致
    assert "不能为空" in file_tools.search_files("", "trading-workspace")["error"]


# ---------------------------------------------------------------- glob_files

async def test_glob_files_finds_py_under_scope(sandbox):
    ws = sandbox.base / "trading-workspace"
    (ws / "a.py").write_text("x", encoding="utf-8")
    (ws / "c.txt").write_text("x", encoding="utf-8")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.py").write_text("x", encoding="utf-8")

    r = await card_tools._h_glob_files("w", "c1", {"pattern": "**/*.py"}, {})
    assert r["truncated"] is False and r["pattern"] == "**/*.py"
    matches = r["matches"]
    assert matches and all(m.endswith(".py") for m in matches), matches
    assert any(m.endswith("a.py") for m in matches)
    assert any(m.endswith("b.py") for m in matches)
    assert not any(m.endswith("c.txt") for m in matches)
    # 相对路径：一律相对项目根（sandbox 的 BASE_DIR）
    assert all(not Path(m).is_absolute() for m in matches)


async def test_glob_files_scoped_path_and_prunes(sandbox):
    ws = sandbox.base / "trading-workspace"
    (ws / "keep.py").write_text("x", encoding="utf-8")
    venv = ws / ".venv"
    venv.mkdir()
    (venv / "skip.py").write_text("x", encoding="utf-8")
    (ws / ".env").write_text("k", encoding="utf-8")

    r = await card_tools._h_glob_files(
        "w", "c1", {"pattern": "**/*.py", "path": "trading-workspace"}, {})
    assert all("keep.py" in m for m in r["matches"]), r
    assert not any(".venv" in m for m in r["matches"]), r
    assert r["scope"] == str(Path("trading-workspace"))

    # 凭证文件不入列
    r = await card_tools._h_glob_files(
        "w", "c1", {"pattern": ".env", "path": "trading-workspace"}, {})
    assert r["matches"] == [], r


async def test_glob_files_caps_at_200(sandbox):
    ws = sandbox.base / "trading-workspace"
    for i in range(205):
        (ws / f"p{i:03d}.py").write_text("x", encoding="utf-8")
    r = await card_tools._h_glob_files(
        "w", "c1", {"pattern": "*.py", "path": "trading-workspace"}, {})
    assert len(r["matches"]) == 200 and r["truncated"] is True, r


async def test_glob_files_empty_pattern_and_out_of_scope(sandbox):
    r = await card_tools._h_glob_files("w", "c1", {"pattern": "  "}, {})
    assert _code(r) == "validation"

    outside = sandbox.tmp / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "x.py").write_text("x", encoding="utf-8")
    r = await card_tools._h_glob_files(
        "w", "c1", {"pattern": "*.py", "path": str(outside)}, {})
    assert _code(r) == "validation" and "越界" in _msg(r), r


# ---------------------------------------------------------------- 删 / 移 / 改名

async def test_delete_file_ok_and_guards(sandbox):
    ws = sandbox.base / "trading-workspace"
    f = ws / "doomed.txt"
    f.write_text("x", encoding="utf-8")

    r = await card_tools._h_delete_file(
        "w", "c1", {"path": "trading-workspace/doomed.txt"}, {})
    assert r["deleted"] is True and r["path"] == str(Path("trading-workspace") / "doomed.txt")
    assert not f.exists()

    # 目录不可删
    d = ws / "adir"
    d.mkdir()
    r = await card_tools._h_delete_file("w", "c1", {"path": "trading-workspace/adir"}, {})
    assert _code(r) == "validation" and _msg(r) == "这是目录不是文件"
    assert d.is_dir()

    # 不存在
    r = await card_tools._h_delete_file(
        "w", "c1", {"path": "trading-workspace/nope.txt"}, {})
    assert _code(r) == "not_found"

    # 越界
    outside = sandbox.tmp / "out.txt"
    outside.write_text("x", encoding="utf-8")
    r = await card_tools._h_delete_file("w", "c1", {"path": str(outside)}, {})
    assert _code(r) == "validation" and "越界" in _msg(r)
    assert outside.exists()

    # 空路径
    r = await card_tools._h_delete_file("w", "c1", {}, {})
    assert _code(r) == "validation"


async def test_move_file_ok_and_guards(sandbox):
    ws = sandbox.base / "trading-workspace"
    src = ws / "m1.txt"
    src.write_text("m", encoding="utf-8")
    sub = ws / "subdir"
    sub.mkdir()

    r = await card_tools._h_move_file(
        "w", "c1", {"src": "trading-workspace/m1.txt",
                    "dst": "trading-workspace/subdir/m1.txt"}, {})
    assert r["path"] == str(Path("trading-workspace") / "subdir" / "m1.txt"), r
    assert not src.exists() and (sub / "m1.txt").exists()

    # dst 是目录 ⇒ 落点＝目录/原文件名，回执报真实落点
    dst_dir = ws / "drop"
    dst_dir.mkdir()
    r = await card_tools._h_move_file(
        "w", "c1", {"src": "trading-workspace/subdir/m1.txt",
                    "dst": "trading-workspace/drop"}, {})
    assert r["path"] == str(Path("trading-workspace") / "drop" / "m1.txt"), r
    assert (dst_dir / "m1.txt").exists()

    # src 不存在
    r = await card_tools._h_move_file(
        "w", "c1", {"src": "trading-workspace/nope.txt",
                    "dst": "trading-workspace/drop/x.txt"}, {})
    assert _code(r) == "not_found"

    # dst 越界
    r = await card_tools._h_move_file(
        "w", "c1", {"src": "trading-workspace/drop/m1.txt",
                    "dst": str(sandbox.tmp / "stolen.txt")}, {})
    assert _code(r) == "validation" and "越界" in _msg(r)


async def test_rename_file_ok_and_guards(sandbox):
    ws = sandbox.base / "trading-workspace"
    f = ws / "old.txt"
    f.write_text("o", encoding="utf-8")

    r = await card_tools._h_rename_file(
        "w", "c1", {"path": "trading-workspace/old.txt", "new_name": "new.txt"}, {})
    assert r["path"] == str(Path("trading-workspace") / "new.txt"), r
    assert not f.exists() and (ws / "new.txt").exists()

    # new_name 带路径分隔符
    for bad in ("sub/new.txt", "sub\\new.txt"):
        r = await card_tools._h_rename_file(
            "w", "c1", {"path": "trading-workspace/new.txt", "new_name": bad}, {})
        assert _code(r) == "validation" and _msg(r) == "new_name 只能是文件名", bad

    # 空 new_name / 不存在的 path
    r = await card_tools._h_rename_file(
        "w", "c1", {"path": "trading-workspace/new.txt", "new_name": " "}, {})
    assert _code(r) == "validation"
    r = await card_tools._h_rename_file(
        "w", "c1", {"path": "trading-workspace/gone.txt", "new_name": "z.txt"}, {})
    assert _code(r) == "not_found"


# ============================================================
# [基础工具找回 2026-10-17] edit_file 精准编辑
#
# 编辑器只认「唯一一处」：匹配 0 次报错、>1 次带次数报错，两种情况**一个
# 字节都不写**（回读比对改前字节）；恰好 1 次才替换，且与「整串 replace 一次」
# 的期望逐字节相同（md5 双向对比，其余段落零改动）。
# ============================================================


async def test_edit_file_replaces_exactly_once(sandbox):
    ws = sandbox.base / "trading-workspace"
    f = ws / "cfg.txt"
    original = "alpha\nbeta\ngamma\n"
    # 夹具用**二进制**落盘：Windows 文本模式会把 \n 写成 \r\n，md5 对比就不成立了
    f.write_bytes(original.encode("utf-8"))

    r = await card_tools._h_edit_file(
        "w", "c1", {"path": "trading-workspace/cfg.txt",
                    "old_string": "beta", "new_string": "BETA"}, PERM_CARDS)
    assert r["ok"] is True and r["replacements"] == 1, r
    assert r["path"] == str(Path("trading-workspace") / "cfg.txt")
    assert "error" not in r

    expected = original.replace("beta", "BETA", 1).encode("utf-8")
    got = f.read_bytes()
    assert got == expected
    assert hashlib.md5(got).hexdigest() == hashlib.md5(expected).hexdigest()
    assert got.decode("utf-8") == "alpha\nBETA\ngamma\n"


async def test_edit_file_multiline_block_with_indent(sandbox):
    """old_string 含缩进与换行：整块换整块，相邻块零改动。"""
    ws = sandbox.base / "trading-workspace"
    f = ws / "mod.py"
    original = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
    f.write_bytes(original.encode("utf-8"))

    r = await card_tools._h_edit_file(
        "w", "c1", {"path": "trading-workspace/mod.py",
                    "old_string": "def g():\n    return 2\n",
                    "new_string": "def g():\n    return 3\n"}, PERM_CARDS)
    assert r["ok"] is True and r["replacements"] == 1, r
    assert f.read_bytes() == original.replace(
        "def g():\n    return 2\n", "def g():\n    return 3\n", 1).encode("utf-8")
    # 相邻的 f() 一个字符都没动
    assert "def f():\n    return 1\n" in f.read_text(encoding="utf-8")


async def test_edit_file_zero_matches_leaves_file_untouched(sandbox):
    ws = sandbox.base / "trading-workspace"
    f = ws / "cfg.txt"
    original = "alpha\nbeta\n"
    f.write_bytes(original.encode("utf-8"))
    before = f.read_bytes()

    r = await card_tools._h_edit_file(
        "w", "c1", {"path": "trading-workspace/cfg.txt",
                    "old_string": "不存在的串", "new_string": "x"}, PERM_CARDS)
    assert "error" in r and r["error"] == "old_string 未在文件中找到", r
    assert f.read_bytes() == before


async def test_edit_file_multiple_matches_reports_count(sandbox):
    ws = sandbox.base / "trading-workspace"
    f = ws / "cfg.txt"
    original = "foo\nbar\nfoo\n"
    f.write_bytes(original.encode("utf-8"))
    before = f.read_bytes()

    r = await card_tools._h_edit_file(
        "w", "c1", {"path": "trading-workspace/cfg.txt",
                    "old_string": "foo", "new_string": "FOO"}, PERM_CARDS)
    assert "error" in r and "出现 2 次" in r["error"], r
    assert f.read_bytes() == before


async def test_edit_file_backs_up_before_overwrite(sandbox):
    """覆盖前备份落到 _backups，backup 字段非空，备份内容＝改前原文。"""
    ws = sandbox.base / "trading-workspace"
    f = ws / "cfg.txt"
    original = "第一版\n"
    f.write_bytes(original.encode("utf-8"))

    r = await card_tools._h_edit_file(
        "w", "c1", {"path": "trading-workspace/cfg.txt",
                    "old_string": "第一版", "new_string": "第二版"}, PERM_CARDS)
    assert r["ok"] is True and r["backup"], r
    b = Path(r["backup"])
    assert b.exists() and b.name == "cfg.txt"
    assert b.read_bytes() == original.encode("utf-8")
    # 落点规则与 write_file 同一算法：_backups/{相对路径}/{时间戳}/{文件名}
    assert b.parent.parent == sandbox.base / "推演agent" / "_backups" / "trading-workspace"
    assert re.fullmatch(r"\d{8}_\d{6}", b.parent.name), b.parent.name
    # 目标文件已改到位
    assert f.read_text(encoding="utf-8") == "第二版\n"


async def test_edit_file_handler_denied_without_tools_file(sandbox):
    """无 tools_file 权限的卡调 edit_file ⇒ denied，且文件零改动。"""
    ws = sandbox.base / "trading-workspace"
    f = ws / "cfg.txt"
    f.write_bytes(b"a\n")
    no_file_perm = {"c1": {"card_id": "c1", "card_type": "sub_project",
                           "perms": ["read", "tools_web"]}}
    r = await card_tools._h_edit_file(
        "w", "c1", {"path": "trading-workspace/cfg.txt",
                    "old_string": "a", "new_string": "b"}, no_file_perm)
    assert r.get("ok") is False and _code(r) == "denied", r
    assert f.read_bytes() == b"a\n"


async def test_edit_file_missing_file_and_dir(sandbox):
    """不存在的文件 / 目录：报错收场，不备份不落盘（与 read_file 同口径）。"""
    ws = sandbox.base / "trading-workspace"
    r = await card_tools._h_edit_file(
        "w", "c1", {"path": "trading-workspace/gone.txt",
                    "old_string": "a", "new_string": "b"}, PERM_CARDS)
    assert "error" in r and "文件不存在" in r["error"], r

    d = ws / "adir"
    d.mkdir()
    r = await card_tools._h_edit_file(
        "w", "c1", {"path": "trading-workspace/adir",
                    "old_string": "a", "new_string": "b"}, PERM_CARDS)
    assert "error" in r and "这是目录不是文件" in r["error"], r


def test_edit_file_version_and_switch():
    """版本 tc-9 / t23-1；edit_file 与 write_file 同开关（tools_file）、写工具。"""
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-16"
    assert prompts.BASE_PROMPT_VERSION == "t24-1"
    assert card_tools.TOOL_SWITCH["edit_file"] == policy.SWITCH_TOOLS_FILE
    assert card_tools.TOOL_SWITCH["edit_file"] == \
        card_tools.TOOL_SWITCH["write_file"]
    assert card_tools.TOOL_REGISTRY["edit_file"].readonly is False
    assert "edit_file" not in card_tools.READONLY_TOOLS

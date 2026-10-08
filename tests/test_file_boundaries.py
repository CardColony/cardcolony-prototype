# -*- coding: utf-8 -*-
"""不变量三：文件工具的四道机械兜底（card_012 工作项 4）。

边界与兜底全在 `src/api/file_tools.py`；步骤三要收紧边界与凭证处理，
先把现有判据钉死——否则「收紧」可能变成「悄悄改判据」，而没有任何东西报警。
"""
from __future__ import annotations

from pathlib import Path

from src.api import file_tools


# ---------------------------------------------------------------- 路径校验

def test_resolve_blocks_escape(sandbox):
    base = sandbox.base

    p, err = file_tools._resolve("trading-workspace/../../escaped.txt")
    assert p is None and "越界" in err["error"]

    p, err = file_tools._resolve(str(sandbox.tmp / "outside.txt"))
    assert p is None and "越界" in err["error"]

    p, err = file_tools._resolve("")
    assert p is None and "不能为空" in err["error"]

    p, err = file_tools._resolve("trading-workspace/a.txt")
    assert err is None
    assert p == (base / "trading-workspace" / "a.txt").resolve()


# ---------------------------------------------------------------- 读写与备份

def test_write_creates_then_backs_up_on_overwrite(sandbox):
    r = file_tools.write_file("trading-workspace/note.md", "第一版")
    assert r["ok"] is True and r["status"] == "created" and r["backup"] == ""

    r2 = file_tools.write_file("trading-workspace/note.md", "第二版")
    assert r2["status"] == "overwritten"
    assert r2["backup"]
    backup = Path(r2["backup"])
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "第一版"
    assert str(backup).startswith(str(file_tools.BACKUP_DIR))

    assert file_tools.read_file("trading-workspace/note.md")["content"] == "第二版"


def test_read_file_line_ranges(sandbox):
    file_tools.write_file("trading-workspace/lines.txt", "l1\nl2\nl3\nl4")
    r = file_tools.read_file("trading-workspace/lines.txt", 2, 3)
    assert r["content"] == "l2\nl3"
    assert r["total_lines"] == 4 and r["read_lines"] == "2-3"

    # 类型错配要给机械事实，不要抛异常
    # [T-0001 §6-1] 两行关键词原先互换：read_file 遇目录报「不是文件」、
    # list_files 遇文件报「不是目录」（file_tools.py:169/128 原文）
    assert "不是文件" in file_tools.read_file("trading-workspace")["error"]
    assert "不是目录" in file_tools.list_files("trading-workspace/lines.txt")["error"]


# ---------------------------------------------------------------- 凭证类

def test_credentials_neither_readable_nor_writable(sandbox):
    """[T-0004 §3.1 同步] 函数名是 10-04 前旧行为的**历史锚点**，行为已反转：

    用户 2026-10-04 21:31 裁定「全盘可读可写」后，凭证拦截已撤
    （file_tools.py:170/242/292 三处注释在案；此前拦截系诸葛自行添加）。
    故开发态**凭证类文件可读可写**，read_file/write_file 不返回 error 键。

    产品态分层指针：产品态 .env/密钥类**永不可读**，系第2批 B-14 前置
    （RELEASE_BLOCKERS 裁决1）；**开发态 10-04 裁定不变**。本用例钉的是
    **开发态现状**——产品态分层落地后需再分层（届时应拆成
    「开发态可读可写 / 产品态拒读」两条独立用例）。
    """
    base = sandbox.base
    (base / "trading-workspace" / ".env").write_text("NEEDLE_IN_ENV", encoding="utf-8")
    (base / "trading-workspace" / "server.pem").write_text("k", encoding="utf-8")

    # 读：不拦截，直接返回正文（无 error 键）
    r = file_tools.read_file("trading-workspace/.env")
    assert "error" not in r and r["content"] == "NEEDLE_IN_ENV"
    r = file_tools.read_file("trading-workspace/server.pem")
    assert "error" not in r and r["content"] == "k"

    # 列目录：凭证文件在开发态可见（list_files 只剪枝目录，不隐藏文件）
    names = [e["name"] for e in file_tools.list_files("trading-workspace")["entries"]]
    assert ".env" in names and "server.pem" in names

    # 检索：凭证文件参与检索，内容可命中
    hits = file_tools.search_files("NEEDLE_IN_ENV", "trading-workspace")["hits"]
    assert hits == [{
        "file": str(Path("trading-workspace") / ".env"),
        "line": 1,
        "text": "NEEDLE_IN_ENV",
    }]
    # 以凭证文件为检索起点：不拦截，正常返回结构
    r = file_tools.search_files("x", "trading-workspace/.env")
    assert "error" not in r and r["hits"] == []

    # 写：不拦截；覆盖已存在文件照常备份
    for name in (".env", "server.pem"):
        w = file_tools.write_file(f"trading-workspace/{name}", "y")
        assert "error" not in w and w["ok"] is True and w["status"] == "overwritten"
    assert (base / "trading-workspace" / ".env").read_text(encoding="utf-8") == "y"


# ---------------------------------------------------------------- 依赖目录

def test_prune_dirs_are_invisible_and_unwritable(sandbox):
    """[T-0004 §3.1 同步] 函数名是 10-04 前旧行为的**历史锚点**，行为已半反转：

    「依赖/缓存目录**写入拦截**」随用户 2026-10-04 21:31 裁定撤除
    （file_tools.py:292 注释在案）；「列出/检索时**剪枝**」保留
    （避免 7.8GB 依赖目录淹没结果，file_tools.py:136/223 在案）。
    故现状＝ invisible（列表不进入、检索不命中）＋ **writable**（可写）。

    产品态分层指针：产品态边界＝数据根之内（RELEASE_BLOCKERS 裁决1）；
    **开发态 10-04 裁定不变**——本用例钉开发态现状，产品态分层落地后需再分层。
    """
    base = sandbox.base
    venv = base / "trading-workspace" / ".venv"
    venv.mkdir()
    (venv / "lib.py").write_text("PRUNE_NEEDLE", encoding="utf-8")

    # invisible（保留的机械兜底）：列表不进入、检索剪枝
    names = [e["name"] for e in file_tools.list_files("trading-workspace")["entries"]]
    assert ".venv" not in names
    assert file_tools.search_files("PRUNE_NEEDLE", "trading-workspace")["hits"] == []

    # writable（10-04 裁定后）：路径明确给出时读/写不再拦截
    r = file_tools.read_file("trading-workspace/.venv/lib.py")
    assert "error" not in r and r["content"] == "PRUNE_NEEDLE"
    w = file_tools.write_file("trading-workspace/.venv/x.py", "y")
    assert "error" not in w and w["ok"] is True and w["status"] == "created"
    assert (venv / "x.py").read_text(encoding="utf-8") == "y"


# ---------------------------------------------------------------- 二进制与体量

def test_binary_and_oversize_guards(sandbox):
    base = sandbox.base
    blob = base / "trading-workspace" / "blob.bin"
    blob.write_bytes(b"\x00\x01\xff\xfe")

    assert "二进制" in file_tools.read_file("trading-workspace/blob.bin")["error"]
    assert "二进制" in file_tools.write_file("trading-workspace/blob.bin", "文本")["error"]

    oversize = "x" * (file_tools.MAX_WRITE_BYTES + 1)
    r = file_tools.write_file("trading-workspace/big.txt", oversize)
    assert "过大" in r["error"]
    assert not (base / "trading-workspace" / "big.txt").exists()


# ---------------------------------------------------------------- 检索

def test_search_files_reports_file_and_line(sandbox):
    file_tools.write_file("trading-workspace/app.py", "a = 1\nTARGET = 2\n")
    r = file_tools.search_files("target", "trading-workspace")
    assert r["truncated"] is False
    assert r["hits"] == [{"file": str(Path("trading-workspace") / "app.py"),
                          "line": 2, "text": "TARGET = 2"}]
    assert "不能为空" in file_tools.search_files("", "trading-workspace")["error"]


# ---------------------------------------------------------------- schema

def test_tool_schema_surface_is_stable():
    """工具清单是模型的可见接口面，条数与名称属回归项。"""
    names = [s["name"] for s in file_tools.build_file_tool_schemas()]
    assert names == ["list_files", "read_file", "search_files", "write_file"]
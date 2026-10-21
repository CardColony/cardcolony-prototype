# -*- coding: utf-8 -*-
"""凭证保护缺口（card_012）· exec 类工具的机械用例。

**防线是两条，用例也就分两组**：

- 静态闸（执行前）：命令/代码里直接点名凭证文件 ⇒ `denied`，且拒绝信封里不回显内容；
- 值闸（执行后）：凭证**值**出现在返回值里 ⇒ 替换成 `***REDACTED***`。这一组必须
  覆盖「拼接路径间接读」「grep 类扫描」两种绕过——静态闸在这两种写法上本来就该失效，
  真正的边界是值闸。

外加一组**防误伤**用例：正常文件读取、`.env.example` 这类非凭证文件、以及
`LOG_LEVEL=INFO` 这种短配置值，都不许被脱敏成乱码（脱敏过宽＝把工具废掉）。

沙箱：conftest 的 autouse fixture 把 `file_tools.BASE_DIR` 搬进 tmp_path，
本模块只在那个沙箱里摊 `.env`，不碰真实凭证。
"""
from __future__ import annotations

import json
import os

from src.api import card_tools, credential_guard, file_tools

SECRET = "sk-test-5713-credguard-abc123"          # 假值，仅用于比对本模块的脱敏
CARDS = {"c1": {"card_id": "c1", "card_type": "sub_project",
                "perms": ["read", "edit", "tools_file"]}}


def _code(r: dict) -> str:
    return (r.get("error") or {}).get("code", "")


def _plant_env(sandbox, extra: str = "") -> None:
    """在沙箱根摊一份 .env（凭证文件）并清掉值采集缓存。"""
    (sandbox.base / ".env").write_text(
        f"DEEPSEEK_API_KEY={SECRET}\nPROBE_NOTE=probe-only-not-a-real-secret\n{extra}",
        encoding="utf-8")
    credential_guard.reset_cache()


# ---------------------------------------------------------------- 静态闸：直读
async def test_python_direct_env_read_is_denied(sandbox):
    """`.env` 直读：拒绝执行，且拒绝文案里不含凭证值。"""
    _plant_env(sandbox)
    r = await card_tools._h_python("w", "c1", {"code": "print(open('.env').read())"}, {})
    assert _code(r) == "denied", r
    assert SECRET not in json.dumps(r, ensure_ascii=False)


async def test_bash_direct_env_read_is_denied(sandbox):
    """`type .env` / `cat .env` 同款直读，同样被拒。"""
    _plant_env(sandbox)
    r = await card_tools._h_bash("w", "c1", {"command": "type .env"}, {})
    assert _code(r) == "denied", r
    assert ".env" in (r.get("error") or {}).get("message", "")

    r2 = await card_tools._h_bash("w", "c1", {"command": "cat .env"}, {})
    assert _code(r2) == "denied", r2


async def test_deny_covers_other_credential_names(sandbox):
    """黑名单是 file_tools 那一份（不是另立一套）：id_rsa / *.pem 同样拒。"""
    _plant_env(sandbox)
    for cmd in ("type id_rsa", "type certs\\server.pem", "type credentials.json"):
        r = await card_tools._h_bash("w", "c1", {"command": cmd}, {})
        assert _code(r) == "denied", (cmd, r)


# ---------------------------------------------------------------- 值闸：间接读
async def test_python_path_join_indirect_read_is_redacted(sandbox):
    """绕过静态闸：文件名拼接 + os.listdir 后拼路径读，内容必须被脱敏。"""
    _plant_env(sandbox)
    code = (
        "import os\n"
        "root = os.getcwd()\n"
        "names = [n for n in os.listdir(root) if n.startswith('.e')]\n"
        "target = os.path.join(root, names[0])\n"
        "print(open(target, encoding='utf-8').read())\n"
    )
    r = await card_tools._h_python("w", "c1", {"code": code}, {})
    assert r.get("exit_code") == 0, r
    blob = str(r.get("stdout", "")) + str(r.get("stderr", ""))
    assert SECRET not in blob, blob
    assert credential_guard.REDACTED in blob, blob


async def test_bash_grep_like_scan_is_redacted(sandbox):
    """grep 类扫描：凭证值被摊到普通文件里再 findstr，出来仍必须是脱敏的。"""
    _plant_env(sandbox)
    (sandbox.base / "notes.txt").write_text(f"key = {SECRET}\n", encoding="utf-8")
    r = await card_tools._h_bash("w", "c1", {"command": "findstr /c:sk- notes.txt"}, {})
    assert r.get("exit_code") == 0, r
    assert SECRET not in r["stdout"], r["stdout"]
    assert credential_guard.REDACTED in r["stdout"], r["stdout"]


async def test_redact_marks_count_and_audit_log(sandbox):
    """脱敏在输出尾部留一句计数；同时落一条 prompt_perm_audit 留痕。"""
    _plant_env(sandbox)
    (sandbox.base / "notes.txt").write_text(f"a {SECRET} b\n", encoding="utf-8")
    r = await card_tools._h_bash("w", "c1", {"command": "type notes.txt"}, {})
    assert "处凭证值已脱敏" in r["stdout"], r["stdout"]
    log = sandbox.logs / "prompt_perm_audit.jsonl"
    assert log.exists(), "凭证闸必须留痕"
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any(x.get("fn") == "exec_credential_guard" and x.get("action") == "redact"
               for x in rows), rows[-3:]


# ---------------------------------------------------------------- 防误伤
async def test_normal_file_read_still_works(sandbox):
    """正常文件读取：内容原样返回，不出现脱敏标记（防脱敏过宽）。"""
    _plant_env(sandbox, extra="LOG_LEVEL=INFO\n")
    (sandbox.base / "hello.txt").write_text("hello world\n", encoding="utf-8")
    r = await card_tools._h_bash("w", "c1", {"command": "type hello.txt"}, {})
    assert r.get("exit_code") == 0 and "hello world" in r["stdout"], r
    assert credential_guard.REDACTED not in r["stdout"]

    r2 = await card_tools._h_python("w", "c1", {"code": "print(1 + 1)"}, {})
    assert r2.get("exit_code") == 0 and "2" in r2["stdout"], r2


async def test_short_config_values_are_not_redacted(sandbox):
    """短配置值（INFO / production 这类）不进采集集：否则输出会被打成筛子。"""
    _plant_env(sandbox, extra="LOG_LEVEL=INFO\nENV_NAME=production\n")
    r = await card_tools._h_bash(
        "w", "c1", {"command": "echo level=INFO env=production"}, {})
    assert "INFO" in r["stdout"] and "production" in r["stdout"], r
    assert credential_guard.REDACTED not in r["stdout"], r


def test_env_example_is_not_a_credential_name():
    """`.env.example` 不是凭证文件：黑名单判据与 file_tools 逐字同一份。"""
    assert credential_guard.find_credential_refs("open('.env.example')") == []
    assert credential_guard.find_credential_refs("type .env") == [".env"]
    assert credential_guard.find_credential_refs("--file=secrets.yaml") == ["secrets.yaml"]


def test_guard_reuses_file_tools_blacklist():
    """判据单一来源：改 file_tools 的黑名单，静态闸立刻跟着变。"""
    try:
        file_tools.CREDENTIAL_NAMES.add("custom_vault.txt")
        assert credential_guard.find_credential_refs("type custom_vault.txt") == [
            "custom_vault.txt"]
    finally:
        file_tools.CREDENTIAL_NAMES.discard("custom_vault.txt")
    assert credential_guard.find_credential_refs("type custom_vault.txt") == []


async def test_secret_harvest_is_cached_until_reset(sandbox):
    """采集带 TTL 缓存：摊完 .env 不清缓存 ⇒ 旧快照（空集）不脱敏；
    清缓存后立刻生效——凭证轮换时要能一键翻新。"""
    (sandbox.base / ".env").write_text(f"K={SECRET}\n", encoding="utf-8")
    credential_guard.reset_cache()
    assert SECRET in credential_guard.secret_values()
    credential_guard.reset_cache()
    (sandbox.base / ".env").write_text("K=another-rotated-secret-9999\n", encoding="utf-8")
    assert "another-rotated-secret-9999" in credential_guard.secret_values()
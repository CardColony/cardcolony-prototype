# -*- coding: utf-8 -*-
"""模型设置·第二步 —— env_store 单测（任务书步骤 3）。

覆盖：写 / 读 / 打码 / 保留其它行与注释 / os.environ 刷新 / 非法键名拒绝。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import env_store  # noqa: E402

SAMPLE = """# 顶上一段说明，保存必须原样留着
OPENAI_API_KEY=sk-existing-abcdefgh

# 模型密钥
NOUS_API_KEY='sk-nous-1234567890'
DEEPSEEK_API_KEY="sk-deep-abcdef"
"""


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    """每个用例一份干净的 .env 副本；备份目录也重定向到 tmp。"""
    p = tmp_path / ".env"
    p.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(env_store, "BACKUP_DIR", tmp_path / "_backups")
    return p


# ---------- 读 ----------


def test_read_keys_parses_all(env_file):
    keys = env_store.read_keys(env_file)
    assert keys["NOUS_API_KEY"] == "sk-nous-1234567890"
    assert keys["DEEPSEEK_API_KEY"] == "sk-deep-abcdef"
    assert keys["OPENAI_API_KEY"] == "sk-existing-abcdefgh"


def test_read_keys_missing_file_returns_empty(tmp_path):
    assert env_store.read_keys(tmp_path / "nope.env") == {}


def test_read_keys_skips_comments(env_file):
    keys = env_store.read_keys(env_file)
    assert not any(k.startswith("#") for k in keys)
    assert "顶上一段说明" not in keys


# ---------- 打码 ----------


def test_mask_keeps_last_four():
    assert env_store.mask("sk-abcdefghijkl") == "····ijkl"


def test_mask_short_value_hides_everything():
    assert env_store.mask("ab") == "····"
    assert env_store.mask("") == ""


def test_mask_never_returns_full_secret():
    v = "sk-super-secret-value-1234"
    m = env_store.mask(v)
    assert "super" not in m
    assert v not in m
    assert m.endswith("1234")


# ---------- 状态 ----------


def test_list_key_status_reports_configured_and_tail(env_file):
    st = env_store.list_key_status(env_file, ["NOUS_API_KEY", "MISSING_KEY"])
    assert st["NOUS_API_KEY"]["configured"] is True
    assert st["NOUS_API_KEY"]["tail"] == "7890"
    assert st["MISSING_KEY"] == {"configured": False, "tail": ""}


def test_list_key_status_leaks_no_plaintext(env_file):
    st = env_store.list_key_status(env_file, ["NOUS_API_KEY", "DEEPSEEK_API_KEY"])
    blob = repr(st)
    assert "sk-nous-1234567890" not in blob
    assert "sk-deep-abcdef" not in blob


# ---------- 写 ----------


def test_set_key_updates_existing_line(env_file):
    env_store.set_key(env_file, "NOUS_API_KEY", "sk-new-value-9999")
    assert env_store.read_keys(env_file)["NOUS_API_KEY"] == "sk-new-value-9999"


def test_set_key_appends_when_absent(env_file):
    env_store.set_key(env_file, "BRAND_NEW_KEY", "sk-brand-new")
    assert env_store.read_keys(env_file)["BRAND_NEW_KEY"] == "sk-brand-new"


def test_set_key_preserves_other_lines_and_comments(env_file):
    env_store.set_key(env_file, "NOUS_API_KEY", "sk-new-value-9999")
    text = env_file.read_text(encoding="utf-8")
    assert "# 顶上一段说明，保存必须原样留着" in text
    assert "# 模型密钥" in text
    assert "OPENAI_API_KEY=sk-existing-abcdefgh" in text
    assert "DEEPSEEK_API_KEY=" in text


def test_set_key_does_not_touch_sibling_value(env_file):
    env_store.set_key(env_file, "NOUS_API_KEY", "sk-new-value-9999")
    assert env_store.read_keys(env_file)["OPENAI_API_KEY"] == "sk-existing-abcdefgh"


def test_set_key_refreshes_os_environ(env_file, monkeypatch):
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    env_store.set_key(env_file, "NOUS_API_KEY", "sk-runtime-4242")
    assert os.environ["NOUS_API_KEY"] == "sk-runtime-4242"


def test_set_key_refreshes_os_environ_for_new_key(env_file, monkeypatch):
    monkeypatch.delenv("FRESH_KEY", raising=False)
    env_store.set_key(env_file, "FRESH_KEY", "sk-fresh-7777")
    assert os.environ["FRESH_KEY"] == "sk-fresh-7777"


def test_set_key_makes_backup(env_file):
    env_store.set_key(env_file, "NOUS_API_KEY", "sk-new-value-9999")
    backups = list((env_file.parent / "_backups").glob(".env_*"))
    assert backups, "写入前必须留备份"
    assert "sk-nous-1234567890" in backups[0].read_text(encoding="utf-8")


def test_set_key_quotes_value_with_spaces(env_file):
    env_store.set_key(env_file, "SPACED_KEY", "a b c")
    assert env_store.read_keys(env_file)["SPACED_KEY"] == "a b c"


def test_set_key_creates_file_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(env_store, "BACKUP_DIR", tmp_path / "_backups")
    p = tmp_path / "fresh.env"
    env_store.set_key(p, "NEW_ONLY", "sk-only")
    assert env_store.read_keys(p)["NEW_ONLY"] == "sk-only"


# ---------- 非法键名必须拒绝（防注入） ----------


@pytest.mark.parametrize("bad", ["", "1BAD", "BAD-NAME", "BAD NAME", "BAD=NAME", "A;B", "NAME\n"])
def test_set_key_rejects_illegal_name(env_file, bad):
    with pytest.raises(ValueError):
        env_store.set_key(env_file, bad, "sk-x")
    # 文件必须一字未动
    assert env_file.read_text(encoding="utf-8") == SAMPLE


def test_remove_key_comments_out(env_file):
    assert env_store.remove_key(env_file, "OPENAI_API_KEY") is True
    text = env_file.read_text(encoding="utf-8")
    assert "[删除于设置界面]" in text
    assert "OPENAI_API_KEY" not in env_store.read_keys(env_file)
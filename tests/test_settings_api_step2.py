# -*- coding: utf-8 -*-
"""模型设置·第二步 —— settings_api 接口自测（任务书步骤 5）。

覆盖：读全量 / 六类 400 校验分支 / 先验后写（预检构造 + 写盘失败回滚）/
保头 / 密钥不回显 / 备份还原。

全部打在 tmp 沙箱上：不碰真实 config/models.yaml、不碰真实 .env、
不换 gateway 单例（reload_gateway 被替成记录器，热生效真弹在验收②单独做）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import env_store, model_config, settings_api  # noqa: E402

HEADER = (
    "# ============================================================\n"
    "# 模型配置 · 唯一真源\n"
    "#\n"
    "# 历史裁定（逐字保留，拆闸铁律）：\n"
    "#   2026-10-02 拆闸裁定：不允许设置任意 max_tokens，不允许墙钟类闸。\n"
    "#     - 所有 max_tokens / timeout 全部移除\n"
    "# ============================================================\n"
)

BODY = """
providers:
  nous:
    protocol: openai
    base_url: "https://inference-api.nousresearch.com/v1"
    api_key_env: NOUS_API_KEY
    note: "Nous Research（OpenAI 兼容协议）"
  deepseek:
    protocol: deepseek
    api_key_env: DEEPSEEK_API_KEY
    models_url: "https://api.deepseek.com/v1/models"

models:
  haiku:
    provider: nous
    model: anthropic/claude-haiku-5.5
    temperature: 0.3
    thinking: auto
    note: "主模型"
  deepseek-pro:
    provider: deepseek
    model: deepseek-v4-pro
    temperature: 0.3

bindings:
  execute: haiku
  condense: deepseek-pro

system:
  default_model: haiku

simple:
  enabled: true

retry_grading:
  enabled: false
"""


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """沙箱：配置真源与 .env 全部指向 tmp；reload_gateway 换成记录器。"""
    cfg = tmp_path / "models.yaml"
    cfg.write_text(HEADER + BODY, encoding="utf-8")
    envf = tmp_path / ".env"
    envf.write_text("NOUS_API_KEY=sk-nous-abcdef1234\nDEEPSEEK_API_KEY=sk-ds-zzzz9999\n",
                    encoding="utf-8")
    backups = tmp_path / "_backups"
    backups.mkdir()

    monkeypatch.setattr(settings_api, "CONFIG_PATH", cfg)
    monkeypatch.setattr(settings_api, "BACKUP_DIR", backups)
    monkeypatch.setattr(model_config, "CONFIG_PATH", cfg)
    monkeypatch.setattr(env_store, "ENV_PATH", envf)
    monkeypatch.setattr(env_store, "BACKUP_DIR", backups)

    calls = []
    monkeypatch.setattr(settings_api.model_service, "reload_gateway",
                        lambda: calls.append(1) or "fake-gateway")

    app = FastAPI()
    app.include_router(settings_api.router)
    return {"cfg": cfg, "env": envf, "backups": backups,
            "reload_calls": calls, "client": TestClient(app)}


def _payload(**over):
    base = yaml.safe_load(HEADER + BODY)
    base.update(over)
    return base


# ---------- GET /models ----------


def test_get_models_returns_all_sections(env):
    r = env["client"].get("/api/settings/models")
    assert r.status_code == 200
    d = r.json()
    for key in ("providers", "models", "bindings", "system", "simple",
                "retry_grading", "roles", "key_status", "meta"):
        assert key in d, f"缺 {key}"
    assert set(d["providers"]) == {"nous", "deepseek"}
    assert set(d["models"]) == {"haiku", "deepseek-pro"}
    assert d["roles"], "角色应现读角色表"


def test_get_models_roles_come_from_roles_json(env):
    d = env["client"].get("/api/settings/models").json()
    assert "execute" in d["roles"]
    assert d["roles"]["viewpoint"]["multi"] is True


def test_get_models_key_status_has_no_plaintext(env):
    d = env["client"].get("/api/settings/models").json()
    ks = d["key_status"]
    assert ks["NOUS_API_KEY"]["configured"] is True
    assert ks["NOUS_API_KEY"]["tail"] == "1234"
    blob = r = env["client"].get("/api/settings/models").text
    assert "sk-nous-abcdef1234" not in blob
    assert "sk-ds-zzzz9999" not in blob


# ---------- PUT /models：成功路径 ----------


def test_put_models_succeeds_and_hot_reloads(env):
    r = env["client"].put("/api/settings/models", json=_payload())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["reloaded"] is True
    assert sorted(d["models"]) == ["deepseek-pro", "haiku"]
    assert env["reload_calls"], "保存后必须热重载"


def test_put_models_writes_to_disk(env):
    env["client"].put("/api/settings/models", json=_payload())
    back = yaml.safe_load(env["cfg"].read_text(encoding="utf-8"))
    assert back["models"]["haiku"]["model"] == "anthropic/claude-haiku-5.5"
    assert back["system"]["default_model"] == "haiku"
    assert "fallback_model" not in back["system"]  # [2026-10-19 裁定] 无降级模型


def test_put_models_preserves_header_byte_for_byte(env):
    """验收⑥兜底：保存一次，头部拆闸铁律逐字未变。

    比对基准取**保存前的实际头部**（含头部与首个顶层键之间的空行分隔），
    而不是硬编码常量——那才是「逐字保留」的真实含义。
    """
    orig_head, _ = model_config._split_header(env["cfg"].read_text(encoding="utf-8"))
    assert "拆闸铁律" in orig_head

    env["client"].put("/api/settings/models", json=_payload())

    text = env["cfg"].read_text(encoding="utf-8")
    head, _ = model_config._split_header(text)
    assert head == orig_head, "头部注释块必须逐字未变"
    # 拆闸铁律那几行逐字仍在
    assert "不允许设置任意 max_tokens" in head


def test_put_models_creates_backup(env):
    r = env["client"].put("/api/settings/models", json=_payload())
    name = r.json()["backup"]
    assert name and (env["backups"] / name).exists()


def test_put_models_keeps_new_fields(env):
    p = _payload()
    p["providers"]["nous"]["note"] = "改过的说明"
    p["models"]["haiku"]["note"] = "改过的模型说明"
    env["client"].put("/api/settings/models", json=p)
    back = yaml.safe_load(env["cfg"].read_text(encoding="utf-8"))
    assert back["providers"]["nous"]["note"] == "改过的说明"
    assert back["models"]["haiku"]["note"] == "改过的模型说明"


def test_put_models_ui_fields_do_not_reach_gateway_config(env):
    """验收⑦：UI 字段不进编译产物（编译发生在 §二，由 model_config 单测兜底）。"""
    p = _payload()
    p["models"]["haiku"]["label"] = "显示名"
    env["client"].put("/api/settings/models", json=p)
    compiled = model_config.compile(yaml.safe_load(env["cfg"].read_text(encoding="utf-8")))
    entry = compiled["models"]["haiku"]
    assert "note" not in entry and "label" not in entry and "models_url" not in entry


# ---------- PUT /models：六类 400 分支 ----------


def _assert_rejected_untouched(env, payload, needle):
    before = env["cfg"].read_bytes()
    mtime = env["cfg"].stat().st_mtime_ns
    r = env["client"].put("/api/settings/models", json=payload)
    assert r.status_code == 400, r.text
    d = r.json()
    assert d["ok"] is False and d["errors"]
    assert any(needle in e for e in d["errors"]), d["errors"]
    # 验收⑤：文件一字不动（内容与 mtime 都不变）
    assert env["cfg"].read_bytes() == before
    assert env["cfg"].stat().st_mtime_ns == mtime
    assert not env["reload_calls"], "被拒的配置不得触发热重载"
    return d


def test_400_empty_providers(env):
    _assert_rejected_untouched(env, _payload(providers={}), "供应商列表不能为空")


def test_400_empty_models(env):
    _assert_rejected_untouched(env, _payload(models={}), "模型列表不能为空")


def test_400_provider_missing_protocol(env):
    p = _payload()
    p["providers"]["nous"].pop("protocol")
    _assert_rejected_untouched(env, p, "缺 protocol")


def test_400_provider_missing_api_key_env(env):
    p = _payload()
    p["providers"]["nous"].pop("api_key_env")
    _assert_rejected_untouched(env, p, "缺 api_key_env")


def test_200_provider_auth_none_passes_without_key(env):
    """免鉴权供应商：声明 auth: none 即合法，不再逼编不存在的 key 名。

    [2026-10-25] 局域网端点（flash-next）与动态令牌（mavis broker 租约）
    两类供应商不走环境变量密钥；原规则把它们顶到「任何设置保存必败」。
    """
    p = _payload()
    p["providers"]["local"] = {"protocol": "openai", "base_url": "http://127.0.0.1:9/v1",
                               "auth": "none"}
    r = env["client"].put("/api/settings/models", json=p)
    assert r.status_code == 200, r.text
    back = yaml.safe_load(env["cfg"].read_text(encoding="utf-8"))
    assert back["providers"]["local"]["auth"] == "none"
    assert "api_key_env" not in back["providers"]["local"]


def test_200_provider_auth_broker_passes_without_key(env):
    """动态令牌供应商：auth: broker（mavis 租约桥）同样合法。"""
    p = _payload()
    p["providers"]["mavis-like"] = {"protocol": "anthropic",
                                    "base_url": "https://example.invalid/v1", "auth": "broker"}
    r = env["client"].put("/api/settings/models", json=p)
    assert r.status_code == 200, r.text


def test_400_provider_neither_key_nor_auth(env):
    """两者都不声明仍照旧拒——真缺失不被放松漏掉。"""
    p = _payload()
    p["providers"]["ghost"] = {"protocol": "openai", "base_url": "https://x.invalid/v1"}
    _assert_rejected_untouched(env, p, "缺 api_key_env")


# ---------- POST /models/reload（外部改文件后的只重载入口） ----------


def test_reload_endpoint_reloads_without_touching_file(env):
    """[2026-10-25] 文件被外部改动（git/直编）后，重载目录不必重启、不写盘。

    用户实况：新增模型座位后选择器看得见（GET 读文件）但保存必败
    （校验读运行中网关编译目录）——缺的入口就是这个。
    """
    before = env["cfg"].read_text(encoding="utf-8")
    r = env["client"].post("/api/settings/models/reload")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["reloaded"] is True
    assert isinstance(body["models"], list) and body["models"]
    assert env["reload_calls"], "必须真的触发 reload_gateway"
    # 一字不写盘：内容与 mtime 都不动
    assert env["cfg"].read_text(encoding="utf-8") == before


def test_400_model_provider_not_found(env):
    p = _payload()
    p["models"]["haiku"]["provider"] = "ghost"
    _assert_rejected_untouched(env, p, "供应商 'ghost' 不存在")


def test_400_model_without_provider(env):
    p = _payload()
    p["models"]["haiku"].pop("provider")
    _assert_rejected_untouched(env, p, "未指定供应商")


def test_400_binding_model_not_found(env):
    p = _payload()
    p["bindings"]["execute"] = "nonexistent"
    _assert_rejected_untouched(env, p, "模型 'nonexistent' 不存在")


def test_400_binding_list_item_not_found(env):
    p = _payload()
    p["bindings"]["viewpoint"] = ["haiku", "ghost-model"]
    _assert_rejected_untouched(env, p, "'ghost-model' 不存在")


def test_400_default_model_not_found(env):
    p = _payload()
    p["system"]["default_model"] = "nonexistent"
    _assert_rejected_untouched(env, p, "system.default_model")


def test_400_collects_multiple_errors(env):
    p = _payload()
    p["models"]["haiku"]["provider"] = "ghost"
    p["system"]["default_model"] = "also-missing"
    d = _assert_rejected_untouched(env, p, "供应商 'ghost' 不存在")
    assert len(d["errors"]) >= 2, "错误要一次全列，不能只报第一条"


def test_binding_unknown_role_is_warning_not_block(env):
    """角色侧刚加/刚删角色时，本页无权拦别人的配置——只警告。"""
    p = _payload()
    p["bindings"]["judge"] = "haiku"
    r = env["client"].put("/api/settings/models", json=p)
    assert r.status_code == 200
    assert any("judge" in w for w in r.json()["warnings"])


# ---------- PUT /models：先验后写 ----------


def test_precheck_failure_rejects_without_writing(env, monkeypatch):
    """② 预检构造失败 → 400，文件一字不动（半坏态防线）。"""
    def boom(_cfg):
        raise RuntimeError("坏协议")
    monkeypatch.setattr(settings_api, "build_gateway", boom)
    _assert_rejected_untouched(env, _payload(), "无法构造网关")


def test_write_failure_rolls_back_from_backup(env, monkeypatch):
    """⑥ 写盘失败 → 从备份回滚，磁盘复原。"""
    before = env["cfg"].read_text(encoding="utf-8")

    def boom(_cfg):
        raise OSError("磁盘满了")
    monkeypatch.setattr(model_config, "save", boom)

    r = env["client"].put("/api/settings/models", json=_payload())
    assert r.status_code == 500
    assert r.json()["rolled_back"] is True
    assert env["cfg"].read_text(encoding="utf-8") == before
    assert not env["reload_calls"], "写盘失败不得热重载"


def test_malformed_json_body_rejected(env):
    r = env["client"].put("/api/settings/models",
                          content=b"{not json",
                          headers={"content-type": "application/json"})
    assert r.status_code == 400


# ---------- 密钥 ----------


def test_get_keys_no_plaintext(env):
    r = env["client"].get("/api/settings/keys")
    assert r.status_code == 200
    assert r.json()["key_status"]["NOUS_API_KEY"]["tail"] == "1234"
    assert "sk-nous-abcdef1234" not in r.text


def test_put_key_writes_and_refreshes_environ(env, monkeypatch):
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    r = env["client"].put("/api/settings/keys/NOUS_API_KEY", json={"value": "sk-brand-new-4242"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["tail"] == "4242"
    assert "sk-brand-new-4242" not in r.text, "明文不得回显"
    assert os.environ["NOUS_API_KEY"] == "sk-brand-new-4242", "必须刷新进程内环境变量"
    assert "sk-brand-new-4242" in env["env"].read_text(encoding="utf-8")


def test_put_key_rejects_illegal_name(env):
    r = env["client"].put("/api/settings/keys/BAD-NAME", json={"value": "sk-x"})
    assert r.status_code == 400


def test_put_key_rejects_empty_value(env):
    r = env["client"].put("/api/settings/keys/NOUS_API_KEY", json={"value": "  "})
    assert r.status_code == 400


# ---------- 备份与还原 ----------


def test_list_backups_newest_first(env):
    for n in ("models_20260101_010101.yaml", "models_20260102_010101.yaml"):
        (env["backups"] / n).write_text(BODY, encoding="utf-8")
    r = env["client"].get("/api/settings/models/backups")
    names = [b["name"] for b in r.json()["backups"]]
    assert names[0] == "models_20260102_010101.yaml"


def test_restore_writes_back_and_reloads(env):
    old = (HEADER + BODY).replace("default_model: haiku", "default_model: deepseek-pro")
    (env["backups"] / "models_20260101_010101.yaml").write_text(old, encoding="utf-8")
    # 先把当前改坏
    env["client"].put("/api/settings/models", json=_payload(system={
        "default_model": "haiku"}))

    r = env["client"].post("/api/settings/models/restore",
                           json={"name": "models_20260101_010101.yaml"})
    assert r.status_code == 200, r.text
    back = yaml.safe_load(env["cfg"].read_text(encoding="utf-8"))
    assert back["system"]["default_model"] == "deepseek-pro"
    assert env["reload_calls"], "还原后必须热生效"


def test_restore_rejects_path_traversal(env):
    r = env["client"].post("/api/settings/models/restore",
                           json={"name": "../../config/models.yaml"})
    assert r.status_code in (400, 404)


def test_restore_missing_backup(env):
    r = env["client"].post("/api/settings/models/restore",
                           json={"name": "models_19700101_000000.yaml"})
    assert r.status_code == 404


# ---------- 拉列表 / 测试连接（无网分支） ----------


def test_fetch_without_key_returns_400(env, monkeypatch):
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    r = env["client"].post("/api/settings/models/fetch", json={"provider": "nous"})
    assert r.status_code == 400
    assert "密钥" in r.json()["error"]


def test_fetch_unknown_provider_404(env):
    r = env["client"].post("/api/settings/models/fetch", json={"provider": "ghost"})
    assert r.status_code == 404


def test_fetch_needs_endpoint_for_new_provider(env):
    r = env["client"].post("/api/settings/models/fetch",
                           json={"base_url": "", "api_key_env": "X", "api_key": "sk-x"})
    assert r.status_code == 400


def test_test_endpoint_without_key_returns_400(env, monkeypatch):
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    r = env["client"].post("/api/settings/models/test", json={"kind": "model",
                                                              "model_name": "haiku"})
    assert r.status_code == 400


def test_test_unknown_model_404(env):
    r = env["client"].post("/api/settings/models/test",
                           json={"kind": "model", "model_name": "ghost"})
    assert r.status_code == 404


def test_test_endpoint_never_sends_max_tokens(env, monkeypatch):
    """拆闸铁律：测试连接也不传输出上限——它测的就是生产的真实行为。"""
    seen = {}

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"model": "anthropic/claude-haiku-5.5",
                    "choices": [{"message": {"content": "pong"}}]}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            seen["url"] = url
            seen["headers"] = headers
            seen["body"] = json
            return FakeResp()

    monkeypatch.setattr(settings_api.httpx, "AsyncClient", FakeClient)
    r = env["client"].post("/api/settings/models/test",
                           json={"kind": "model", "model_name": "haiku"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["reply"] == "pong"
    assert "max_tokens" not in seen["body"], "测试请求不得带 max_tokens"
    assert seen["body"]["model"] == "anthropic/claude-haiku-5.5"


def test_fetch_sends_browser_headers_and_bearer(env, monkeypatch):
    """方案 §五·风险 4：opencode 类端点 403 靠浏览器头。"""
    seen = {}

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "deepseek-v4.1-flash", "owned_by": "x"}]}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            seen["url"] = url
            seen["headers"] = headers
            return FakeResp()

    monkeypatch.setattr(settings_api.httpx, "AsyncClient", FakeClient)
    r = env["client"].post("/api/settings/models/fetch", json={"provider": "nous"})
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert seen["headers"]["Authorization"].startswith("Bearer ")
    assert "Mozilla" in seen["headers"]["User-Agent"]
    assert seen["url"].endswith("/models")


def test_fetch_uses_explicit_models_url_when_given(env, monkeypatch):
    """deepseek 无 base_url，靠 models_url 拉。"""
    # 生产由 main.py 的 load_dotenv 填 os.environ；这里显式补上等价条件
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-zzzz9999")
    seen = {}

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "deepseek-chat"}]}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            seen["url"] = url
            return FakeResp()

    monkeypatch.setattr(settings_api.httpx, "AsyncClient", FakeClient)
    r = env["client"].post("/api/settings/models/fetch", json={"provider": "deepseek"})
    assert r.status_code == 200
    assert seen["url"] == "https://api.deepseek.com/v1/models"


def test_fetch_new_provider_uses_form_values(env, monkeypatch):
    """未保存的新供应商也要能拉：端点与密钥走请求体。"""
    seen = {}

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "m1"}, {"id": "m2"}]}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            seen["url"] = url
            seen["headers"] = headers
            return FakeResp()

    monkeypatch.setattr(settings_api.httpx, "AsyncClient", FakeClient)
    r = env["client"].post("/api/settings/models/fetch", json={
        "base_url": "https://new.invalid/v1", "api_key_env": "NEW_KEY",
        "api_key": "sk-new-secret", "protocol": "openai"})
    assert r.status_code == 200
    assert r.json()["count"] == 2
    assert seen["url"] == "https://new.invalid/v1/models"
    assert seen["headers"]["Authorization"] == "Bearer sk-new-secret"
    assert "sk-new-secret" not in r.text, "密钥不得回显"
# -*- coding: utf-8 -*-
"""模型设置接口（模型设置 · 第二步）。

    router = APIRouter(prefix="/api/settings")

六个端点（方案 §六）：
    GET  /models                    读全量（providers/models/bindings/system/simple/
                                   retry_grading/roles/key_status/meta）
    PUT  /models                    保存：校验 → 预检构造 → 备份 → 原子写 → 热重载
    POST /models/fetch              拉供应商的模型列表（加供应商时勾选用）
    POST /models/test               测试连接/模型（只提示，不拦截保存）
    GET  /keys  ·  PUT /keys/{name} 密钥状态与代存
    GET  /models/backups · POST /models/restore   备份与还原

三条不可让步的规矩：

1. **先验后写**（方案 §五·风险 5）。若先写盘、后构造 gateway，而新配置过了结构校验
   却过不了 gateway 构造，就会出现「磁盘已是新配置、运行中的单例还是旧的」——半坏态，
   要到下次重启才暴露。故顺序固定为：
       ① 结构校验 → 不过 400，文件一字不动
       ② 预检构造（build_gateway，但不换单例）→ 抛错 400，文件一字不动
       ③ 备份 → ④ 原子写盘 → ⑤ reload_gateway 热生效 → ⑥ 写盘失败从备份回滚
   ②⑤ 各构造一次 gateway 都是**纯内存**操作（ModelGateway.__init__ 只存 config），
   无网络无开销，换取确定性。

2. **密钥明文永不出接口**。只回 {configured, tail}（末 4 位）。
   日志里同样不出现值——本模块所有 logger 调用都只打键名/键数。

3. **不传 max_tokens**（拆闸铁律）。测试连接也守：测的就是生产的真实行为，
   生产不传任何输出上限，测试若传了，测的就不是它。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..model_gateway.gateway import build_gateway  # 既有导出，只读复用，不修改
from .. import roles
from . import env_store, model_config, model_service

logger = logging.getLogger("api.settings")

router = APIRouter(prefix="/api/settings")

BACKUP_DIR = env_store.BACKUP_DIR
CONFIG_PATH = model_config.CONFIG_PATH
MAX_BACKUPS = 20

# 深色主题之外的固定超时（秒）。拉列表与测试连接都走短超时，避免界面转圈。
FETCH_TIMEOUT = 15.0
TEST_TIMEOUT = 30.0

# 部分聚合端点（如 opencode-go）对裸 httpx 返回 403，要求浏览器风格请求头。
# 方案 §五·风险 4：这类端点统一带一组，不按域名逐个硬编。
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 无 base_url 的供应商（deepseek 走自有协议），测试/兜底拉取用
_PROTOCOL_DEFAULT_BASE = {
    "deepseek": "https://api.deepseek.com/v1",
}

# [2026-10-13 协议知识层] 思考强度档位：端点列表返回能力字段就用端点的（nous 的
# reasoning.supported_efforts、deepseek 官方的 effort.supported_levels）；端点列表**不返回**
# （opencode 聚合端点只给 id/owned_by）时，按模型家族已知协议补——这是家族知识，不是
# 探针测出来的。实测：opencode 的 deepseek-v4-flash 接受 reasoning_effort（low/high 真实
# 生效），DeepSeek 系协议自带思考强度档位。新 deepseek 系模型进来自动带档位，零请求零探针。
_KNOWN_REASONING: tuple[tuple[str, list[str]], ...] = (
    ("deepseek", ["low", "high", "max"]),
)


def _known_efforts(model_id: str) -> list[str]:
    """按模型 id 家族查已知思考档位；不认识返回空（界面回落开关）。"""
    mid = (model_id or "").lower()
    for family, levels in _KNOWN_REASONING:
        if family in mid:
            return list(levels)
    return []

TOP_SECTIONS = ("providers", "models", "bindings", "system", "simple", "retry_grading")


# ---------------------------------------------------------------- 读


def _load_raw() -> dict[str, Any]:
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("设置读配置失败 %s: %s", CONFIG_PATH, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _key_names(raw: dict[str, Any]) -> list[str]:
    """配置里引用到的全部密钥变量名（去重、保序）。"""
    names: list[str] = []
    for prov in (raw.get("providers") or {}).values():
        if isinstance(prov, dict) and prov.get("api_key_env"):
            n = str(prov["api_key_env"])
            if n not in names:
                names.append(n)
    return names


def _meta() -> dict[str, Any]:
    try:
        mtime = datetime.fromtimestamp(CONFIG_PATH.stat().st_mtime).astimezone().isoformat()
    except OSError:
        mtime = None
    return {"path": str(CONFIG_PATH), "mtime": mtime}


@router.get("/models")
async def get_models() -> JSONResponse:
    """读全量设置。roles 现读角色表；key_status 只给「已配置 + 末 4 位」。

    [2026-10-13 协议知识层] 已配置模型若配置里没有思考档位声明（端点列表不给，
    如 opencode 聚合端点只给 id/owned_by），按模型家族协议知识补——deepseek 系
    自动带 low/high/max（家族自带 reasoning_effort，实测接受且低档真实生效）。
    """
    raw = _load_raw()
    key_status = env_store.list_key_status(env_store.ENV_PATH, _key_names(raw))
    models = raw.get("models") or {}
    enriched: dict[str, Any] = {}
    for mname, mdef in models.items():
        if not isinstance(mdef, dict):
            enriched[mname] = mdef
            continue
        entry = dict(mdef)
        if not entry.get("reasoning_efforts"):
            model_id = str(entry.get("model") or mname)
            known = _known_efforts(model_id)
            if known:
                entry["reasoning_efforts"] = known
                if not entry.get("default_effort"):
                    # deepseek 官方端点实测 default_level=high（effort.supported_levels 同源）
                    entry["default_effort"] = "high"
        enriched[mname] = entry
    return JSONResponse({
        "ok": True,
        "providers": raw.get("providers") or {},
        "models": enriched,
        "bindings": raw.get("bindings") or {},
        "system": raw.get("system") or {},
        # [新建项目 2026-10-21] 分层配置与「新项目默认」：草稿态徽标显示后者
        # （depth_0 首选），不再拿 system.default_model 那个兜底值糊在用户脸上。
        "tiers": raw.get("tiers") or {},
        "new_project_default": model_config.new_project_default(),
        "simple": raw.get("simple") or {},
        "retry_grading": raw.get("retry_grading") or {},
        "roles": roles.list_roles(),
        "key_status": key_status,
        "meta": _meta(),
    })


# ---------------------------------------------------------------- 校验


def _validate(cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    """结构校验。返回 (errors, warnings)。errors 非空 → 拒存，文件一字不动。

    六条规则见方案 §6.2。绑定 key 不在角色表 → **只警告不拦截**（角色侧可能刚加/刚删，
    角色归模块侧管，本页无权删别人的配置）。
    """
    errors: list[str] = []
    warnings: list[str] = []

    providers = cfg.get("providers") or {}
    models = cfg.get("models") or {}
    bindings = cfg.get("bindings") or {}
    system = cfg.get("system") or {}

    # ① 供应商 / 模型非空
    if not isinstance(providers, dict) or not providers:
        errors.append("供应商列表不能为空")
    if not isinstance(models, dict) or not models:
        errors.append("模型列表不能为空")

    # ⑥ 每个供应商必须有 protocol ＋（api_key_env 或 auth 声明）
    # [2026-10-25 用户报「另一个卡片的调用凭什么挡我选模型」追出来的洞]
    # 原规则「必须有 api_key_env」假定所有供应商都走环境变量密钥——但有两类
    # 合法供应商不走：免鉴权（局域网端点，flash-next）与动态令牌（mavis 的
    # broker 租约现铸）。逼它们编一个不存在的 key 名是骗人，故开显式口子：
    # auth: none（免鉴权）/ auth: broker（租约桥等动态令牌）。两者都不声明
    # 仍照旧拒——真缺失不会被这条放松漏掉。
    for pname, prov in (providers or {}).items():
        if not isinstance(prov, dict):
            errors.append(f"供应商 '{pname}' 定义不是对象")
            continue
        if not prov.get("protocol"):
            errors.append(f"供应商 '{pname}' 缺 protocol")
        if not prov.get("api_key_env") and not prov.get("auth"):
            errors.append(
                f"供应商 '{pname}' 缺 api_key_env（免鉴权/动态令牌供应商须显式声明 auth: none|broker）"
            )

    # ② 每个模型的 provider 必须存在于 providers
    for mname, mdef in (models or {}).items():
        if not isinstance(mdef, dict):
            errors.append(f"模型 '{mname}' 定义不是对象")
            continue
        pname = mdef.get("provider")
        if not pname:
            errors.append(f"模型 '{mname}' 未指定供应商")
        elif pname not in (providers or {}):
            errors.append(f"模型 '{mname}' 的供应商 '{pname}' 不存在")

    # ③ 每个绑定值（字符串或数组项）必须存在于 models
    for role, val in (bindings or {}).items():
        items = val if isinstance(val, list) else [val]
        for m in items:
            if m is None or m == "":
                continue
            if m not in (models or {}):
                errors.append(f"绑定 '{role}' 指向的模型 '{m}' 不存在")

    # ④ system.default_model 必须存在于 models（[2026-10-19 裁定] 无降级模型，无 fallback_model）
    name = system.get("default_model")
    if name and name not in (models or {}):
        errors.append(f"system.default_model 指向的模型 '{name}' 不存在")

    # ⑤ 绑定 key 必须在角色表里——只警告
    known_roles = set(roles.list_roles() or {})
    for role in (bindings or {}):
        if role not in known_roles:
            warnings.append(f"绑定 '{role}' 不在角色表，已忽略")

    return errors, warnings


# ---------------------------------------------------------------- 备份 / 还原


def _backup_current() -> Optional[Path]:
    """把当前 models.yaml 备份到 _backups/models_YYYYmmdd_HHMMSS.yaml。"""
    if not CONFIG_PATH.exists():
        return None
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dst = BACKUP_DIR / f"models_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
        shutil.copy2(CONFIG_PATH, dst)
        _prune_backups()
        return dst
    except OSError as exc:
        logger.warning("模型配置备份失败: %s", exc)
        return None


def _prune_backups() -> None:
    """只保留最近 MAX_BACKUPS 份（时间倒序，旧的删）。"""
    try:
        files = sorted(BACKUP_DIR.glob("models_*.yaml"),
                       key=lambda p: p.name, reverse=True)
        for old in files[MAX_BACKUPS:]:
            old.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("备份清理失败: %s", exc)


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写文本（临时文件 → os.replace，同目录保证同卷）。"""
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 写


@router.put("/models")
async def put_models(request: Request) -> JSONResponse:
    """保存设置。先验后写，六步（方案 §6.2）。"""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400,
                            content={"ok": False, "errors": ["请求体不是合法 JSON"]})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400,
                            content={"ok": False, "errors": ["请求体必须是对象"]})

    # 只接受约定的六段，其余键忽略（防前端误传把顶层字段写坏）
    cfg = {k: payload.get(k) for k in TOP_SECTIONS}
    cfg["providers"] = cfg.get("providers") or {}
    cfg["models"] = cfg.get("models") or {}
    cfg["bindings"] = cfg.get("bindings") or {}
    cfg["system"] = cfg.get("system") or {}
    cfg["simple"] = cfg.get("simple") or {}
    cfg["retry_grading"] = cfg.get("retry_grading") or {}

    # ① 结构校验
    errors, warnings = _validate(cfg)
    if errors:
        return JSONResponse(status_code=400, content={"ok": False, "errors": errors})

    # ② 预检构造：「建」必须在「写盘」之前。抛错 → 400，文件一字不动。
    try:
        new_config = model_config.compile(cfg)
        build_gateway(new_config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("设置保存预检构造失败: %s", exc)
        return JSONResponse(status_code=400, content={
            "ok": False,
            "errors": [f"新配置无法构造网关: {type(exc).__name__}: {exc}"],
        })

    # ③ 备份当前
    backup = _backup_current()
    backup_name = backup.name if backup else None

    # ④ 原子写盘
    try:
        model_config.save(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.error("设置保存写盘失败: %s", exc)
        # ⑥ 从备份回滚，磁盘复原
        rolled = False
        if backup and backup.exists():
            try:
                _atomic_write_text(CONFIG_PATH, backup.read_text(encoding="utf-8"))
                rolled = True
            except Exception as rb_exc:  # noqa: BLE001
                logger.error("回滚失败: %s", rb_exc)
        return JSONResponse(status_code=500, content={
            "ok": False,
            "errors": [f"写盘失败: {exc}"],
            "rolled_back": rolled,
        })

    # ⑤ 热生效：从磁盘重读重建，一步换单例（不重启、不打断在跑调用）
    try:
        model_service.reload_gateway()
        reloaded = True
    except Exception as exc:  # noqa: BLE001
        logger.error("热重载失败: %s", exc)
        return JSONResponse(status_code=500, content={
            "ok": False,
            "errors": [f"已写盘但热重载失败: {exc}"],
            "reloaded": False,
        })

    return JSONResponse({
        "ok": True,
        "reloaded": reloaded,
        "models": list((cfg["models"] or {}).keys()),
        "backup": backup_name,
        "warnings": warnings,
    })


# ---------------------------------------------------------------- 拉模型列表


def _resolve_endpoint(base_url: str, protocol: Optional[str] = None) -> str:
    base = (base_url or "").rstrip("/")
    if not base and protocol:
        base = _PROTOCOL_DEFAULT_BASE.get(protocol, "")
    return base


def _fetch_headers(api_key: str, extra: Optional[dict] = None) -> dict[str, str]:
    """请求头。Authorization 带密钥；**不落日志、不回显**。"""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    headers.update(_BROWSER_HEADERS)
    if extra:
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


@router.put("/tiers")
async def put_tiers(request: Request) -> JSONResponse:
    """保存**分层与兜底顺序**（models.yaml 的 tiers 一段）。

    [层内兜底 2026-10-21 · 用户令] 「兜底的设置，你在设置里给我界面好了」。
    每层是一个有序模型列表：第一个是首选，后面依次是它挂掉时接替的。
    保存后立刻按新分层重算已有项目的默认行（src=manual 的不动）——
    否则界面改了、老卡还跑旧链，用户会以为没生效。
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400,
                            content={"ok": False, "errors": ["请求体不是合法 JSON"]})
    tiers = payload.get("tiers") if isinstance(payload, dict) else None
    if not isinstance(tiers, dict):
        return JSONResponse(status_code=400,
                            content={"ok": False, "errors": ["tiers 必须是对象"]})
    res = model_config.save_tiers(tiers)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    logger.info("[分层保存] tiers=%s 重算=%s", res.get("tiers"),
                (res.get("reseed") or {}).get("changed"))
    return JSONResponse(res)


@router.post("/models/fetch")
async def fetch_models(request: Request) -> JSONResponse:
    """拉某供应商的模型列表（加供应商时「拉取 → 勾选」用）。

    支持已保存的供应商（给 provider 名）或**尚未保存**的新供应商（给表单里的
    base_url/api_key_env/api_key）——新供应商还没保存也得能拉。
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体不是合法 JSON"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体必须是对象"})

    raw = _load_raw()
    providers = raw.get("providers") or {}

    pname = payload.get("provider")
    prov = providers.get(pname) if pname else None
    if prov is None and pname:
        return JSONResponse(status_code=404, content={"ok": False, "error": f"供应商 '{pname}' 不存在"})
    prov = prov or {}

    base_url = payload.get("base_url") or prov.get("base_url") or ""
    protocol = payload.get("protocol") or prov.get("protocol")
    models_url = payload.get("models_url") or prov.get("models_url")
    if not models_url:
        base = _resolve_endpoint(base_url, protocol)
        if not base:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "缺少端点地址：请填 base_url 或 models_url"})
        models_url = f"{base}/models"

    api_key_env = payload.get("api_key_env") or prov.get("api_key_env")
    # 密钥优先用请求体里的（未保存的新供应商只能这么传），否则读环境变量
    api_key = payload.get("api_key") or (os.environ.get(api_key_env) if api_key_env else "")
    if not api_key:
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": f"缺少密钥：请在页面填入，或先配置环境变量 {api_key_env or '(未指定)'}"})

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            resp = await client.get(models_url, headers=_fetch_headers(
                api_key, prov.get("extra_headers")))
        if resp.status_code >= 400:
            return JSONResponse(status_code=502, content={
                "ok": False,
                "error": f"拉取失败 HTTP {resp.status_code}",
                "detail": resp.text,
            })
        data = resp.json()
    except httpx.TimeoutException:
        return JSONResponse(status_code=504, content={"ok": False, "error": "拉取超时（15 秒）"})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={
            "ok": False, "error": f"拉取失败: {type(exc).__name__}: {exc}"})

    items = data.get("data") if isinstance(data, dict) else data
    models: list[dict[str, Any]] = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and (it.get("id") or it.get("name")):
                entry: dict[str, Any] = {
                    "id": str(it.get("id") or it.get("name")),
                    "owned_by": it.get("owned_by") or it.get("type") or "",
                }
                # [模型录入时从模型端获取能力 2026-10-13，修订] 取思考档位，三个来源依次：
                # ① 端点返回 reasoning.supported_efforts（nous 每家都带）；
                # ② 端点返回 effort.supported_levels（deepseek 官方用这个字段名）；
                # ③ 端点列表没给 → 按模型家族协议知识补（deepseek 系自带 low/high/max）。
                # 前两端点给什么界面就有什么；第三层是家族知识不是探针——opencode 的
                # deepseek-v4-flash 列表只给 id，但 DeepSeek 系协议自带 reasoning_effort。
                reasoning = it.get("reasoning") if isinstance(it.get("reasoning"), dict) else {}
                efforts = reasoning.get("supported_efforts") or []
                effort = it.get("effort") if isinstance(it.get("effort"), dict) else {}
                if not efforts:
                    efforts = effort.get("supported_levels") or []
                if not efforts:
                    efforts = _known_efforts(entry["id"])
                if isinstance(efforts, list) and efforts:
                    entry["reasoning_efforts"] = [str(x) for x in efforts]
                    if reasoning.get("default_effort"):
                        entry["default_effort"] = str(reasoning["default_effort"])
                    elif effort.get("default_level"):
                        entry["default_effort"] = str(effort["default_level"])
                    if reasoning.get("mandatory") is not None:
                        entry["reasoning_mandatory"] = bool(reasoning["mandatory"])
                models.append(entry)
    return JSONResponse({"ok": True, "count": len(models), "models": models})


# ---------------------------------------------------------------- 测试连接


@router.post("/models/test")
async def test_model(request: Request) -> JSONResponse:
    """测试连接/测试模型。

    直接走 httpx 最小请求，**不碰 gateway 单例**——这样能测「还没保存」的配置。
    **不传 max_tokens**（拆闸铁律：测的就是生产的真实行为）。失败只提示，不拦保存。
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体不是合法 JSON"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体必须是对象"})

    raw = _load_raw()
    providers = raw.get("providers") or {}
    models = raw.get("models") or {}

    kind = payload.get("kind") or "model"

    if kind == "model":
        mname = payload.get("model_name")
        mdef = models.get(mname) or {}
        if not mdef:
            return JSONResponse(status_code=404, content={"ok": False, "error": f"模型 '{mname}' 不存在"})
        prov = providers.get(mdef.get("provider")) or {}
        base_url = payload.get("base_url") or prov.get("base_url")
        protocol = prov.get("protocol")
        api_key_env = payload.get("api_key_env") or prov.get("api_key_env")
        api_key = payload.get("api_key") or (os.environ.get(api_key_env) if api_key_env else "")
        model_id = payload.get("model") or mdef.get("model")
        extra = prov.get("extra_headers")
    else:
        pname = payload.get("provider")
        prov = providers.get(pname) or {}
        base_url = payload.get("base_url") or prov.get("base_url")
        protocol = payload.get("protocol") or prov.get("protocol")
        api_key_env = payload.get("api_key_env") or prov.get("api_key_env")
        api_key = payload.get("api_key") or (os.environ.get(api_key_env) if api_key_env else "")
        extra = prov.get("extra_headers")
        model_id = payload.get("model")
        if not model_id:
            # 没指定型号：取该供应商下第一个已配置模型
            for mn, md in models.items():
                if isinstance(md, dict) and md.get("provider") == pname and md.get("model"):
                    model_id = md["model"]
                    break

    base = _resolve_endpoint(base_url, protocol)
    if not base:
        return JSONResponse(status_code=400, content={"ok": False, "error": "缺少端点地址"})
    if not model_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "缺少模型型号"})
    if not api_key:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": f"缺少密钥（配置环境变量 {api_key_env or '(未指定)'} 或直接填入）"})

    url = f"{base}/chat/completions"
    body = {"model": model_id, "messages": [{"role": "user", "content": "ping"}]}
    # 注意：body 里刻意没有 max_tokens（拆闸铁律）

    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT) as client:
            resp = await client.post(url, headers=_fetch_headers(api_key, extra), json=body)
        latency = int((time.time() - started) * 1000)
        if resp.status_code >= 400:
            return JSONResponse(status_code=200, content={
                "ok": False, "latency_ms": latency,
                "error": f"HTTP {resp.status_code}", "detail": resp.text})
        data = resp.json()
        reply = ""
        try:
            reply = (data["choices"][0]["message"]["content"] or "").strip()
        except Exception:  # noqa: BLE001
            reply = ""
        return JSONResponse({
            "ok": True, "latency_ms": latency,
            "reply": reply or "(空响应)",
            "model_used": (data.get("model") or model_id),
        })
    except httpx.TimeoutException:
        return JSONResponse(status_code=200, content={
            "ok": False, "error": f"测试超时（{int(TEST_TIMEOUT)} 秒）"})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=200, content={
            "ok": False, "error": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------- 密钥


@router.get("/keys")
async def get_keys() -> JSONResponse:
    """全部被引用密钥的状态。**只给 configured + 末 4 位，明文不出。**"""
    raw = _load_raw()
    return JSONResponse({"ok": True, "key_status": env_store.list_key_status(
        env_store.ENV_PATH, _key_names(raw))})


@router.put("/keys/{name}")
async def put_key(name: str, request: Request) -> JSONResponse:
    """代存密钥：备份 → 原子写 → os.environ 刷新（不重启即生效）。"""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体不是合法 JSON"})
    value = (payload or {}).get("value") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value.strip():
        return JSONResponse(status_code=400, content={"ok": False, "error": "缺少 value"})

    try:
        env_store.set_key(env_store.ENV_PATH, name, value.strip())
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        logger.error("密钥写入失败 %s: %s", name, exc)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"写入失败: {exc}"})

    # 值不回显，只回状态
    tail = value.strip()[-4:]
    return JSONResponse({"ok": True, "configured": True, "tail": tail, "name": name})


# ---------------------------------------------------------------- 备份 / 还原


@router.get("/models/backups")
async def list_backups() -> JSONResponse:
    """列出模型配置备份（时间倒序）。"""
    try:
        files = sorted(BACKUP_DIR.glob("models_*.yaml"),
                       key=lambda p: p.name, reverse=True)
    except OSError:
        files = []
    items = []
    for p in files[:MAX_BACKUPS]:
        try:
            st = p.stat()
            items.append({
                "name": p.name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(),
            })
        except OSError:
            continue
    return JSONResponse({"ok": True, "backups": items})


@router.post("/models/restore")
async def restore_backup(request: Request) -> JSONResponse:
    """从备份还原：先把当前版本也备份一份，再写回选定备份 → 热生效。"""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体不是合法 JSON"})
    name = (payload or {}).get("name") if isinstance(payload, dict) else None
    if not name or not isinstance(name, str):
        return JSONResponse(status_code=400, content={"ok": False, "error": "缺少备份名"})

    # 只允许取本目录下的 models_*.yaml，防路径穿越
    src = (BACKUP_DIR / name).resolve()
    if not str(src).startswith(str(BACKUP_DIR.resolve())) or not src.is_file():
        return JSONResponse(status_code=404, content={"ok": False, "error": "备份不存在"})
    if not src.name.startswith("models_") or src.suffix != ".yaml":
        return JSONResponse(status_code=400, content={"ok": False, "error": "不是模型配置备份"})

    try:
        text = src.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": f"备份读取失败: {exc}"})
    if not isinstance(data, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "备份内容不是合法配置"})

    errors, _ = _validate({k: data.get(k) for k in TOP_SECTIONS})
    if errors:
        return JSONResponse(status_code=400,
                            content={"ok": False, "errors": errors})

    saved = _backup_current()  # 还原前也留一份，用户还能再还原回来
    try:
        # 逐字写回（备份本身含完整头部，逐字还原最忠实）
        _atomic_write_text(CONFIG_PATH, text)
    except Exception as exc:  # noqa: BLE001
        logger.error("还原写盘失败: %s", exc)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"写盘失败: {exc}"})

    try:
        model_service.reload_gateway()
    except Exception as exc:  # noqa: BLE001
        logger.error("还原后热重载失败: %s", exc)
        return JSONResponse(status_code=500, content={
            "ok": False, "error": f"已还原但热重载失败: {exc}"})

    return JSONResponse({
        "ok": True,
        "reloaded": True,
        "restored": src.name,
        "backup": saved.name if saved else None,
        "models": list((data.get("models") or {}).keys()),
    })


@router.post("/models/reload")
async def reload_models() -> JSONResponse:
    """只重载、不写盘：models.yaml 被外部改动（git 拉取/直接编辑）后，让运行中的
    进程重读生效，**不必重启、不必打断任何在途调用**。

    为什么需要它（2026-10-25 实况）：直接改文件＋git 提交新增模型座位后，
    GET /models 读文件（有新座），但保存校验读运行中网关编译的目录（旧）⇒
    选择器里看得见、保存必失败；而目录重载原本只挂在「保存/还原」后面——
    两条路都要重写文件（丢注释），外部改文件的场景被逼到「重启」这一条路，
    重启又是进程级的，会被任何在途调用挡死（用户原话：「另一个卡片的调用，
    为什么影响这个卡片的模型选择？」——不该影响，缺的就是这个入口）。

    语义同 reload_gateway()：重读→重编译→重建单例，先建后换；在途调用持旧
    对象跑完，新调用拿新对象。文件已在盘上，编译＋建网关本身就是校验，
    故本端点不重复校验 payload、不碰文件一个字节。
    """
    try:
        model_service.reload_gateway()
    except Exception as exc:  # noqa: BLE001 坏配置在此现形：500 + 原文，不吞
        logger.error("模型配置重载失败: %s", exc)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"重载失败: {exc}"})
    return JSONResponse({
        "ok": True,
        "reloaded": True,
        "models": sorted(model_config.model_names()),
    })


# ================================================================
# 模型设置 · 按项目/卡片覆盖（per-work / per-card model override）
# ================================================================
# 语义（产品裁定 2026-10-19）：
#   - 模型目录（providers/models）全局唯一；覆盖只决定「角色→模型 / 项目默认模型」。
#   - 解析优先序：card > work > global(models.yaml)。
#   - PUT 为**整作用域替换**：发送什么即是什么；要清某字段就从 body 省略它。
#   - 任何引用的模型必须存在于全局目录（model_config.validate_override 校验）。
#   - fallback_model 保持全局（降级是韧性设置，不做 per-scope）。
# 载体：works/{work_id}/model_override.json（见 storage/model_override.py）。


def _override_work_or_404(work_id: str) -> Optional[JSONResponse]:
    """校验 work 存在；不存在返 404 响应，存在返 None。"""
    from ..storage import work_store
    try:
        if work_store.read_work(work_id) is None:
            return JSONResponse(status_code=404, content={"ok": False, "error": f"项目不存在: {work_id}"})
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"ok": False, "error": "读取项目失败"})
    return None


def _override_card_or_404(work_id: str, card_id: str) -> Optional[JSONResponse]:
    """校验卡片存在；不存在返 404 响应，存在返 None。"""
    from ..storage import card_store
    try:
        if not card_store.card_exists(work_id, card_id):
            return JSONResponse(status_code=404,
                                content={"ok": False, "error": f"卡片不存在: {card_id}"})
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"ok": False, "error": "读取卡片失败"})
    return None


def _override_body(payload: Any) -> Any:
    """校验覆盖请求体；非法返 400 JSONResponse，合法返 dict。"""
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体必须是对象"})
    return payload


@router.get("/model-overrides")
async def list_model_overrides() -> JSONResponse:
    """全部项目的覆盖概况（供设置界面选项目建下拉）：有覆盖的项目标记出来。"""
    from ..storage import work_store, model_override
    works: list[dict[str, Any]] = []
    for w in work_store.list_works():
        wid = str(w.get("work_id") or "")
        raw = model_override.load(wid)
        # [模型配置表 2026-10-21] 表里没有「work 级」：有行＝这个项目有配置
        cards = raw.get("cards")
        card_ids = [cid for cid, ov in (cards or {}).items()
                    if isinstance(ov, dict) and ov]
        work_has = bool(card_ids)
        works.append({
            "work_id": wid,
            "title": w.get("title") or wid,
            "has_work": work_has,
            "cards": card_ids,
        })
    return JSONResponse({"ok": True, "works": works})


@router.get("/model-overrides/{work_id}/effective")
async def get_model_override_effective(work_id: str, card_id: str = "") -> JSONResponse:
    """当前生效值（解析后）：项目级（card_id 空）或单卡级，供界面显示「实际用谁」。"""
    missing = _override_work_or_404(work_id)
    if missing is not None:
        return missing
    cid = card_id or None
    # 解析当前生效的默认模型与各角色绑定
    # [模型配置表 2026-10-21] 无卡＝项目的"表里一致的那个主模型"（项目默认）；
    # 表里各行不一致 ⇒ 没有单一项目默认，回落策略值。
    if cid:
        default = model_config.default_model(work_id=work_id, card_id=cid)
    else:
        from ..storage import model_override as _mo
        default = (_mo.project_default(work_id)
                   or model_config.default_model(work_id=work_id))
    role_eff: dict[str, Any] = {}
    for rid in roles.list_roles():
        val = model_config.pick_one(rid, work_id=work_id, card_id=cid)
        if val is not None:
            role_eff[rid] = val
    extra = {"card_id": card_id} if cid else {}
    return JSONResponse({
        "ok": True, "work_id": work_id, **extra,
        "default_model": default,
        "bindings": role_eff,
    })


@router.get("/model-overrides/{work_id}")
async def get_model_override(work_id: str, card_id: str = "") -> JSONResponse:
    """读某项目的模型配置表（每张卡一行）。

    [按需补行 2026-10-21] 带 card_id 时，若表里还没有这张卡，按默认策略补一行再返回。
    老项目（表还没建）否则永远读不到值 ⇒ 前端一路回落到 system.default_model
    （minimax），用户看到的就是「怎么永远锁在 minimax」。只补**这一张**（O(1)），
    不遍历全项目。
    """
    missing = _override_work_or_404(work_id)
    if missing is not None:
        return missing
    from ..storage import model_override
    if card_id:
        try:
            model_config.seed_card(work_id, card_id)   # 缺行就按默认策略补（幂等）
        except Exception as exc:  # noqa: BLE001 补行失败不影响读
            logger.warning("补模型配置行失败 work=%s card=%s: %s", work_id, card_id, exc)
    # [模型配置表 2026-10-21] 内部是「一张表存结果」；对外仍用旧形状（前端零改动）
    view = model_override.table_summary(work_id)
    return JSONResponse({
        "ok": True, "work_id": work_id,
        "work": view["work"],
        "types": view["types"],                # 项目的按卡类型策略快照（建卡时用）
        "cards": view["cards"],
    })


@router.put("/model-overrides/{work_id}")
async def put_model_override(work_id: str, request: Request) -> JSONResponse:
    """写 work 级覆盖（整作用域替换）。缺省/空 body = 清空 work 级覆盖。"""
    missing = _override_work_or_404(work_id)
    if missing is not None:
        return missing
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 JSON 解析/超时
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体不是合法 JSON"})
    payload = _override_body(body)
    if isinstance(payload, JSONResponse):
        return payload
    from ..storage import model_override
    cleaned = model_override._clean(payload)
    errors = model_config.validate_override(cleaned)
    if errors:
        return JSONResponse(status_code=400, content={"ok": False, "errors": errors})
    # [模型配置表 2026-10-21] 项目级改模型＝改表里每一行（人手改过的行不动）
    # 返回 affected：这次真正改了几张卡——前端据此明确提示「本项目 N 张卡」，
    # 避免用户以为只改了当前这张（他报的「切了一张卡结果全变了」正是这个歧义）。
    def _models_now() -> dict:
        return {cid: (v or {}).get("default_model")
                for cid, v in model_override.table_summary(work_id)["cards"].items()}
    before = _models_now()
    model_override.update_work(work_id, cleaned)
    after = _models_now()
    affected = sum(1 for cid in after if before.get(cid) != after[cid])
    view = model_override.table_summary(work_id)
    return JSONResponse({
        "ok": True, "work_id": work_id,
        "work": view["work"], "cards": view["cards"], "affected": affected,
    })


@router.delete("/model-overrides/{work_id}")
async def delete_model_override(work_id: str) -> JSONResponse:
    """清空 work 级覆盖（保留卡片级）。"""
    missing = _override_work_or_404(work_id)
    if missing is not None:
        return missing
    from ..storage import model_override
    model_override.clear_work(work_id)
    return JSONResponse({"ok": True, "work_id": work_id, "work": None})


@router.put("/model-overrides/{work_id}/card/{card_id}")
async def put_model_override_card(work_id: str, card_id: str, request: Request) -> JSONResponse:
    """写单卡覆盖（整作用域替换）。缺省/空 body = 清空该卡覆盖。"""
    missing = _override_work_or_404(work_id)
    if missing is not None:
        return missing
    missing_card = _override_card_or_404(work_id, card_id)
    if missing_card is not None:
        return missing_card
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 JSON 解析/超时
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体不是合法 JSON"})
    payload = _override_body(body)
    if isinstance(payload, JSONResponse):
        return payload
    from ..storage import model_override
    cleaned = model_override._clean(payload)
    errors = model_config.validate_override(cleaned)
    if errors:
        return JSONResponse(status_code=400, content={"ok": False, "errors": errors})
    # [模型配置表 2026-10-21] 改卡片模型＝改表里那一行（记 manual）
    model_override.update_card(work_id, card_id, cleaned)
    view = model_override.table_summary(work_id)
    return JSONResponse({
        "ok": True, "work_id": work_id, "card_id": card_id,
        "work": view["work"], "cards": view["cards"],
    })


@router.delete("/model-overrides/{work_id}/card/{card_id}")
async def delete_model_override_card(work_id: str, card_id: str) -> JSONResponse:
    """清空单卡覆盖。"""
    missing = _override_work_or_404(work_id)
    if missing is not None:
        return missing
    missing_card = _override_card_or_404(work_id, card_id)
    if missing_card is not None:
        return missing_card
    from ..storage import model_override
    model_override.clear_card(work_id, card_id)
    return JSONResponse({"ok": True, "work_id": work_id, "card_id": card_id, "cards": None})


# ================================================================
# 按卡片类型设置默认模型（全局模板 → 新建项目种子化）
# ================================================================
# 语义（2026-10-19 产品裁定）：后台按卡类型配置的模型，是**新建项目时的默认模板**；
# 每次新建项目会把这份模板整体拷贝成该项目自己的配置表（works/{wid}/model_override.json
# 的 types）。此后用户在项目内改某卡模型，以卡片覆盖为准，不回写全局。


@router.get("/card-type-models")
async def get_card_type_models() -> JSONResponse:
    """读全局模板 + 合法卡类型清单 + 当前模型目录（供设置界面）。"""
    return JSONResponse({
        "ok": True,
        "types": model_config.card_type_models(),
        "card_types": model_config.card_types(),
        "models": sorted(model_config.model_names()),
    })


@router.put("/card-type-models")
async def put_card_type_models(request: Request) -> JSONResponse:
    """保存全局模板：校验每个值都在模型目录；合法则原子写。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体不是合法 JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体必须是对象"})
    types = body.get("types") if isinstance(body, dict) else None
    if not isinstance(types, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "缺少 types 对象"})

    models = model_config.model_names()
    errors: list[str] = []
    cleaned: dict[str, str] = {}
    for ct, val in types.items():
        if not isinstance(val, str) or not val.strip():
            continue  # 空 = 未配置
        if val.strip() not in models:
            errors.append(f"类型 {ct} 引用了不在全局目录的模型: {val.strip()}")
        else:
            cleaned[str(ct)] = val.strip()
    if errors:
        return JSONResponse(status_code=400, content={"ok": False, "errors": errors})

    try:
        model_config.save_card_type_models(cleaned)
    except Exception as exc:  # noqa: BLE001
        logger.error("按卡类型模板保存失败: %s", exc)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"保存失败: {exc}"})
    return JSONResponse({"ok": True, "types": cleaned, "card_types": model_config.card_types()})
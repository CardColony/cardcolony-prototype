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
    """读全量设置。roles 现读角色表；key_status 只给「已配置 + 末 4 位」。"""
    raw = _load_raw()
    key_status = env_store.list_key_status(env_store.ENV_PATH, _key_names(raw))
    return JSONResponse({
        "ok": True,
        "providers": raw.get("providers") or {},
        "models": raw.get("models") or {},
        "bindings": raw.get("bindings") or {},
        "system": raw.get("system") or {},
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

    # ⑥ 每个供应商必须有 protocol + api_key_env
    for pname, prov in (providers or {}).items():
        if not isinstance(prov, dict):
            errors.append(f"供应商 '{pname}' 定义不是对象")
            continue
        if not prov.get("protocol"):
            errors.append(f"供应商 '{pname}' 缺 protocol")
        if not prov.get("api_key_env"):
            errors.append(f"供应商 '{pname}' 缺 api_key_env")

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

    # ④ system.default_model / fallback_model 必须存在于 models
    for field in ("default_model", "fallback_model"):
        name = system.get(field)
        if name and name not in (models or {}):
            errors.append(f"system.{field} 指向的模型 '{name}' 不存在")

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
                "detail": resp.text[:300],
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
                models.append({
                    "id": str(it.get("id") or it.get("name")),
                    "owned_by": it.get("owned_by") or it.get("type") or "",
                })
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
                "error": f"HTTP {resp.status_code}", "detail": resp.text[:300]})
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
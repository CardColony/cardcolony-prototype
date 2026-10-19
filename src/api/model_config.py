# -*- coding: utf-8 -*-
"""模型配置唯一入口（模型设置 · 第一步收拢）。

全项目只经本模块取模型配置，别处不许直读 config/models.yaml。

三层结构（一个文件 config/models.yaml）：
    providers  —— 供应商父层（端点/密钥/协议，去重只存一次）
    models     —— 模型子层（挂在供应商下，纯模型无角色语义）
    bindings   —— 角色→模型绑定（key 全部来自 config/roles.json，不手写角色）
    system     —— 默认模型 / 降级模型
    simple     —— 三席审议总开关

编译层：把三层结构编译成网关（src/model_gateway/gateway.py）认识的平铺 config：
    - provider 继承展开（供应商 protocol → 编译条目的 provider 字段）
    - 登记 vendor（供应商名）——供 llm_openai_compat 适配器判定 thinking 方言
    - thinking / reasoning_efforts / default_effort **原样带下去**，不翻译不注入
      （[2026-10-16 裁定 B] 翻译职责归适配器：llm_openai_compat 的 dialects 表管方言）

热加载语义：
    - resolve() / default_model() / model_names()：从 gateway.config **同源读**
      （bindings/system/models 三者来自同一次编译，「选模型」与「调用」永远
      同一份 config；测试替身替换 _gateway 后三者自动跟随——可控）。
    - 热生效走 reload_gateway()（重读→重编译→重建 gateway 单例，先建后换）：
      改文件后调它，「选模型」与「调用」原子切换，不重启、不打断在跑调用。
    - get_model() / list_providers() / list_models() / list_bindings()：现读文件
      （供第二步设置界面读取编辑用，不参与运行期「选模型/调用」）。
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger("api.model_config")

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = BASE_DIR / "config" / "models.yaml"

# [模型层底座抄写 2026-10-16 · 用户裁定 B] 编译层**不再翻译 thinking 方言 / 不再注入
# effort**——「翻译职责归适配器」（原版架构本意：effort / 方言都是适配器一等参数）。
# 方言表（minimax thinking.type=adaptive / aliyun enable_thinking）已迁进
# ``src/model_gateway/llm_openai_compat/dialects.py``；各模型的 thinking/default_effort
# 原样带进编译条目，由该适配器的 RequestDefaults 消费。
# 原 `_THINKING_DIALECT` / `_PROVIDER_DIALECT` 两张表**已删除**。

# 默认模型（角色未绑定时的回落）。承接原 model_pick.json 的 default→main 语义。
DEFAULT_MODEL = "haiku"

# [模型设置第二步 2026-10-12] 非 gateway 字段：设置界面用的字段，编译进 gateway 前一律弹出。
# [2026-10-16 收窄] thinking / reasoning_efforts / default_effort **不再弹出**——新底座
# 的适配器要读它们（thinking 声明 → RequestDefaults / 方言）。仍弹出的只剩纯界面字段。
_NON_GATEWAY_KEYS = {
    "note", "label", "models_url",
    "reasoning_mandatory",
}

# 文件头注释块的边界：第一个顶层键（行首非空白、非 # 的 key:）之前的一切。
# models.yaml 头部 1-13 行是「历史裁定（逐字保留，拆闸铁律）」——用户的永久禁令载体，
# yaml.safe_dump 不输出任何注释，保存会抹掉它。save() 切出旧头逐字前置。
_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):", re.MULTILINE)


def _load_raw_file() -> dict[str, Any]:
    """现读 config/models.yaml，返回原始三层结构。坏文件 fail-open 返空 dict（不抛）。"""
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("模型配置不存在: %s", CONFIG_PATH)
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型配置读取失败 %s: %s", CONFIG_PATH, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _compile(raw: dict[str, Any]) -> dict[str, Any]:
    """把三层结构编译成 gateway 认识的平铺 config。gateway.py 一行不碰。"""
    providers = raw.get("providers") or {}
    models = raw.get("models") or {}
    system = raw.get("system") or {}

    compiled_models: dict[str, Any] = {}
    for mname, mdef in models.items():
        if not isinstance(mdef, dict):
            continue
        prov_name = mdef.get("provider")
        prov = providers.get(prov_name) or {}
        if not isinstance(prov, dict):
            prov = {}

        # 1. 继承供应商字段（protocol/base_url/api_key_env/extra_headers/...）
        entry: dict[str, Any] = {}
        for k, v in prov.items():
            entry[k] = dict(v) if isinstance(v, dict) else v

        # 2. 模型自身字段覆盖（provider 字段除外——它是供应商引用，非 gateway 字段）
        for k, v in mdef.items():
            if k == "provider":
                continue
            entry[k] = dict(v) if isinstance(v, dict) else v

        # 3. protocol → provider（gateway 要的字段名 provider = litellm 协议）
        if "protocol" in entry:
            entry["provider"] = entry.pop("protocol")

        # 4. [模型层底座抄写 2026-10-16 · 用户裁定 B] **不再翻译 thinking 方言、
        #    不再注入 effort**——翻译职责归适配器（llm_openai_compat/dialects.py）。
        #    这里只登记供应商名 vendor，供 llm_openai_compat 适配器按供应商判定方言
        #    （迁自原 _PROVIDER_DIALECT 的判定维度）。
        entry["vendor"] = prov_name

        # 5. 弹出纯界面字段（note/label/models_url/reasoning_mandatory）。
        for _k in _NON_GATEWAY_KEYS:
            entry.pop(_k, None)

        compiled_models[mname] = entry

    return {
        "models": compiled_models,
        "simple": raw.get("simple") or {},
        "retry_grading": raw.get("retry_grading") or {"enabled": False},
        # bindings / system 一并编译进 config（gateway 只读 models/simple/retry_grading，
        # 多出的键它不理会、无害；但 resolve/default_model 要随 gateway 同源读，保持
        # 「选模型」与「调用」用同一份 config，测试替身替换 gateway 时二者同步）。
        "bindings": raw.get("bindings") or {},
        "system": raw.get("system") or {},
    }


def compile(raw: dict[str, Any]) -> dict[str, Any]:
    """公开编译口（第二步设置界面「先验后写」用）：三层 raw → gateway 平铺 config。

    与 load() 的分工：load() 现读磁盘文件；本函数吃调用方给的**内存**结构，
    供保存前预检构造 gateway 用（先验后写——新配置必须先过构造，才允许写盘）。
    """
    return _compile(raw)


def load() -> dict[str, Any]:
    """现读 + 编译，返回 gateway 认识的平铺 config。"""
    return _compile(_load_raw_file())


def reload() -> dict[str, Any]:
    """重读 + 重编译（供 model_service.reload_gateway 热重载重建 gateway）。"""
    return load()


def _gateway_config() -> dict[str, Any]:
    """取 gateway 的 config（与调用同源）。取不到返空 dict（fail-open）。"""
    try:
        from . import model_service as _ms  # 延迟 import 避循环
        return _ms.get_gateway().config or {}
    except Exception:  # noqa: BLE001
        logger.warning("模型配置：gateway config 获取失败")
        return {}


# ============================================================
# [模型设置按项目/卡片覆盖 2026-10-19]
# 覆盖层：card > work > global(models.yaml)。载体与读写见
# src/storage/model_override.py；本模块只做「解析优先序」，不碰文件。
# ============================================================


def _scope_overrides(work_id: str, card_id: Optional[str]):
    """按需取 (work 级覆盖, card 级覆盖)；失败/缺失 ⇒ 两个空 dict（fail-open）。"""
    try:
        from ..storage import model_override as _mo  # 延迟 import 避循环
    except Exception:  # noqa: BLE001
        return {}, {}
    try:
        return _mo.load_scope(work_id, card_id)
    except Exception:  # noqa: BLE001
        logger.warning("模型覆盖读取失败 work=%s card=%s", work_id, card_id)
        return {}, {}


def _pick_override_binding(role: str, work_ov: dict, card_ov: dict):
    """按 card > work 顺序在覆盖 bindings 里找 role；命中即返原始值（str/list），
    未命中返 None（让位全局）。"""
    for ov in (card_ov, work_ov):
        b = ov.get("bindings")
        if not isinstance(b, dict):
            continue
        if role in b:
            return b[role]
    return None


def _pick_override_default(work_ov: dict, card_ov: dict, global_default: str) -> str:
    """按 card > work 顺序在覆盖 default_model 里找；都没设置返全局默认。"""
    for ov in (card_ov, work_ov):
        v = ov.get("default_model")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return global_default


def resolve(role: str, *, work_id: Optional[str] = None,
            card_id: Optional[str] = None) -> Optional[Any]:
    """角色 → 模型名（全局 bindings 同源读；work/card 覆盖优先）。

    解析优先序：card 覆盖 > work 覆盖 > 全局 bindings。
    multi 返 list[str]，单角色返 str；未绑定（缺键/空值）返 None。
    值是否合法由上层校验：pick_model 保留原始值让消费点显式报错，
    resolve_model 用 model_names() 校验并回落——分层校验，避免「配错名静默用默认」。

    同源保证「选模型」与「调用」同一份 config；热生效经
    reload_gateway 重建 gateway（先建后换）后自然更新。
    """
    if work_id or card_id:
        work_ov, card_ov = _scope_overrides(work_id, card_id)
        hit = _pick_override_binding(role, work_ov, card_ov)
        if hit is not None:
            return [str(x) for x in hit] if isinstance(hit, list) else str(hit)
    cfg = _gateway_config()
    bindings = cfg.get("bindings") or {}
    hit = bindings.get(role)
    if hit is None or hit == "" or hit == []:
        return None

    if isinstance(hit, list):
        return [str(x) for x in hit]
    return str(hit)


def default_model(*, work_id: Optional[str] = None,
                  card_id: Optional[str] = None) -> str:
    """回落模型名（角色未绑定时）。work/card 覆盖优先于 system.default_model。

    从 gateway.config.system.default_model 同源读，兜底 DEFAULT_MODEL。
    card 覆盖 > work 覆盖 > system.default_model > DEFAULT_MODEL。
    随 gateway 重建更新。
    """
    system = (_gateway_config().get("system")) or {}
    gd = str(system.get("default_model")) if system.get("default_model") else DEFAULT_MODEL
    if work_id or card_id:
        work_ov, card_ov = _scope_overrides(work_id, card_id)
        return _pick_override_default(work_ov, card_ov, gd)
    return gd


def validate_override(override: dict) -> list[str]:
    """校验覆盖里引用的模型是否都在当前全局目录（model_names 键集）。

    供 settings 侧保存前校验用：覆盖只能从全局目录里挑模型，不能发明新模型。
    返回错误文案列表（空 = 合法）。
    """
    models = model_names()
    errs: list[str] = []
    if not isinstance(override, dict):
        return ["覆盖必须是对象"]

    v = override.get("default_model")
    if isinstance(v, str) and v.strip() and v.strip() not in models:
        errs.append(f"default_model 引用了不在全局目录的模型: {v.strip()}")

    bindings = override.get("bindings")
    if isinstance(bindings, dict):
        for role, val in bindings.items():
            vals = val if isinstance(val, list) else [val]
            for item in vals:
                if isinstance(item, str) and item and item not in models:
                    errs.append(f"角色 {role} 引用了不在全局目录的模型: {item}")
    return errs


def model_names() -> set[str]:
    """当前 gateway 已编译的模型键集（供 resolve_model 校验）。

    从 gateway.config.models 同源读——随 reload_gateway 重建 gateway 后更新；
    「选模型」与「调用」同一份 config，选出的模型名必然可调用。
    """
    models_cfg = (_gateway_config().get("models")) or {}
    return set(models_cfg.keys())


def get_model(name: str) -> Optional[dict[str, Any]]:
    """取某模型定义（raw 三层结构里的 models 条目）。未命中返 None。"""
    models = _load_raw_file().get("models") or {}
    entry = models.get(name)
    return entry if isinstance(entry, dict) else None


def list_providers() -> dict[str, Any]:
    return _load_raw_file().get("providers") or {}


def list_models() -> dict[str, Any]:
    return _load_raw_file().get("models") or {}


def list_bindings() -> dict[str, Any]:
    return _load_raw_file().get("bindings") or {}


def _split_header(text: str) -> tuple[str, str]:
    """切出「文件头注释块」与「第一个顶层键起的正文」。无顶层键则整体算头。

    头部是 models.yaml 头部 1-13 行的「历史裁定（逐字保留，拆闸铁律）」。
    yaml.safe_dump 不产注释，保存会把它抹成一片空白——必须逐字捞回来前置。
    """
    m = _TOP_KEY_RE.search(text)
    if not m:
        return text, ""
    return text[:m.start()], text[m.start():]


def save(cfg: dict[str, Any]) -> None:
    """原子写盘（先写临时文件后 os.replace），不触发 reload（由调用方决定）。

    [模型设置第二步 2026-10-12] **保头**：从现有文件切出头部注释块逐字保留、重新前置，
    再拼上 safe_dump 的正文。头里是用户的永久禁令载体（拆闸铁律），一个字节都不能丢。
    正文（各段内部的行内注释）无法保留——项目未装 ruamel.yaml，做不到；这些注释已在
    第二步迁进 note 字段，界面上可见可改，语义不丢。

    第二步设置界面经此保存；第一步先立函数。
    """
    try:
        old = CONFIG_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        old = ""
    header, _ = _split_header(old)
    body = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    text = header + body

    fd, tmp_path = tempfile.mkstemp(dir=str(CONFIG_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
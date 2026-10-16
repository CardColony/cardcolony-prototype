# -*- coding: utf-8 -*-
"""模型配置唯一入口（模型设置 · 第一步收拢）。

全项目只经本模块取模型配置，别处不许直读 config/models.yaml。

三层结构（一个文件 config/models.yaml）：
    providers  —— 供应商父层（端点/密钥/协议，去重只存一次）
    models     —— 模型子层（挂在供应商下，纯模型无角色语义）
    bindings   —— 角色→模型绑定（key 全部来自 config/roles.json，不手写角色）
    system     —— 默认模型 / 降级模型
    simple     —— 三席审议总开关

编译层：把三层结构编译成 gateway（src/model_gateway/gateway.py，禁改）认识的
平铺 config：
    - provider 继承展开（供应商 protocol → gateway 的 provider 字段）
    - thinking 方言翻译（off/on → 各家私有参数）
    - fallback 复制（system.fallback_model → config["models"]["fallback"]）

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

# 思考方言翻译表：模型名 → 方言（各家私有参数）。
# thinking=off 时按方言注入「关闭思考」参数；thinking=on 时按方言注入「开启」参数。
# 无方言（auto / 其它模型 / 未知模型）→ 不传任何思考参数（端点自然行为）。
_THINKING_DIALECT = {
    "minimax-m3": "minimax",          # MiniMax：extra_body.thinking.type = disabled
    "minimax-m31-flash-preview": "minimax",  # [2026-10-15] 更名后模型也认（旧键对不上导致 on 不注入）
    "qwen-max": "qwen",               # qwen：extra_body.enable_thinking = false/true
}
# [2026-10-15] provider 维度方言：同一供应商下思考方言一致，模型更名/新增自动跟随，
# 避免"模型名不在方言表 → thinking 白写不注入"的假配置。minimax / aliyun(qwen) 两系。
_PROVIDER_DIALECT = {
    "minimax": "minimax",
    "aliyun": "qwen",
}

# 默认模型（角色未绑定时的回落）。承接原 model_pick.json 的 default→main 语义。
DEFAULT_MODEL = "haiku"

# [模型设置第二步 2026-10-12] 非 gateway 字段：设置界面用的字段，编译进 gateway 前一律弹出。
# _compile() 把供应商**全部字段**继承展开到模型条目，任何 UI 字段都会随之泄漏进
# gateway.config（gateway 不认，多余键虽无害，但配置面污染、验收⑦要过）。
#   thinking —— 已翻译进 extra_body / reasoning_effort（方言），字段本身 gateway 不认
#   note     —— 界面说明文字（原行内注释迁入，用户可编辑）
#   label    —— 界面显示名（缺省用模型键名）
#   models_url —— 拉模型列表地址（供应商级；deepseek 无 base_url 需显式给）
#   reasoning_efforts / default_effort / reasoning_mandatory —— [2026-10-13] 模型录入时
#       从模型端拉回的思考档位声明（端点给的 UI 元数据），编译进 gateway 前弹出。
_NON_GATEWAY_KEYS = {
    "thinking", "note", "label", "models_url",
    "reasoning_efforts", "default_effort", "reasoning_mandatory",
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

        # 4. thinking 方言翻译。thinking 取值分三类：
        #    - auto        —— 不传思考参数（端点自然行为）
        #    - on / off    —— 开关语义
        #    - 具体档位     —— [2026-10-13] 模型录入时从模型端获取的思考强度
        #                      （如 Nous 的 low/medium/high/max），翻译成端点参数。
        thinking = mdef.get("thinking", "auto")
        # YAML 会把裸 off/on 解析成布尔 False/True，规范化为字符串再判方言
        if thinking is True:
            thinking = "on"
        elif thinking is False:
            thinking = "off"
        elif not isinstance(thinking, str):
            thinking = "auto"
        # 方言解析：先按模型名，再按供应商（更名/新增模型自动跟随，避免"配置写 on 不注入"）。
        dialect = _THINKING_DIALECT.get(mname) or _PROVIDER_DIALECT.get(prov_name)
        # DeepSeek 系判定（OpenAI 兼容层走 reasoning_effort；opencode 聚合端点的 deepseek 系都在此列）
        is_deepseek = "deepseek" in mname.lower() or "deepseek" in str(entry.get("model", "")).lower()
        if dialect == "minimax":
            # [探针实测 2026-10-12] MiniMax 的 thinking.type **只接受
            # adaptive / disabled**，传 enabled 直接 400（invalid thinking.type:
            # "enabled" (allowed: adaptive, disabled)）。故「开思考」＝adaptive，
            # 档位值也一律映射 adaptive（该端点没有分档能力）。
            entry.setdefault("extra_body", {})
            entry["extra_body"]["thinking"] = {
                "type": "disabled" if thinking == "off" else "adaptive",
            }
        elif dialect == "qwen":
            entry.setdefault("extra_body", {})
            entry["extra_body"]["enable_thinking"] = (thinking != "off")
            # qwen 档位（若端点提供）→ thinking_budget（未实测，暂不注入；见 note）
        elif thinking not in ("auto", "on", "off"):
            # 无方言（Nous/DeepSeek 档位等）：thinking 是具体档位 → 注入
            # reasoning_effort（OpenAI 兼容层通用思考强度参数；Nous 实测
            # supported_parameters 含 "reasoning_effort"）。端点不认则失败由
            # 网关/调用层暴露，界面录入时只存端点声明过的档位，天然安全。
            entry["reasoning_effort"] = thinking
        elif thinking == "on" and is_deepseek:
            # [2026-10-15] 修"配置 on 不注入"：DeepSeek 系无方言，但自带
            # reasoning_efforts/default_effort 能力声明，on 必须真正开思考——
            # 注入 default_effort（缺省 high），否则思考流(第3环)永远取不到增量。
            entry["reasoning_effort"] = str(mdef.get("default_effort") or "high")
        # 其它（auto / off / 无方言非 deepseek）→ 不传思考参数（端点自然行为）

        # 5. 弹出非 gateway 字段（thinking 已翻译进 extra_body，字段本身 gateway 不认；
        #    note/label/models_url 是设置界面字段，继承或覆盖进来的都一并弹出——验收⑦）
        for _k in _NON_GATEWAY_KEYS:
            entry.pop(_k, None)

        compiled_models[mname] = entry

    # 6. fallback 复制：system.fallback_model → config["models"]["fallback"]
    fb_name = system.get("fallback_model")
    if fb_name and fb_name in compiled_models and "fallback" not in compiled_models:
        compiled_models["fallback"] = dict(compiled_models[fb_name])

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


def resolve(role: str) -> Optional[Any]:
    """角色 → 模型名（从 gateway.config.bindings **同源读**，不校验值是否在键集）。

    multi 返 list[str]，单角色返 str；未绑定（缺键/空值）返 None。
    值是否合法由上层校验：pick_model 保留原始值让消费点显式报错，
    resolve_model 用 model_names() 校验并回落——分层校验，避免「配错名静默用默认」。

    同源保证「选模型」与「调用」同一份 config；热生效经
    reload_gateway 重建 gateway（先建后换）后自然更新。
    """
    cfg = _gateway_config()
    bindings = cfg.get("bindings") or {}
    hit = bindings.get(role)
    if hit is None or hit == "" or hit == []:
        return None

    if isinstance(hit, list):
        return [str(x) for x in hit]
    return str(hit)


def default_model() -> str:
    """回落模型名（角色未绑定时）。从 gateway.config.system.default_model 同源读，
    兜底 DEFAULT_MODEL。随 gateway 重建更新。"""
    system = (_gateway_config().get("system")) or {}
    name = system.get("default_model")
    return str(name) if name else DEFAULT_MODEL


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
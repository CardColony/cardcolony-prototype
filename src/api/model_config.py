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

import json
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
CARD_TYPE_MODELS_PATH = BASE_DIR / "config" / "card_type_models.json"
CARD_TYPE_REGISTRY_PATH = BASE_DIR / "config" / "card_type_registry.json"

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
        # [模型分层 / 并发调度 2026-10-19] 一并编译进 config：
        #   tiers       —— 按卡深度选模型（resolve 读）
        #   concurrency —— 每模型并发上限（gateway._sem_for 读）
        # 与 bindings/system 同理：gateway 不理会这两个键，但 resolve 与并发闸要
        # 跟「调用」读同一份 config。
        "tiers": raw.get("tiers") or {},
        "concurrency": raw.get("concurrency") or {},
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


#: 未建 gateway 时的编译配置缓存（按 models.yaml mtime 失效）。
#  选模型是高频动作（每轮每步一次），每次重读 yaml + 编译太贵（实测 18 个模型
#  的编译在每次 pick 里跑一遍）。gateway 建好后走它的 config，与本缓存无关。
_CFG_CACHE: dict[str, Any] = {"key": None, "cfg": {}}


def _compiled_config_cached() -> dict[str, Any]:
    # key 必须含**路径**：测试/沙箱会把 CONFIG_PATH 指到临时文件，而同一秒内
    # 创建的两个 tmp 文件 mtime 可能相同 ⇒ 只按 mtime 会命中别人的缓存。
    try:
        st = CONFIG_PATH.stat()
        key = (str(CONFIG_PATH), st.st_mtime, st.st_size)
    except OSError:
        return {}
    if _CFG_CACHE.get("key") == key and _CFG_CACHE.get("cfg"):
        return _CFG_CACHE["cfg"]
    cfg = _compile(_load_raw_file())
    _CFG_CACHE["key"] = key
    _CFG_CACHE["cfg"] = cfg
    return cfg


def _gateway_config() -> dict[str, Any]:
    """取 gateway 的 config（与调用同源）。取不到返空 dict（fail-open）。

    [模型配置表 2026-10-21 · 修] **绝不在这里构造 gateway**。
    原实现调 `model_service.get_gateway()`，而它是懒初始化单例——于是「选模型」
    这个纯读动作会把网关（含 MOCK_LLM 判定、真实适配器）提前固化下来。
    实测踩到：建卡时 seed 一次模型 → 网关在 MOCK_LLM 生效前就被建成真实网关
    ⇒ 之后所有测试都打真实 API。选模型不需要实例，只需要配置。
    """
    try:
        from . import model_service as _ms  # 延迟 import 避循环
        gw = getattr(_ms, "_gateway", None)
        if gw is not None:
            return gw.config or {}
    except Exception:  # noqa: BLE001
        pass
    try:
        return _compiled_config_cached()     # 未建实例：读+编译（按 mtime 缓存）
    except Exception:  # noqa: BLE001
        logger.warning("模型配置：编译配置获取失败")
        return {}


# ============================================================
# [模型配置表 2026-10-21] 项目级配置的载体与读写全在
# src/storage/model_override.py（``works/{wid}/model_config_table.json``）。
# 本模块**不再做「覆盖优先序」**——那是旧设计（card>work>global），已被
# 「一张表存结果、运行时只读表」取代。原来这里两个函数
# ``_scope_overrides`` / ``_override_hit`` 已删除（无人调用）。
# ============================================================


def _norm(v: Any) -> Any:
    """统一成 str 或 list[str]（配置里可能写成元组/带空串）。"""
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return str(v)


def _load_card_type_models() -> dict[str, Any]:
    """读全局模板；缺/坏 ⇒ {"types": {}}（fail-open）。不抛。"""
    try:
        data = json.loads(CARD_TYPE_MODELS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"types": {}}
    return data if isinstance(data, dict) else {"types": {}}


def card_type_models() -> dict[str, Any]:
    """全局模板现读（type → 模型键；空串=未配置）。供设置界面与新建项目种子化。"""
    return _load_card_type_models().get("types") or {}


def save_card_type_models(types: dict) -> None:
    """保存全局模板（原子写）。由 settings 侧先 validate_override 校验再调本函数。"""
    cleaned = {k: v for k, v in (types or {}).items() if isinstance(v, str) and v.strip()}
    data = {"_meta": {
        "version": 1,
        "purpose": "按卡片类型设置默认模型（全局模板）。新建项目时整体拷贝进该项目的模型配置表。",
    }, "types": cleaned}
    CARD_TYPE_MODELS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def card_types() -> list[str]:
    """全部合法卡类型（去重、保序）：root 词表 + 各项目类 L1 词表。供设置界面。"""
    try:
        reg = json.loads(CARD_TYPE_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    vocab = reg.get("l1_vocabularies") or {}
    seen: list[str] = []
    for scope in ("root",):
        for t in (vocab.get(scope, {}) or {}).get("types", []) or []:
            c = t.get("code")
            if c and c not in seen:
                seen.append(c)
    for v in vocab.values():
        for t in (v or {}).get("types", []) or []:
            c = t.get("code")
            if c and c not in seen:
                seen.append(c)
    return seen


def _card_type_of(work_id: str, card_id: str) -> str:
    """查一张卡的类型；失败 ⇒ ""（回落全局）。fail-open 不抛。"""
    try:
        from ..storage import card_store
        meta = card_store._meta_of(work_id, card_id) or {}
        ct = meta.get("card_type")
        return str(ct) if ct else ""
    except Exception:  # noqa: BLE001
        return ""


def _work_type_model(work_id: str, card_type: str) -> str:
    """项目配置表 types[card_type]；读不到 ⇒ ""。"""
    try:
        from ..storage import model_override as _mo
        return str(_mo.load_types(work_id).get(card_type) or "")
    except Exception:  # noqa: BLE001 读不到 ⇒ 交给下一层
        return ""


# ============================================================
# [模型配置表 2026-10-21 · 用户裁定]
#   「每个项目有且只有一张模型配置表，整个系统都按这张表调模型；
#     新建项目/卡时按一套默认策略走，把结果填进这张表；
#     改卡片模型就是更新表里那一行。」
#
# ⇒ 运行时**只读表**，不做优先级仲裁（改前那套 card>work>tier>bindings>types
#   的链是错的：多个来源并存，必然出现「改了不生效 / 谁赢谁」）。
#   默认策略（_policy_value）只在**填表那一刻**被调用一次：
#     · 新建卡：card_store 建完立刻 seed_card()
#     · 老卡/漏填：第一次取模型时现算一次并写入（自愈）
#
# 三个消费口，其余全是薄包装：
#   pick()      → 表里那一行给该角色的值（str | list[str]）
#   pick_one()  → 单值（池则按深度选：上层主备优先、深层轮询分散）
#   pick_pool() → 列表（单值包成 [x]；三席用它）
# ============================================================


def pick(role: str, *, work_id: Optional[str] = None,
         card_id: Optional[str] = None) -> Any:
    """模型选择：**读配置表**。表里没有这张卡 ⇒ 按默认策略填一次再读。"""
    role = str(role or "default")

    if work_id and card_id:
        from ..storage import model_override as _mo
        row = _mo.get_row(work_id, card_id)
        if row is None:
            row = _policy_row(work_id, card_id)
            try:
                _mo.put_row(work_id, card_id, row, src="default")   # 自愈填行
            except Exception as exc:  # noqa: BLE001 写表失败不影响本次选择
                logger.warning("模型配置表写入失败 work=%s card=%s: %s",
                               work_id, card_id, exc)
        got = _row_value(role, row)
        if got is not None:
            if _still_available(got):
                return got
            # 表里那个模型已经不在当前模型目录（换过 models.yaml / 测试替身）
            # ⇒ 按当前策略回落，**不改表**（人的选择留着，等他自己改或补回目录）
            logger.warning("模型配置表里的模型已不可用，按默认策略回落 "
                           "work=%s card=%s role=%s model=%s",
                           work_id, card_id, role, got)
            return _policy_value(role, work_id, card_id)
        # 行里没有这个角色的例外、也没有主模型（极端）⇒ 现算兜底，不落表

    # 无卡（全局后台动作：凝练/压缩等）⇒ 按默认策略现算，不落表
    return _policy_value(role, work_id, card_id)


def _still_available(v: Any) -> bool:
    """表里的值是否还在当前模型目录里。目录取不到（gateway 未就绪）⇒ 不判。"""
    try:
        names = model_names()
    except Exception:  # noqa: BLE001
        return True
    if not names:
        return True
    items = v if isinstance(v, list) else [v]
    return any(str(x) in names for x in items)


def _row_value(role: str, row: dict) -> Any:
    """从表的一行里取该角色的值：bindings[role] 优先，其次该行主模型。"""
    if not isinstance(row, dict):
        return None
    b = row.get("bindings")
    if isinstance(b, dict):
        v = b.get(role)
        if v not in (None, "", []):
            return _norm(v)
    m = row.get("model")
    if isinstance(m, str) and m.strip():
        return m.strip()
    return None


def _policy_value(role: str, work_id: Optional[str],
                  card_id: Optional[str]) -> Any:
    """**默认策略**（只在填表时用，运行时不该走到这里）。

    auto 卡按深度（tiers）> 角色绑定（bindings）> 卡类型模板（types）> 系统默认。
    """
    cfg = _gateway_config()

    tier = _tier_raw(work_id, card_id, cfg)
    if tier is not None:
        return _norm(tier)

    hit = (cfg.get("bindings") or {}).get(role)
    if hit not in (None, "", []):
        return _norm(hit)

    if work_id and card_id:
        ct = _card_type_of(work_id, card_id)
        if ct:
            pt = _work_type_model(work_id, ct)
            if pt:
                return pt
            gt = card_type_models().get(ct)
            if gt:
                return str(gt)

    system = (cfg.get("system") or {})
    return str(system.get("default_model") or DEFAULT_MODEL)


def _policy_row(work_id: str, card_id: str) -> dict:
    """按默认策略生成**表里的一行**：主模型 ＋ 三席池（若有）。"""
    row: dict[str, Any] = {}
    main = _policy_value("default", work_id, card_id)
    if isinstance(main, list):
        main = _choose_from_pool(main, work_id, card_id)
    if main:
        row["model"] = str(main)
    pool = _policy_value("viewpoint", work_id, card_id)
    if isinstance(pool, list):
        pool = [str(x) for x in pool if str(x).strip()]
        if len(pool) > 1:            # 单值不必存例外（主模型就是它）
            row["bindings"] = {"viewpoint": pool}
    return row


def seed_card(work_id: str, card_id: str, *, force: bool = False) -> dict:
    """把某张卡的默认策略结果**写进表**（新建卡时调用；force=True 覆盖已有行）。"""
    from ..storage import model_override as _mo
    if not work_id or not card_id:
        return {}
    if not force and _mo.get_row(work_id, card_id) is not None:
        return _mo.get_row(work_id, card_id) or {}
    row = _policy_row(work_id, card_id)
    try:
        _mo.put_row(work_id, card_id, row, src="default")   # 策略填的（人没改过）
    except Exception as exc:  # noqa: BLE001 建卡不因填表失败而失败
        logger.warning("模型配置表填行失败 work=%s card=%s: %s", work_id, card_id, exc)
    return row


def seed_project(work_id: str, card_ids: Optional[list[str]] = None) -> int:
    """把项目里**还没填过的卡**一次性填进表（老项目自愈 / 建项目后补全）。"""
    from ..storage import model_override as _mo
    if card_ids is None:
        try:
            from ..storage import card_store
            card_ids = [c.get("card_id") for c in card_store.list_cards(work_id) or []
                        if c.get("card_id")]
        except Exception:  # noqa: BLE001 列不出来就只填已有行
            card_ids = list(_mo.list_rows(work_id).keys())
    n = 0
    for cid in card_ids or []:
        if _mo.get_row(work_id, cid) is None:
            seed_card(work_id, cid)
            n += 1
    return n


def _choose_from_pool(pool: list[str], work_id: Optional[str],
                      card_id: Optional[str]) -> str:
    """池里挑一个：上层（depth ≤ 1）主备顺序优先；深层（depth ≥ 2）按实时占用分散。

    gateway 拿不到（未初始化/测试）⇒ crc32 分散（同卡稳定、跨卡分散）。
    """
    pool = [str(x) for x in (pool or []) if str(x).strip()]
    if not pool:
        return ""
    if len(pool) == 1:
        return pool[0]
    depth = _card_depth(str(work_id or ""), str(card_id or "")) if card_id else 0
    try:
        from . import model_service as _ms
        gw = getattr(_ms, "_gateway", None)   # 只读已有实例，不构造（见 _gateway_config 注）
        if gw is not None:
            mode = "primary" if depth <= 1 else "spread"
            picked = gw.pick_from(list(pool), mode=mode)
            if picked:
                return picked
    except Exception:  # noqa: BLE001 gateway 不可用 ⇒ 退回静态分散
        pass
    import zlib
    idx = zlib.crc32(str(card_id or "").encode("utf-8")) % len(pool)
    return pool[idx]


def pick_one(role: str, *, work_id: Optional[str] = None,
             card_id: Optional[str] = None) -> str:
    """单值口：池按深度选一个。所有「只接受一个模型名」的调用方用它。"""
    v = pick(role, work_id=work_id, card_id=card_id)
    if isinstance(v, list):
        return _choose_from_pool(v, work_id, card_id)
    return str(v)


def pick_pool(role: str, *, work_id: Optional[str] = None,
              card_id: Optional[str] = None) -> list[str]:
    """列表口：单值包成 [x]。三席（viewpoint）用它——席数＝池大小。"""
    v = pick(role, work_id=work_id, card_id=card_id)
    return list(v) if isinstance(v, list) else [str(v)]


def default_model(*, work_id: Optional[str] = None,
                  card_id: Optional[str] = None) -> str:
    """「这张卡/项目的默认模型」＝ pick_one("default")。保留旧签名（多处消费）。"""
    return pick_one("default", work_id=work_id, card_id=card_id)


def resolve(role: str, *, work_id: Optional[str] = None,
            card_id: Optional[str] = None) -> Any:
    """角色 → 模型名（保留旧签名）。**实现只有 pick 一条链**。

    与改前的差别：未绑定不再返 None——底座自带兜底（system.default_model）。
    需要「未配置」语义的调用方请自行比较返回值与 bindings，不要再分叉解析。
    """
    return pick(role, work_id=work_id, card_id=card_id)


def _card_depth(work_id: str, card_id: str) -> int:
    """卡在树中的深度（根卡＝0）。读不到 / 成环 / 超过 32 层 ⇒ 停在当前值。"""
    try:
        from ..storage import card_store
    except Exception:  # noqa: BLE001 存储不可用 ⇒ 当作根
        return 0
    depth = 0
    cur = str(card_id or "")
    seen: set[str] = set()
    while cur and cur not in seen and depth < 32:
        seen.add(cur)
        try:
            meta = card_store._meta_of(work_id, cur)
        except Exception:  # noqa: BLE001 读不到就停
            break
        cur = str((meta or {}).get("parent") or "")
        if cur:
            depth += 1
    return depth


def _tier_raw(work_id: Optional[str], card_id: Optional[str],
              cfg: dict[str, Any]) -> Any:
    """该卡在深度分层里的原始值（str 或 list）；不适用 ⇒ None。

    **只对 auto 模式的卡生效**：分层是为「一棵自动组织起来的树」设计的默认设置——
    普通对话卡有自己的角色绑定，不该被它在树里的位置悄悄改写。

    [模型选择底座 2026-10-21] 取消原 `_TIER_ROLES` 白名单：分层是「这张卡的
    默认模型」，对**所有角色**成立（三席跟着池走、工具轮跟着走）。原来只放行
    execute/think/synthesize/draft/default，导致同一个底座里有两套规则：
    auto 卡上角色绑定被 tier 静默盖掉，而 viewpoint/condense 又不受分层管——
    这正是「互相冲突」的来源。现在一条规则：auto 卡按深度，手动卡按绑定。
    """
    tiers = cfg.get("tiers") or {}
    if not tiers or not card_id:
        return None
    try:
        from ..storage import card_store
        meta = card_store._meta_of(str(work_id or ""), str(card_id)) or {}
    except Exception:  # noqa: BLE001 读不到卡 ⇒ 不分层
        return None
    if not meta.get("auto_mode"):
        return None
    depth = _card_depth(str(work_id or ""), str(card_id))
    return tiers.get(f"depth_{depth}") or tiers.get("default") or None


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
"""[批六·阶段复用 COST_MEMORY_STAGE_CACHE] 凝练/归拢的**带版本阶段缓存**。

共同契约（任务书 §二）：

- **阶段身份由宿主构造**：key = sha256(canonical(stage, work, card, session,
  shard, 输入指纹集, 模型座, 线路配置身份, 提示指纹, 选项指纹, 校验版本))。
  原文/可见性/选材/阶段契约任一变化 ⇒ 换键不再命中；等长改写、块边界
  变化、顺序变化、影响可见性的宿主字段（quarantine 等）变化同样失效。
- **阶段过校验后才落盘**：命中方以同一机械契约复核产物（解析函数原样
  重跑）——坏产物、半写文件、损坏 JSON 一律按未命中记账。
- 文件名与旧版分开（``stage-{key}.json``），开态只从新版记录取命中；
  读损坏/写失败不阻断，沿原阶段和失败记账继续。
- **缓存产物只是发布候选**：提交侧仍走 ``save_memory`` 版本比较与
  一致发布——本模块不管发布。
- 保留有界：同卡同阶段至多保留 ``KEEP_PER_STAGE`` 条最新记录，
  超龄文件在写盘时顺带回收（``_prune``，失败不阻断）。

关态（``COST_MEMORY_STAGE_CACHE != 1``）：本模块完全不被调用，旧路逐字保留。
缩量试点 ``COST_MEMORY_COMPACT_PROMPT=1`` 仅在阶段缓存开时生效。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CACHE_VERSION = 2
KEEP_PER_STAGE = 16          # 同卡同阶段最多留 16 份最新产物
_MAX_RECORD_BYTES = 1 << 21  # 单份产物上限 2MiB——超限不落盘


@dataclass(frozen=True)
class StageResponse:
    text: str
    origin_call: str | None


def stage_on() -> bool:
    return str(os.environ.get("COST_MEMORY_STAGE_CACHE") or "").strip() == "1"


def compact_on() -> bool:
    """缩量试点只在阶段缓存开启时生效（任务书 §四）。"""
    return stage_on() and str(
        os.environ.get("COST_MEMORY_COMPACT_PROMPTS") or "").strip() == "1"


# ================================================================ 指纹
def sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def canon(obj: Any) -> str:
    """确定性 JSON 序列化（key 排序、UTF-8、无空白）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def block_fp(block: dict[str, Any]) -> dict[str, Any]:
    """**单块内容指纹**——含影响可见性与语义的全部宿主字段。

    等长改写（content sha 变）、块边界变化（seq/覆盖区间变）、顺序
    变化（键序由调用方保证）、隔离态变化（quarantine 位）都换指纹。"""
    return {
        "seq": int(block.get("seq") or 0),
        "role": str(block.get("role") or block.get("actor") or ""),
        "from": str(block.get("from") or ""),
        "ts": str(block.get("ts") or ""),
        "quarantine": str(block.get("quarantine") or ""),
        "sha": sha(str(block.get("content") or "")),
    }


def blocks_fp(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """有序块列指纹——**次序本身是键的一部分**（列表顺序入 JSON）。"""
    return [block_fp(b) for b in blocks]


def member_fp(node: dict[str, Any]) -> dict[str, Any]:
    """归拢成员节点指纹：块号之外把 gist/覆盖/timeline/level/子条/
    隔离态全部计入——顶层 seq+gist 恒定时的成员内容变化也失效。"""
    anchor = node.get("anchor") or {}
    tl = node.get("timeline") or {}
    return {
        "seq": int(anchor.get("seq") or 0),
        "gist_sha": sha(str(node.get("gist") or "")),
        "covers": canon(node.get("covers") or node.get("coveredSeqs") or []),
        "timeline": canon([tl.get("from"), tl.get("to")]),
        "level": int(node.get("level") or 0),
        "status": str(node.get("status") or ""),
        "sunk": bool(node.get("sunk")),
        "children": canon(node.get("lower") or []),
    }


def stage_key(*, work_id: str, card_id: str, session_id: str, stage: str,
              shard: str = "", inputs: Any = None, model_seat: str = "",
              route: str = "", prompt_fp: str = "", options_fp: str = "",
              validate_ver: int = CACHE_VERSION) -> str:
    """阶段身份键：宿主身份 + 有序输入指纹 + 线路 + 提示 + 校验版本。"""
    return sha(canon({
        "v": validate_ver, "w": work_id, "c": card_id, "s": session_id,
        "stage": stage, "shard": shard, "in": inputs,
        "seat": model_seat, "route": route,
        "prompt": prompt_fp, "opts": options_fp}))


# ================================================================ 读写（原子）
def _dir(work_id: str, card_id: str) -> Path:
    from . import session_store
    return (session_store.WORKS_DIR.parent / "logs" / "stage_cache"
            / str(work_id) / str(card_id))


def _path(work_id: str, card_id: str, key: str) -> Path:
    return _dir(work_id, card_id) / f"stage-{key}.json"


def read(work_id: str, card_id: str, key: str) -> Optional[dict[str, Any]]:
    """读阶段记录。损坏/半写/版本不符 ⇒ None（调用方按未命中记账）。"""
    try:
        raw = _path(work_id, card_id, key).read_text(encoding="utf-8")
        rec = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(rec, dict) or rec.get("v") != CACHE_VERSION:
        return None
    if rec.get("key") != key:
        return None
    return rec


def write(work_id: str, card_id: str, key: str, *, stage: str,
          input_fp: Any, product: Any, origin_call: str = "") -> bool:
    """临时文件＋原子替换落盘；产物超限/写失败 ⇒ False（不阻断）。"""
    try:
        payload = json.dumps(
            {"v": CACHE_VERSION, "key": key, "stage": stage,
             "input_fp": input_fp, "product": product,
             "origin_call": str(origin_call or ""), "ts": time.time()},
            ensure_ascii=False)
        if len(payload.encode("utf-8")) > _MAX_RECORD_BYTES:
            logger.warning("[stage_cache] 产物超界不落盘 stage=%s", stage)
            return False
        d = _dir(work_id, card_id)
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".stage-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, str(_path(work_id, card_id, key)))
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        try:
            _prune(work_id, card_id, stage)   # 回收是独立的尽力操作——
        except Exception as exc:              # 任何回收失败都不得打断
            logger.warning(                    # 刚写完的有效产物
                "[stage_cache] 回收失败（产物已写，不阻断）stage=%s: %s",
                stage, exc)
        return True
    except OSError as exc:
        logger.warning("[stage_cache] 写失败（不阻断）stage=%s: %s", stage, exc)
        return False


def _prune(work_id: str, card_id: str, stage: str) -> None:
    """同阶段产物数超 KEEP_PER_STAGE ⇒ 按 mtime 删最旧。

    尽力操作：任何一份记录不可读/不可判都不抛出——损坏、半写、
    非 dict 结构（如合法 JSON ``[]``）一律当作待回收候选处理。"""
    try:
        d = _dir(work_id, card_id)
        files = [p for p in d.glob("stage-*.json")]
        recs: list[tuple[float, Path]] = []
        for p in files:
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(rec, dict) and rec.get("stage") == stage:
                    recs.append((p.stat().st_mtime, p))
                    continue
                recs.append((0.0, p))   # 非本阶段/非 dict/损坏 ⇒ 优先回收
            except (OSError, ValueError, TypeError):
                recs.append((0.0, p))   # 损坏文件优先回收
        recs.sort()
        for _mt, p in recs[:max(0, len(recs) - KEEP_PER_STAGE)]:
            try:
                p.unlink()
            except OSError:
                pass
    except (OSError, TypeError):
        pass


# ================================================================ 命中/落账门面
def lookup(work_id: str, card_id: str, *, stage: str, key: str,
           revalidate, require_origin: bool = False) -> Optional[Any]:
    """命中即复核：读记录 → 用**同一机械契约**（调用方给的 revalidate
    函数）重跑校验，过了才当产物用。读坏、校验不过 ⇒ None。"""
    rec = read(work_id, card_id, key)
    if rec is None or (require_origin and not rec.get("origin_call")):
        return None
    product = rec.get("product")
    try:
        ok = revalidate(product)
    except Exception:  # noqa: BLE001 校验异常按未命中
        return None
    if not ok:
        logger.info("[stage_cache] 命中但复核不过 stage=%s（作废）", stage)
        return None
    logger.info("[stage_cache] 阶段命中 stage=%s（零出站）work=%s card=%s",
                stage, work_id, card_id)
    return product


def store_valid(work_id: str, card_id: str, *, stage: str, key: str,
                input_fp: Any, product: Any, origin_call: str = "") -> None:
    """**过校验后**才允许落盘——调用方必须先跑机械校验。"""
    write(work_id, card_id, key, stage=stage,
          input_fp=input_fp, product=product, origin_call=origin_call)

# -*- coding: utf-8 -*-
"""模型设置覆盖（per-work / per-card model override）。

产品形态：模型的「目录」（providers + models 定义）是全局唯一的，只存在于
config/models.yaml；但「角色 → 模型」的绑定、以及项目的默认/降级模型，
允许在**项目（work）级**与**卡片（card）级**覆盖。解析优先级：
    card 覆盖 > work.types[card_type]（项目自身表）> 全局模板[card_type] > 全局默认。

载体：每个 work 一个 ``works/{work_id}/model_override.json``，结构：
    {
      "types": {"project": "...", "sub_project": "...", ...},  // 项目自己的配置表（新建时从全局模板拷贝）
      "cards": {"card_002": {"default_model": "...", "bindings": {...}}, ...}
    }
- types 缺失/为空 ⇒ 该类型回落全局模板/全局默认。
- cards[card_id] 是单卡覆盖；键缺省/值为空 ⇒ 让位（继承 types[card_type]）。
- 该文件属于运行数据（同 cards.db / sessions），随快照备份，不进 git。
- 坏文件/缺文件 ⇒ fail-open 空结构（解析层回落全局），绝不抛。

本模块只负责「读写与标准化」；「解析优先序 / 校验模型引用是否在全局目录」在
src/api/model_config.py 与 settings 侧完成。设计原则：分层解耦，覆盖存取
与配置语义互不掺。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import work_store

logger = __import__("logging").getLogger("storage.model_override")

OVERRIDE_FILE = "model_override.json"


def path_for(work_id: str) -> Path:
    return work_store._work_dir(work_id) / OVERRIDE_FILE


def has_any(work_id: str) -> bool:
    """该 work 是否有任何覆盖（types 或任一张卡）。"""
    return bool(load(work_id))


def load(work_id: str) -> dict[str, Any]:
    """读整份覆盖文件。缺/坏 ⇒ {}. 不抛。"""
    p = path_for(work_id)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型覆盖读取失败 work=%s: %s", work_id, exc)
        return {}
    return data if isinstance(data, dict) else {}


def load_types(work_id: str) -> dict[str, str]:
    """取该项目的「按卡类型配置表」（type → 模型键）；缺/坏 ⇒ {}。不抛。"""
    data = load(work_id)
    t = data.get("types")
    if not isinstance(t, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in t.items():
        if isinstance(v, str) and v.strip():
            out[str(k)] = v.strip()
    return out


def seed_types(work_id: str, template: dict) -> dict:
    """新建项目时把全局模板整体拷成该项目的配置表（只写 types，不动 cards）。"""
    data = load(work_id)
    data["types"] = _clean_types(template)
    data.setdefault("cards", {})
    _atomic_write(data, work_id)
    return data


def load_scope(work_id: str, card_id: Optional[str] = None) -> tuple[dict, dict]:
    """取 (work 级覆盖, card 级覆盖)；无覆盖 ⇒ 空 dict。不抛。"""
    data = load(work_id)
    work_ov = data.get("work")
    if not isinstance(work_ov, dict):
        work_ov = {}
    card_ov: dict = {}
    if card_id:
        cards = data.get("cards")
        if isinstance(cards, dict):
            co = cards.get(card_id)
            if isinstance(co, dict):
                card_ov = co
    return work_ov, card_ov


def _clean_types(template: dict) -> dict[str, str]:
    """标准化配置表：只保留非空字符串值；空/None ⇒ 丢弃（表示未配置、回落）。"""
    out: dict[str, str] = {}
    if not isinstance(template, dict):
        return out
    for k, v in template.items():
        if isinstance(v, str) and v.strip():
            out[str(k)] = v.strip()
    return out


def _clean(override: dict) -> dict:
    """标准化覆盖：只保留非空、合法形态的字段；空串/None/空表视作「没设」（丢弃）。"""
    out: dict[str, Any] = {}
    if not isinstance(override, dict):
        return out
    v = override.get("default_model")
    if isinstance(v, str) and v.strip():
        out["default_model"] = v.strip()
    bindings = override.get("bindings")
    if isinstance(bindings, dict):
        cleaned: dict[str, Any] = {}
        for role, val in bindings.items():
            if val is None or val == "" or val == []:
                continue
            if isinstance(val, list):
                items = [str(x) for x in val if isinstance(x, str) and x.strip()]
                if items:
                    cleaned[str(role)] = items
            elif isinstance(val, str) and val.strip():
                cleaned[str(role)] = val.strip()
        if cleaned:
            out["bindings"] = cleaned
    return out


def _atomic_write(data: dict[str, Any], work_id: str) -> None:
    """原子落盘；work 目录不存在则创建。覆盖后再写，不 trim 语义。"""
    p = path_for(work_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_work(work_id: str, override: dict) -> dict:
    """写 work 级覆盖（覆盖式）。返回保存后的整份文件。"""
    data = load(work_id)
    data["work"] = _clean(override)
    data.setdefault("cards", {})
    _atomic_write(data, work_id)
    return data


def update_card(work_id: str, card_id: str, override: dict) -> dict:
    """写单卡覆盖。返回保存后的整份文件。"""
    data = load(work_id)
    data.setdefault("work", {})
    cards = data.setdefault("cards", {})
    cards[card_id] = _clean(override)
    _atomic_write(data, work_id)
    return data


def clear_work(work_id: str) -> dict:
    """清空 work 级覆盖（保留卡片级）。整份为空则删文件。"""
    data = load(work_id)
    data.pop("work", None)
    _write_or_remove(data, work_id)
    return data


def clear_card(work_id: str, card_id: str) -> dict:
    """清空某张卡的覆盖。整份为空则删文件。"""
    data = load(work_id)
    cards = data.get("cards")
    if isinstance(cards, dict):
        cards.pop(card_id, None)
        if not cards:
            data.pop("cards", None)
    _write_or_remove(data, work_id)
    return data


def _write_or_remove(data: dict[str, Any], work_id: str) -> None:
    if data:
        _atomic_write(data, work_id)
    else:
        p = path_for(work_id)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
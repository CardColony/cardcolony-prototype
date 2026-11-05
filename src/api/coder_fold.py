# -*- coding: utf-8 -*-
"""worker 卡归档编排（模块名沿用 coder_fold）:卡行⇄档案行的搬运(卡片世界与记录世界之间唯一的门)。

归档 = 档案行写 + cards 行/影子行删 + 会话与事件文件原位不动。
解档 = cards 行重建(使命还原/状态复位) + 档案行删(会话延续)。
触发(见 routes/message_runner 接线):worker 回合收口后自动;silence_card 同一动作。
"""
from __future__ import annotations

import logging
from typing import Any

from ..storage import card_db, card_store, coder_archive, shadow_store

logger = logging.getLogger(__name__)


async def archive_worker(work_id: str, card_id: str) -> dict[str, Any]:
    """完工 worker 卡折叠成归档记录。幂等(已归档 ⇒ 原样返回);非 worker/未完工 ⇒ 拒。

    会话/事件文件不动——它们就是剩下的记录(timeline 按卡号照常可查)。
    """
    meta = card_store._meta_of(work_id, card_id) or {}
    if str(meta.get("card_type") or "") != "worker":
        return {"ok": False, "error": "only_worker",
                "message": "归档只作用于worker 卡"}
    # [机制判断归harness 第3步] 完工判定走派生视图（事件权威＋列回落）。
    from ..infra import card_status as _cs
    _dstate = _cs.derive(work_id, card_id)["delivery"]
    if _dstate != "completed":
        return {"ok": False, "error": "not_completed",
                "message": f"worker 卡尚未完工(state={_dstate or '?'})"}

    # 产物清单从影子提取(路径指针);影子随卡行一起退役
    artifacts: list[str] = []
    try:
        raw = (await shadow_store.read_shadow_raw(work_id, card_id)) or ""
        for line in raw.splitlines():
            s = line.strip().lstrip("- ").strip()
            if s and (":" in s or s.startswith(("D:", "C:", "/")) or "\\" in s
                      or s.count(" ") >= 1):
                # 粗提取:产物清单节里带路径样式的行(路径 + 用途)
                if any(ch in s for ch in ("/", "\\", ".md", ".py", ".json",
                                          ".txt", ".go", ".js")):
                    artifacts.append(s[:200])
    except Exception as exc:  # noqa: BLE001 提取失败不挡归档
        logger.warning("[archive] 影子产物提取失败 card=%s: %s", card_id, exc)

    # toolcard_final 是 JSON 列:report_done 写的是纯文本(非合法 JSON),
    # row_to_meta 解析失败会给 None——读原始列兜底,回执内容不丢。
    final_report = str(meta.get("toolcard_final") or "")
    if not final_report:
        def _raw_final(conn) -> str:
            row = card_db.get_card_row(conn, card_id)
            if row is None:
                return ""
            import json as _json
            raw = row["toolcard_final"]
            if raw is None:
                return ""
            try:
                v = _json.loads(raw)
                return str(v) if not isinstance(v, (dict, list)) else _json.dumps(v, ensure_ascii=False)
            except (TypeError, ValueError):
                return str(raw)          # 纯文本(实际形态)
        try:
            final_report = str(card_db.read_sync(work_id, _raw_final, "") or "")
        except Exception:  # noqa: BLE001
            pass
    body = {}
    try:
        body = (await card_store.read_card(work_id, card_id)).get("body") or {}
    except Exception:  # noqa: BLE001
        pass
    await coder_archive.archive(
        work_id, card_id,
        title=str(meta.get("title") or ""),
        parent=str(meta.get("parent") or ""),
        task=str(body.get("mission") or ""),
        final_report=final_report,
        artifacts=artifacts[:20])

    def _drop(conn) -> None:
        card_db.delete_card_row(conn, card_id)
        card_db.delete_shadow_row(conn, card_id)
        # 修改历史按卡号保留——它本来就是记录形态,不占卡面

    await card_db.run_write(work_id, _drop)
    logger.info("[archive] worker 归档 work=%s card=%s (产物 %d 条)",
                work_id, card_id, len(artifacts))
    return {"ok": True, "card_id": card_id, "artifacts": len(artifacts)}


async def unarchive_coder(work_id: str, card_id: str) -> dict[str, Any]:
    """归档记录复活成卡(重派活路径):cards 行重建(使命还原/状态 active)+档案删。

    会话延续(文件一直在);toolcard_final/goal 清零——重新派活从新任务起跑。
    """
    rec = coder_archive.get_record(work_id, card_id)
    if rec is None:
        return {"ok": False, "error": "not_archived",
                "message": "该卡号没有归档记录"}
    if card_store.card_exists(work_id, card_id):
        return {"ok": False, "error": "card_exists",
                "message": "活卡已存在,不需要解档"}
    body = f"## 使命\n{rec['task'] or rec['title']}"
    r = await card_store.create_card(
        work_id, rec["title"] or card_id, "系统派生",
        card_type="worker", parent=rec["parent"], card_id=card_id,
        charter=body, skip_kick=True)
    if "error" in r:
        return {"ok": False, "error": str(r["error"])[:200]}
    await coder_archive.remove(work_id, card_id)
    logger.info("[archive] worker 解档 work=%s card=%s", work_id, card_id)
    return {"ok": True, "card_id": card_id, "note": "已复活为活卡,可重新派活"}

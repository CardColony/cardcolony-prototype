# -*- coding: utf-8 -*-
import pytest, json
from pathlib import Path
from src.infra import cost_ledger as cl
from test_batch7_cost_accounting import _route_setup, _USAGE, _ledger_rows


@pytest.mark.asyncio
async def test_dbg(work, sandbox, monkeypatch):
    from outbound_probe import OutboundRecorder, sse, frame_text, frame_finish
    from src.api import model_service
    import httpx
    _route_setup(sandbox, monkeypatch)
    def respond(index, _body):
        if index == 0:
            raise httpx.ConnectError("断连")
        return sse(frame_text("好。"), frame_finish("stop"),
                   {"choices": [], "usage": _USAGE})
    OutboundRecorder(respond).install(monkeypatch)
    gw = model_service.get_gateway()
    monkeypatch.setattr(gw, "_retry_params", lambda m: (1, 0))
    await model_service.call_main(
        system_prompt="s", user_prompt="u",
        model_name="ds-v41-flash-eco-amr",
        fallbacks=["ds-v41-flash-eco-amr2"],
        work_id=work["work_id"], card_id=work["work_card_id"],
        ledger_task={"work_id": work["work_id"], "card_id": work["work_card_id"],
                     "pair_id": "pair-seat"})
    rows = _ledger_rows(sandbox)
    Path("D:/Temp/dbg_b7_rows.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    assert False

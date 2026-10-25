# -*- coding: utf-8 -*-
"""多轮思考回传（echo_reasoning）——2026-10-23 commandcode 模型「跑不通」的修复钉测。

背景（用户实测）：commandcode 的三个模型（cmd-ds-v41-flash / cmd-beam-free /
cmd-minimax-flash）在系统里带工具面调用时「跑不通」。逐层查下来的真因：

1. ``reflection/beam:free`` 的思考字段叫 **``reasoning``**（OpenRouter 系），
   不叫 DeepSeek 系的 ``reasoning_content``。适配器只认后者 ⇒ beam 的思考整段
   丢失 ⇒ 多轮时没有思考可回传。
2. 该端点**强制要求**：assistant 消息带 tool_calls 时，必须同时带回它自己上一轮
   给出的 ``reasoning_content``，否则 400::

       Missing required parameter: 'messages[1].reasoning_content'.
       This model needs the reasoning of every earlier assistant message:
       send back the 'reasoning_content' returned with that message.

   而 message_runner 的回传条件写的是 ``"deepseek" in model_used`` 字符串包含
   判断，beam 家族漏在外面 ⇒ 带工具面的第二轮起必 400，重试 3 次后整轮判死。
   （官方 DeepSeek API 一贯有此要求，故原判断对 deepseek 系是对的，只是不全。）

修法（两处，都不猜模型名）：
  · ``llm_openai_compat/translate.py``：思考字段双形态都认（reasoning_content /
    reasoning），先到先得、空串不覆盖。
  · ``models.yaml`` + ``model_config.reasoning_echo_fill()``：回传判据改为
    显式配置开关 ``echo_reasoning: true``，message_runner 读它、不再按名字猜。

零网络。
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from src.model_gateway.llm_openai_compat.translate import translate

# ---------------------------------------------------------------- 辅助


async def _payloads(*items):
    for item in items:
        yield item


async def _tr(*items):
    return [c async for c in translate(_payloads(*items))]


def _chunk(delta=None, finish=None):
    body: dict = {"choices": []}
    if delta is not None or finish is not None:
        body["choices"] = [{"delta": delta or {}, "finish_reason": finish}]
    return json.dumps(body)


DONE = "[DONE]"


# ---------------------------------------------------------------- 字段双形态


async def test_reasoning_field_openrouter_style_is_parsed():
    """beam:free 的思考字段叫 ``reasoning``——必须认，否则整段丢失。"""
    out = await _tr(_chunk(delta={"reasoning": "想一下"}), DONE)
    kinds = [c.type for c in out]
    assert kinds[0] == "block-start"
    assert out[0].block_type == "reasoning"
    assert out[1].type == "reasoning-delta"
    assert out[1].text == "想一下"


async def test_reasoning_content_field_deepseek_style_still_parsed():
    """DeepSeek 系的 ``reasoning_content`` 照旧认（不能修一个坏一个）。"""
    out = await _tr(_chunk(delta={"reasoning_content": "想一下"}), DONE)
    assert out[0].block_type == "reasoning"
    assert out[1].text == "想一下"


async def test_reasoning_content_wins_when_both_present():
    """两字段同帧时 reasoning_content 优先（DeepSeek 形态是主口径）。"""
    out = await _tr(_chunk(delta={"reasoning_content": "主", "reasoning": "备"}), DONE)
    assert out[1].text == "主"


async def test_empty_reasoning_field_does_not_open_block():
    """空串不开块（两种字段名都适用）。"""
    out = await _tr(_chunk(delta={"reasoning": "", "reasoning_content": ""}),
                    _chunk(delta={"content": "答"}), DONE)
    assert out[0].block_type == "text"


# ---------------------------------------------------------------- 回传判据


def test_no_name_substring_guessing_left():
    """回传判据不许再按模型名猜（改前是 ``"deepseek" in model_used``）。

    那条判断把 beam 家族漏在外面，是本次故障的直接原因。message_runner 的
    **代码里**（不含注释）不许再出现类似的字符串包含判断。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "api" / "message_runner.py").read_text(encoding="utf-8")
    code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
    assert '"deepseek" in' not in code, "message_runner 里还有按模型名猜的回传判断"
    assert "reasoning_echo_fill" in src, "必须走配置驱动的判定函数"


# ---------------------------------------------------------------- 配置登记


#: 期望登记 echo_reasoning 的模型集合（deepseek 全系 8 个 + beam 1 个）。
#: 改这个集合＝改「哪些端点强制要求回传思考」，必须是实测过的决定。
EXPECTED = {
    "ds-v4-flash-volc", "ds-flash-official",
    "ds-v41-flash-aliyun", "ds-v4-flash-aliyun",
    "deepseek-pro", "deepseek-v41-flash", "deepseek-v4-flash",
    "cmd-ds-v41-flash", "cmd-beam-free",
    # [2026-10-25 用户令] 方舟 coding 端点新增两席（同端点同纪律）
    "ds-v41-flash-eco-volc", "kimi-k31-pro-volc",
}


def test_models_yaml_registers_echo_reasoning_explicitly():
    """models.yaml 里显式登记了哪些模型要回传——不多不少。

    漏登记 ⇒ 该模型多轮必 400（本次故障）；多登记 ⇒ 往不要求的端点带额外字段，
    有被严格校验拒的风险。两边都是错，故钉死精确集合。
    """
    import yaml

    cfg = yaml.safe_load(
        io.open(Path(__file__).resolve().parents[1] / "config" / "models.yaml",
                encoding="utf-8").read())
    flagged = {k for k, m in cfg["models"].items() if m.get("echo_reasoning")}
    assert flagged == EXPECTED, (
        f"echo_reasoning 登记集合变了：多出 {sorted(flagged - EXPECTED)}，"
        f"缺少 {sorted(EXPECTED - flagged)}")


def test_echo_flag_survives_compilation():
    """开关要活到 gateway config 里——``_NON_GATEWAY_KEYS`` 别把它当界面字段弹掉。"""
    from src.api import model_config

    cfg = model_config._gateway_config()
    assert cfg["models"]["cmd-beam-free"].get("echo_reasoning") is True
    assert cfg["models"]["cmd-minimax-flash"].get("echo_reasoning") is None


# ---------------------------------------------------------------- 占位三态


@pytest.mark.parametrize("used,seat,want", [
    # beam：字段必须存在且非空（空串照样 400）
    ("reflection/beam:free", "cmd-beam-free", "(系统注入的消息，无模型思考记录)"),
    ("cmd-beam-free", "", "(系统注入的消息，无模型思考记录)"),
    # DeepSeek 系：字段必须存在，空串即可（官方 API 生产实测口径，逐字不变）
    ("deepseek/deepseek-v4.1-flash", "cmd-ds-v41-flash", ""),
    ("deepseek-flash", "ds-flash-official", ""),
    ("deepseek-v4.1-flash", "deepseek-v41-flash", ""),
    # 不要求的端点：None ⇒ 别加字段（多带字段有被严格端点拒的风险）
    ("minimax/minimax-m3.1-flash", "cmd-minimax-flash", None),
    ("glm-5.3", "glm-53", None),
    ("", "", None),
])
def test_reasoning_echo_fill_three_states(used, seat, want):
    """无思考可回传时补什么：beam 非空占位 / deepseek 空串 / 其他不加字段。"""
    from src.api import model_config

    assert model_config.reasoning_echo_fill(used, seat) == want


def test_ds_norm_placeholder_is_family_specific():
    """注入消息（判断协议）的占位按家族区分——不动 DeepSeek 的生产口径。"""
    from src.model_gateway import gateway as g
    from src.api import model_config

    cfg = model_config._gateway_config()["models"]
    tail = [{"role": "assistant", "content": "【判断协议】"}]

    # beam：非空（空串照样 400，实测）
    assert g.reasoning_echo_fill("cmd-beam-free", cfg["cmd-beam-free"]) != ""
    filled = g._ds_norm(tail, g.reasoning_echo_fill("cmd-beam-free", cfg["cmd-beam-free"]))
    assert filled[0]["reasoning_content"] != ""

    # DeepSeek 系：空串（官方 API 生产实测口径，逐字不变）
    assert g.reasoning_echo_fill("cmd-ds-v41-flash", cfg["cmd-ds-v41-flash"]) == ""
    filled_ds = g._ds_norm(tail, g.reasoning_echo_fill("cmd-ds-v41-flash", cfg["cmd-ds-v41-flash"]))
    assert filled_ds[0]["reasoning_content"] == ""

    # 不要求的端点：None ⇒ **调用方不走 _ds_norm**（不加字段）。
    # 注意「不加字段」是调用方的决定（gateway 两处 `if _fill is not None`），
    # _ds_norm 自身契约是「被调用就补」——默认补空串。
    assert g.reasoning_echo_fill("cmd-minimax-flash", cfg["cmd-minimax-flash"]) is None
    assert g._ds_norm(None) is None
    assert g._ds_norm([]) == []


def test_ds_norm_keeps_existing_reasoning_untouched():
    """已有 reasoning_content 的消息不被占位覆盖（回真的，不补假的）。"""
    from src.model_gateway import gateway as g

    msgs = [{"role": "assistant", "content": "x", "reasoning_content": "真的想过"}]
    assert g._ds_norm(msgs, "(系统注入的消息，无模型思考记录)") == msgs

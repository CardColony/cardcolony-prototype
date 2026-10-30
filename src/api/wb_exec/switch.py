# -*- coding: utf-8 -*-
"""**执行体总开关**（手动、运行时生效，不用重启）。

用户令（2026-10-23 16:20）：*"再做一个手动开关，开启走workbuddy，关闭走系统模型"*。

**为什么是运行时文件而不是配置项**：
``config/wb_exec.json`` 的 ``enabled`` 改完要重启 5700 才生效——
手动开关要的是**点一下就切**，所以另存一个
``config/wb_exec_switch.json``（与既有的 ``config/exec_mode.json`` 同一套路）：

.. code-block:: json

    {"enabled": true, "at": "2026-10-23T16:20:00+08:00"}

**优先级**：运行时开关文件 > ``config/wb_exec.json`` 的 ``enabled``。
**fail-open**：文件缺失/损坏 ⇒ 退回配置里的 ``enabled``（不崩）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("api.wb_exec.switch")

from .config import REPO_ROOT, load as load_config

#: 运行时开关文件。
SWITCH_PATH = Path(REPO_ROOT) / "config" / "wb_exec_switch.json"


def _read() -> Optional[bool]:
    try:
        data = json.loads(SWITCH_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 坏文件 ⇒ 退回配置
        logger.warning("[wb_exec] 开关文件读不了（退回配置）: %s", exc)
        return None
    if isinstance(data, dict) and "enabled" in data:
        return bool(data.get("enabled"))
    return None


def is_enabled() -> bool:
    """当前是否走 WorkBuddy 执行体。

    读**每次**都读文件（这样点一下就切，不用重启）。
    """
    v = _read()
    if v is not None:
        return v
    try:
        return bool(load_config().enabled)
    except Exception:  # noqa: BLE001
        return False


def set_enabled(enabled: bool) -> bool:
    """设置总开关（**写运行时文件**），返回写入后的值。"""
    payload = {"enabled": bool(enabled),
               "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        SWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SWITCH_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, SWITCH_PATH)
    except Exception as exc:  # noqa: BLE001 写不了就只读退回
        logger.error("[wb_exec] 写开关文件失败: %s", exc)
        return is_enabled()
    logger.info("[wb_exec] 执行体总开关 ⇒ %s", "开（走 WorkBuddy）" if enabled
                else "关（走系统自己的模型）")
    return bool(enabled)


def state() -> dict:
    """开关状态（给端点/UI 用）。"""
    v = _read()
    try:
        cfg_enabled = bool(load_config().enabled)
    except Exception:  # noqa: BLE001
        cfg_enabled = False
    return {
        "enabled": is_enabled(),
        "source": "runtime" if v is not None else "config",
        "config_enabled": cfg_enabled,
        "path": str(SWITCH_PATH),
    }

# -*- coding: utf-8 -*-
"""浏览器 CLI —— wb_exec 委派卡的执行体通道（纯标准库）。

用法（执行体 Bash 里直接调，JSON 进 JSON 出）::

    python3 -m src.infra.browser.cli manual                      # 上手手册（先读）
    python3 -m src.infra.browser.cli navigate --session S2 --input '{"url":"http://127.0.0.1:4173"}'
    python3 -m src.infra.browser.cli inspect  --session S2
    python3 -m src.infra.browser.cli click    --session S2 --input '{"ref":"e3.12"}'
    python3 -m src.infra.browser.cli close    --session S2

会话隔离：--session 名 ⇒ <root>/<name>/ 独立 Chrome profile（cookie/缓存
互不污染）。root 默认 ./_browser_sessions，可用 --root 或环境变量
CARDLO_BROWSER_ROOT 改。跨调用 Chrome 常驻，CLI 每次重连。

信封与平台工具同构：{ok:true,data:{...}} / {ok:false,error:{code,message}}。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 允许「python path/to/cli.py」直跑：把仓根插进 sys.path
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    __package__ = "src.infra.browser"

from . import session as _session  # noqa: E402
from . import chrome as _chrome  # noqa: E402

MANUAL = """\
# 浏览器操作手册（control-browser v1）

你有一个真实 headless Chrome。通过本 CLI 逐动作驱动，JSON 进 JSON 出。

## 基本循环
1. navigate {url}        —— 打开页面（http/https/file 才可）
2. inspect               —— 拿页面语义地图：可操作元素列表，每个带 ref（如 e2.7）
3. 用 ref 调动作          —— click/fill/type/scroll/…
4. 动作后需要确认页面状态 → 再 inspect 或 query

## 铁律
- ref 只在当前快照代次有效。页面导航/reload 后旧 ref 全部作废——重新 inspect。
- click 成功只证明「输入已派发」，不证明页面如你所愿——effect.verifificationRequired
  为 true 时，用 inspect/query 验证预期效果再下结论。
- 预期导航的点击用 click_and_wait_for_navigation（原子动作）；别拆成 click+wait。
- 一次一个动作，等结果再下一个（会话串行，并发会被 BROWSER_SESSION_BUSY 拒）。
- 动作失败/无效 → inspect 一次刷新目标再试一次；仍不行就停下来报告阻塞，
  别拿坐标乱猜、别死循环重试。
- fill 会清原值再写；type 追加输入（clear:true 可清）。密码框值永远被遮蔽。
- query kind=text 拿页面可见文字；kind=dom 拿 HTML；kind=console/network 拿诊断
  （页面报错、失败请求——排障先看这两个）。
- screenshot 落盘成证据文件（返回 path）。
- 完事 close 释放会话。

## 动作速查
navigate {url,timeout?}                打开页面
inspect {snapshotId?,offset?}          语义地图＋ref（分页续传带 snapshotId+offset）
query {kind,selector?,limit?}          text/dom/console/network/url
click {ref}                            点元素中心（真实鼠标事件）
double_click {ref}                     双击
click_and_wait_for_navigation {ref,timeout?}  点击并等导航
hover {ref}                            悬停（看弹层/title）
fill {ref,text}                        清值并填入（带 input/change 事件）
type {ref,text,clear?}                 聚焦后输入（真实输入管线）
press_key {key}                        Enter/Tab/Esc/方向键/单字符…
check {ref} / uncheck {ref}            勾选/取消（可验证 effect）
select_option {ref,value|label}        下拉选择
scroll {direction,amount?,ref?}        滚动（effect 报 moved/atStart/atEnd）
scroll_into_view {ref}                 把元素滚进视口
wait {ms} / wait_for_text {text,timeout?} / wait_for_selector {selector,timeout?}
wait_for_navigation {timeout?}
back / forward / reload {hard?}
new_tab {url} / tabs / switch_tab {targetId}
screenshot {fullPage?,path?}           截图落盘
status                                 会话状态
close                                  关会话（Chrome 退出）
manual                                 本手册
"""


def _envelope_ok(data: dict) -> dict:
    return {"ok": True, "data": data}


def _envelope_err(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(MANUAL)
        return 0
    action = argv.pop(0)
    if action == "manual":
        print(MANUAL)
        return 0
    if action == "install":
        # 下载官方 chrome-headless-shell（WSL/无原生浏览器环境的正解）
        dest = None
        if "--to" in argv:
            i = argv.index("--to")
            if i + 1 < len(argv):
                dest = Path(argv[i + 1])
        try:
            binary = _chrome.install_headless_shell(dest)
            print(json.dumps(_envelope_ok({"binary": str(binary)}),
                             ensure_ascii=False))
            return 0
        except Exception as exc:  # noqa: BLE001
            print(json.dumps(_envelope_err("INSTALL_FAILED", str(exc)),
                             ensure_ascii=False))
            return 1

    session_name = "default"
    root = os.environ.get("CARDLO_BROWSER_ROOT") or "_browser_sessions"
    input_json = "{}"
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--session", "-s") and i + 1 < len(argv):
            session_name = argv[i + 1]; i += 2
        elif a == "--root" and i + 1 < len(argv):
            root = argv[i + 1]; i += 2
        elif a == "--input" and i + 1 < len(argv):
            input_json = argv[i + 1]; i += 2
        elif a == "--input-file" and i + 1 < len(argv):
            input_json = Path(argv[i + 1]).read_text(encoding="utf-8"); i += 2
        else:
            i += 1
    try:
        inp = json.loads(input_json) if input_json.strip() else {}
        if not isinstance(inp, dict):
            raise ValueError("input 必须是 JSON 对象")
    except (ValueError, json.JSONDecodeError) as exc:
        print(json.dumps(_envelope_err("INVALID_INPUT",
                                       f"--input 不是合法 JSON: {exc}"),
                         ensure_ascii=False))
        return 1

    sess = _session.BrowserSession(root, session_name)
    try:
        data = sess.act(action, inp)
        print(json.dumps(_envelope_ok(data), ensure_ascii=False))
        return 0
    except _session.BrowserError as exc:
        print(json.dumps(_envelope_err(exc.code, exc.message),
                         ensure_ascii=False))
        return 1
    except _chrome.ChromeNotFoundError as exc:
        print(json.dumps(_envelope_err("CHROME_NOT_FOUND", str(exc)),
                         ensure_ascii=False))
        return 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps(_envelope_err("INTERNAL", f"{type(exc).__name__}: {exc}"),
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

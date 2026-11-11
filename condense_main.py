# -*- coding: utf-8 -*-
"""[凝练独立进程 2026-11-10 · 用户令「凝练应该独立一个runner」]

``python condense_main.py`` ⇒ ECO_ROLE=condense。全库唯一干凝练的进程：
扫盘器、排水循环、fold 管线全在这里——runner/web 永不凝（memory_store
内 _condense_home 闸门），跨进程重复凝由「只有一个进程能凝」根除
（runner0 扫盘与 runner1 回合末同凝 card_061 的实案）。

它不跑 FastAPI、不接卡域活：凝练只读会话文件、只写记忆表。
健康面：127.0.0.1:5710（ECO_CONDENSE_PORT）裸 200 探针——watchdog 照
runner 同款探活；pid 落 .server-condense.pid；stdout 落
logs/condense_stdout.log（start.bat 负责重定向）。
"""
import asyncio
import logging
import os
import sys

os.environ.setdefault("ECO_ROLE", "condense")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# 与 main.py 同款：CONDENSE_ENABLED、模型端点、密钥全在 .env——不装它，
# 凝练进程上线也是哑的（开关读不到 ⇒ 扫盘器不启）。
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception as _exc:  # noqa: BLE001 .env 装不上照跑（环境变量可在壳里）
    print(f"[startup] .env 加载失败（不阻断）: {_exc}", flush=True)

from src.infra.console_io import ensure_utf8_console  # noqa: E402
from src.infra.server_log import install_file_logging  # noqa: E402

ensure_utf8_console()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
try:
    from pathlib import Path
    install_file_logging(Path(ROOT))
except Exception as _exc:  # noqa: BLE001 日志装不上不阻断
    print(f"[startup] 日志落盘失败（不阻断）: {_exc}", flush=True)

logger = logging.getLogger("condense-main")

PID_FILE = os.path.join(ROOT, ".server-condense.pid")
HEALTH_PORT = int(os.environ.get("ECO_CONDENSE_PORT") or 5710)


def _write_pid() -> None:
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as exc:  # noqa: BLE001
        logger.warning("[startup] pid 文件写失败: %s", exc)


async def _handle_probe(reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
    """裸探针：watchdog 只需要一个「活着且能应」的信号，不供业务面。"""
    try:
        await reader.read(4096)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                     b"Connection: close\r\n\r\nok")
        await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _amain() -> None:
    _write_pid()
    # 先绑探针再起扫盘——扫盘首轮是同步重活（全库会话逐个量 token），
    # 事件环被它独占几十秒起步；先听口，watchdog 探活才不扑空。
    srv = await asyncio.start_server(_handle_probe, "127.0.0.1", HEALTH_PORT)
    logger.info("[startup] 健康探针 127.0.0.1:%d", HEALTH_PORT)
    from src.storage import memory_store as _mem
    task = _mem.start_condense_sweeper()
    if task is None:
        # 开关关（CONDENSE_ENABLED 未置位）：进程照活，探针照供——
        # 看门狗不该把「合法停摆」当死拉活。
        logger.warning("[startup] CONDENSE_ENABLED 未开——扫盘器不启，仅探针")
    else:
        logger.info("[startup] 凝练进程上线 role=%s pid=%d",
                    os.environ.get("ECO_ROLE"), os.getpid())
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass

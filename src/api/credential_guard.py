# -*- coding: utf-8 -*-
"""exec 类工具的凭证保护（2026-10-20 · 修 5713 已实测缺口）。

**缺口事实**（card_012 立项已取证）：`file_tools` 对 `read_file` / `search_files`
有凭证黑名单（`_credential_excluded`），但 `bash` / `python` 执行任意代码、
不经过那道判据 —— 一张有 `tools_file` 的卡用
`open(os.path.join(worktree, '.env')).read()` 就能把 `.env` 全文拿回会话。

**两道闸，分工不同**：

1. **静态闸（执行前）**`find_credential_refs`：扫命令/代码文本里的**文件名 token**，
   命中凭证黑名单就拒绝执行。它挡的是"直读"这种最自然的写法，代价是可能误伤
   （代码里出现 `.env` 三个字就拒），故只做**拒绝**、不做改写。
   它必然挡不住拼接/编码（`'.en'+'v'`、listdir 后拼路径）——这一层本来就不是
   安全边界，只是第一道便宜的门槛。

2. **值闸（执行后）**`redact_output`：从 BASE_DIR 下的凭证文件里采集**凭证值**
   （`.env` 的 KEY=VALUE 值、JSON/引号里的值、私钥长串等），把工具返回值里
   出现的这些值替换成 `***REDACTED***`。**这才是安全边界**：不论代码怎么绕
   （拼接路径、listdir、grep 扫描、把值写到别的文件再 cat），只要明文进了
   返回值，就必然与采集到的值逐字相等，必被替换。

**为什么选"静态拒 + 值脱敏"而不是"给 exec 单开一个默认关闭的开关"**：
后者等于把工具关掉——凡有 `tools_file` 的卡都靠 bash/python 干活（跑测试、
跑脚本、拉构建），默认关闭是把生产力换安全，代价不可接受；而值脱敏只在
"输出里真的出现凭证值"时才动手，正常用零影响。

**采集范围分两层（2026-10-21 覆盖修复，本模块最重要的一处语义）**：
上一版用「全盘共用一个 `_MAX_VALUES=400` 的个数上限」+ `os.walk(BASE_DIR)`，
而 `BASE_DIR=D:\AI_station`（station 根，含十几个互不相干的项目）⇒ 遍历从顶层
开始，扫到本项目之前就被 `.workbuddy` 的证书包吃满（实测那一个目录 12672 个
可采值、本项目 887 个），**本项目 `.env` 的 key 从未进过采集集**：真机实测
`redact_output` 命中 0 处、`open('.en'+'v').read()` 的明文原样回显。
现在改成按**来源分层**：`project` 层（项目根，先扫、预算最足、**不设个数上限**）
＋ `station` 层（BASE_DIR 其余目录，后扫、只扫浅层、共用一个补充预算）——
后者只能「加」值，不可能把前者的值挤掉。全局硬天花板只用来护住正则规模。

**已知残留**（写清楚，不当没看见）：模型若把值做**变换**再输出（反转、逐字符
加空格、base64、哈希），脱敏认不出；`exec` 类工具也挡不住"先复制到普通文件、
再用 read_file 读"的两步洗白（read_file 侧没有值闸）。两者都需要后续单独立项，
不在本次最小改动的范围内。

不碰 5700：本模块只读 `file_tools.BASE_DIR`（调用时现取，测试/沙箱改写即跟着走）。
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from pathlib import Path

from . import file_tools

# ── 静态闸 ────────────────────────────────────────────────────────────────

# 文件名 token：字母数字、下划线、点、连字符。路径分隔符不在集合内 ⇒ 路径天然
# 被切成段（`D:\x\.env` → `D` / `x` / `.env`），只对末段做黑名单判定。
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]{1,128}")


def find_credential_refs(text: str) -> list[str]:
    """命令/代码文本里出现的凭证文件名（去重保序）。空结果＝放行。

    判据复用 `file_tools._credential_excluded`（同一份黑名单，不另立一套）：
    名字、后缀、前缀三档逐字一致，避免两处判据漂移。
    """
    hits: list[str] = []
    seen: set[str] = set()
    for tok in _TOKEN_RE.findall(str(text or "")):
        cand = tok
        for sep in ("/", "\\"):
            if sep in cand:
                cand = cand.rsplit(sep, 1)[-1]
        if not cand or cand in seen:
            continue
        if file_tools._credential_excluded(Path(cand)):
            seen.add(cand)
            hits.append(cand)
    return hits


# ── 值闸 ──────────────────────────────────────────────────────────────────

REDACTED = "***REDACTED***"

# 采集参数：凭证文件本身不大；上限只为防"有人把 5GB 日志改名成 secrets.yaml"。
_MAX_FILE_BYTES = 512 * 1024

# ── 值上限的**语义**（2026-10-21 覆盖修复；旧值是 _MAX_VALUES = 400）─────────
# 旧实现：全盘共用一个「按个数封顶」的上限，而 os.walk 从 D:\AI_station 顶层
# 开始 ⇒ 扫到本项目之前就被 .workbuddy 的证书包吃满（实测 .workbuddy 一个目录
# 12672 个可采值、推演agent 887 个），项目自己的 .env 值从来没进过采集集——
# 真机实测 redact_output 命中 0 处、明文原样回显。
# 现在按**来源分层**：项目层先采、不设个数上限；其余目录共用一个小的补充预算。
_MAX_VALUES = 20000            # 硬天花板（只为护住正则规模；全盘实测 7546 个值）
_MAX_STATION_VALUES = 2000     # 非项目目录的补充预算（够覆盖 trading-workspace 等）
_MAX_FILES = 3000              # 单次采集最多读多少个凭证文件（护住 IO）
_CACHE_TTL_SECONDS = 60.0

# 扫描的时间预算（秒）。依据是 2026-10-21 真实布局实测：
#   D:\AI_station 全树 os.walk 26.5s（27 万个条目）｜本项目树 8.3s ｜
#   而 BFS 到 depth 4 只要 1.06s 就拿到了本项目全部 131 个凭证文件。
# 取值 1.5 + 0.5 ⇒ 冷启一次采集实测 2.06s（1183 个值，真 .env 的 35 个值全在内），
# TTL 60s 内命中缓存 0.0000s。放宽到 2.0 + 1.0 时冷启 3.11s / 3739 个值，但项目
# .env 的覆盖**完全一样**——多出来的时间是花在证书包噪声上，不值，故压到 1.5+0.5。
_PROJECT_SCAN_SECONDS = 1.5
_STATION_SCAN_SECONDS = 0.5

# 证书包后缀：单个文件能吐几十个长串，是「噪声」的主体；采的时候排在
# dotenv / json / netrc 这类「一行一个真凭证」的文件之后。
_BULK_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".jks")

_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.\-]*)\s*=\s*(.+?)\s*$")
_QUOTED_VALUE_RE = re.compile(r"""[:=]\s*["']([^"'\n]{8,})["']""")
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_\-]{28,}")
_SECRET_WORD_RE = re.compile(
    r"(?i)\b(?:password|passwd|token|secret|api[_\-]?key|access[_\-]?key)\b"
    r"\s*[:=]?\s+(\S{8,})")

# 值过滤：短值、明显占位、纯小写单词（INFO / production / true 这类配置词）不采，
# 否则「凭证脱敏」会退化成「输出乱码」——这是本实现最需要小心的误伤面。
_PLACEHOLDERS = {"changeme", "password", "secret", "example", "placeholder"}


def _worth_redacting(value: str) -> bool:
    v = value.strip().strip("'\"")
    if len(v) < 8 or len(v) > 4096:
        return False
    if v.lower() in _PLACEHOLDERS:
        return False
    if v.startswith("${") or v.startswith("<") or set(v) <= set("*-.x "):
        return False
    # 纯小写/纯大写字母的长词多是配置枚举值（production / INFO），不采；
    # 含数字、含符号、或大小写混排的才像真凭证。
    if v.isalpha() and (v.islower() or v.isupper()):
        return len(v) >= 24
    return True


def _harvest_text(text: str) -> set[str]:
    """从一份凭证文件的文本里挖出**值**（不是键、不是整行）。

    三个来源各有各的坑，逐个写清：
    · `.env` 行 `KEY=VALUE`：取 group(2)＝值。首版误取 group(1)＝键，于是把
      `DEEPSEEK_API_KEY` 这种键名当凭证值采集，真正的值反倒漏采（单测当场抓到）。
    · `.netrc` / yml 的 `password xxx`：取 group(1)（该正则无键组）。
    · 长串（私钥体、长 token）：`K=xxx` 这种会被整段匹配成 token，故带 `=` 时
      再补一个「取等号后一段」的候选——PEM 尾部的 `=` 填充不会因此丢候选
      （等号后为空就跳过）。
    """
    out: set[str] = set()
    for line in text.splitlines():
        m = _ENV_LINE_RE.match(line)
        if m:
            out.add(m.group(2))          # 值
            continue
        m = _SECRET_WORD_RE.search(line)
        if m:
            out.add(m.group(1))          # netrc / yml 风格的值
    for m in _QUOTED_VALUE_RE.finditer(text):
        out.add(m.group(1))
    for m in _LONG_TOKEN_RE.finditer(text):
        tok = m.group(0)
        out.add(tok)
        if "=" in tok:
            out.add(tok.split("=", 1)[1])   # `KEY=xxx` → `xxx`
    return {v.strip().strip("'\"") for v in out if _worth_redacting(v)}


def _harvest_file(path: Path) -> set[str]:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return set()
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return set()          # 二进制凭证（.p12/.jks）不采：内容也不会以文本进返回值
    if path.suffix.lower() == ".json":
        try:
            text = text + "\n" + json.dumps(json.loads(text), ensure_ascii=False)
        except (ValueError, TypeError):
            pass
    return _harvest_text(text)


def project_root() -> Path | None:
    """本项目根：本模块文件所在、且落在 BASE_DIR 之内的**最外层**目录。

    为什么从 ``__file__`` 推：``file_tools.BASE_DIR`` 指向 station 根
    （``D:\AI_station``，底下十几个互不相干的项目），它回答不了「哪个是本次运行的
    项目」；而代码自己在哪个项目里不需要猜——
    ``D:\AI_station\推演agent\.worktrees\5713\src\api\credential_guard.py``
    的最外层前缀就是 ``D:\AI_station\推演agent``。

    为什么不改 ``file_tools.BASE_DIR``：它是全仓十余处模块级路径根之一（文件工具、
    权限、快照、备份共用），改它波及面远大于收益（RELEASE_BLOCKERS B-03 /
    card_008 审计 H1）。采集范围只在本模块的消费端界定，不动别人的根。

    BASE_DIR 被测试/沙箱改写到别处（本模块文件不在其下）时返回 ``None``——
    调用方据此把 BASE_DIR 自身当项目层处理。
    """
    try:
        base = Path(file_tools.BASE_DIR).resolve()
        me = Path(__file__).resolve()
    except (OSError, RuntimeError):
        return None
    if me == base or base not in me.parents:
        return None
    outer = base
    for anc in me.parents:
        if anc == base:
            break
        outer = anc
    return outer if outer != base else None


def _sweep(root: Path, deadline: float) -> list[Path]:
    """广度优先（浅层优先）收集 root 下的凭证文件路径，直到时间预算用完。

    为什么 BFS + 时间预算，而不是 os.walk 全树（2026-10-21 实测）：
    · ``D:\AI_station`` 全树 os.walk 26.5s（27 万个条目），本项目树 8.3s；
    · 凭证文件却 98% 在浅层——本项目 131 个里 124 个是 depth 2 的
      ``.worktrees\<name>\.env``，BFS 到 depth 4 只用 1.06s 就全拿到。
    BFS 还顺带给出**结构性保证**：离根越近越先采，所以本项目根下的 ``.env``
    永远排在最早被采到的位置——时间预算再紧也挤不掉它。
    """
    prune = {d.lower() for d in file_tools.PRUNE_DIRS}
    out: list[Path] = []
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    while queue:
        d, depth = queue.popleft()
        if time.monotonic() > deadline:
            break
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower() not in prune:
                        queue.append((Path(e.path), depth + 1))
                elif file_tools._credential_excluded(Path(e.path)):
                    out.append(Path(e.path))
            except OSError:
                continue
    return out


def _bulk_last(paths: list[Path]) -> list[Path]:
    """层内排序：dotenv / json / netrc 类在前，证书包（*.pem 等）在后。

    稳定分区（不改各自的相对次序），故 BFS 的浅层优先在同类文件之间仍然成立。
    意义：万一补充预算先被用完，被挡在外面的是证书包，不是真正的 .env。
    """
    head = [p for p in paths if p.suffix.lower() not in _BULK_SUFFIXES]
    tail = [p for p in paths if p.suffix.lower() in _BULK_SUFFIXES]
    return head + tail


def _scan_tiers(base: Path) -> list[tuple[str, list[Path]]]:
    """按优先级返回 [(层名, 凭证文件路径列表), ...]。

    层 1 ``project``：项目根（推导不出时用 BASE_DIR 自身）——先扫、预算最足。
    层 2 ``station``：BASE_DIR 下其余的顶层目录——后扫、只给一分钟的零头。
    """
    proj = project_root()
    if proj is None:
        primary, extras = base, []
    else:
        primary = proj
        extras = []
        try:
            for e in os.scandir(base):
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                cand = Path(e.path)
                if cand == proj:
                    continue
                extras.append(cand)
        except OSError:
            extras = []

    tiers: list[tuple[str, list[Path]]] = [
        ("project", _bulk_last(_sweep(primary, time.monotonic() + _PROJECT_SCAN_SECONDS))),
    ]
    if extras:
        found: list[Path] = []
        deadline = time.monotonic() + _STATION_SCAN_SECONDS
        for d in extras:
            if time.monotonic() > deadline:
                break
            found.extend(_sweep(d, deadline))
        tiers.append(("station", _bulk_last(found)))
    return tiers


_cache: dict[str, object] = {"root": None, "expires": 0.0, "values": ()}
_pattern_cache: dict[str, object] = {"values": (), "re": None}


def secret_values() -> tuple[str, ...]:
    """采集本机凭证值（长值在前，便于整段替换）。

    **采集范围分两层——这就是 2026-10-21 的覆盖修复**：
    1. ``project`` 层（``project_root()``；推导不出项目根时用 BASE_DIR 自身）：
       先扫、扫得最久、**不设个数上限** ⇒ 本项目的 ``.env`` 值必进采集集；
    2. ``station`` 层：其余顶层目录，只扫浅层、共用一个补充预算 ⇒ 它们只可能
       「加」值，不可能把项目层的值挤掉。

    总数仍有 ``_MAX_VALUES`` 硬天花板护住正则规模。

    TTL 缓存：整套扫描实测秒级，凭证文件几乎不变——60 秒内复用同一份采集结果。
    """
    base = Path(file_tools.BASE_DIR)
    root_key = str(base)
    now = time.monotonic()
    if _cache["root"] == root_key and float(_cache["expires"]) > now:
        return _cache["values"]  # type: ignore[return-value]

    values: set[str] = set()
    files = 0
    try:
        for tier, paths in _scan_tiers(base):
            if not paths:
                continue
            before = len(values)
            cap = _MAX_VALUES if tier == "project" else _MAX_STATION_VALUES
            for p in paths:
                if files >= _MAX_FILES or len(values) >= _MAX_VALUES:
                    break
                if tier != "project" and len(values) - before >= cap:
                    break
                files += 1
                values |= _harvest_file(p)
    except OSError:
        pass      # 采集失败＝这一轮不脱敏（不阻断工具），下个 TTL 再试
    ordered = tuple(sorted(values, key=len, reverse=True))
    _cache.update(root=root_key, expires=now + _CACHE_TTL_SECONDS, values=ordered)
    return ordered


def _pattern_for(values: tuple[str, ...]):
    """把采集集编译成一条替换正则（按对象身份缓存——采集集是 TTL 级不可变元组）。"""
    if _pattern_cache.get("values") is not values:
        _pattern_cache.update(
            values=values,
            re=re.compile("|".join(re.escape(v) for v in values)))
    return _pattern_cache["re"]


def reset_cache() -> None:
    """清采集缓存（测试用；也供凭证轮换后立刻生效）。"""
    _cache.update(root=None, expires=0.0, values=())
    _pattern_cache.update(values=(), re=None)


def redact_output(text: str) -> tuple[str, int]:
    """把输出里的凭证值替换成 REDACTED，返回 (脱敏后文本, 命中处数)。"""
    if not text:
        return text, 0
    values = secret_values()
    if not values:
        return text, 0
    new, n = _pattern_for(values).subn(REDACTED, text)
    return new, n

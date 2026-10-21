# -*- coding: utf-8 -*-
"""卡片权限底座（重做 2026-10-12 · 任务书《权限底座重做》v2）。

**两级定位**：``project_class``（工牌，仅根卡承载、同树子卡继承）＋ ``card_type``
（卡类型）→ 一组权限开关。**权限表不在代码里**，在 ``config/permissions.json``。

[用户裁定 1-7 · 2026-10-12 逐条落地]

    1. 权限＝按卡片类型分配，两级定位（project_class ＋ card_type）。
    2. **类型不可变**：建卡定死；改 card_type / project_class 由 card_store
       存储层拒（本模块不参与——它只在**算怎么发权**时才被调到）。
    3. **可配置**：加类型＝改 permissions.json，本文件零改动。
    4. **固化 ＋ 版本刷新**：建卡时算一次写进卡的 ``perms``，同时记
       ``perms_version``（当时配置的 version）；**运行时只读卡上的 perms，
       不读配置文件**。改配置＝version+1 ＋ 跑 scripts/refresh_perms.py。
    5. **默认关门**：配置里没配到的组合＝空列表＝全拒。
    6. **开关可扩展**：开关词汇表＝本文件的常量集合 ``SWITCHES``；加开关＝
       代码加一个常量 ＋ 配置里用起来。配置里出现不在词汇表内的开关＝无效
       （丢弃并留 warning，不静默生效）。
    7. edit 本次保留（未来会收走）。

**树作用域（防越界）独立于类型分权**（方案 §4）：``check`` 先过类型分权，
再对 ``TREE_SWITCHES``（edit/delete/link/unlink/create_child）判「操作者是否在
目标上游」——独立函数 ``_is_in_subtree``，不含任何权限语义。

**R-C**：本模块只判权限，不设任何时间/长度/轮次闸。打点在唯一判定点，
两态同记（granted true/false），不做二次判定、不分叉。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from ..api import prompt_log

__all__ = [
    "SWITCHES",
    "TREE_SWITCHES",
    "SCOPE_SUBTREE",
    "SCOPE_LINKED",
    "SCOPE_ANY",
    "check_linked",
    "linked",
    "_linked_between",
    "PERMISSIONS_PATH",
    "PERM_KEY",
    "PERM_VERSION_KEY",
    "perms_for",
    "config_version",
    "compute_perms",
    "has_perm",
    "check",
    "_is_in_subtree",
    # [自动模式 2026-10-17 · 回灌 5700 块0.2]
    "AUTO_SWITCHES",
    "AUTO_PERMS_VERSION",
    "auto_perms",
]

logger = logging.getLogger("permissions.policy")


# ============================================================
# 开关词汇表（代码里定义的常量集合；加开关＝在此加一个常量）
# ============================================================

SWITCH_READ = "read"                    # 能读
SWITCH_EDIT = "edit"                    # 能改（未来会收走）
SWITCH_DELETE = "delete"                # 能删
SWITCH_CREATE_CHILD = "create_child"    # 能在它下面建子卡
SWITCH_LINK = "link"                    # 能连线
SWITCH_UNLINK = "unlink"                # 能解线
SWITCH_TOOLS_FILE = "tools_file"        # 能读写文件
# [角色只读核验 2026-10-21 · card_004] **只读文件开关**：给「只能看、不能改」的卡用。
# 为什么不能复用 tools_file：那一个开关管着 11 件工具——read_file／list_files／
# search_files／glob_files 之外，还有 **write_file／edit_file／delete_file／
# move_file／rename_file／bash／python**。把 tools_file 发给离树的职能角色，
# 等于同时给它写盘权和 shell 执行权，与「第二层只说话不动手」直接冲突。
# ⇒ 拆一个弱化开关，只解锁四件只读工具。
# 包含关系见 _SWITCH_IMPLIES：有 tools_file 者自动具备 tools_read_file，
# 故编程卡/根卡行为逐字不变、无需改配置；反向不成立（只读 ≠ 可写）。
SWITCH_TOOLS_READ_FILE = "tools_read_file"   # 只读文件/列目录/检索，不能写、不能执行
SWITCH_TOOLS_WEB = "tools_web"          # 能联网
SWITCH_SHADOW_WRITE = "shadow_write"    # 能写自己的影子卡
SWITCH_MANUSCRIPT = "manuscript"        # 能查阅本项目存档的原始资料（manuscript 三件）
SWITCH_INVOKE_TOOL = "invoke_tool"      # 能调用工具卡（run_tool_card）
SWITCH_PLAN = "plan"                    # 能建计划、改计划、推进步骤（思维链六件）

# [卡片底座第一步 · 沿关系的数据访问与通信 2026-10-14] 两个新开关——「沿关系」
# 这一个机制的两个方向：读（沿通信线读对方卡的正文/影子/对话）与写（沿关系发消息）。
# 与既有 read 分开授权：沿连线读**比直接读更敏感**（能读到有连线关系的对方卡数据），
# 故独立一码；发消息独立一码（沿关系发）。两码与 read 无包含关系，各判各的。
# 纪律同 [裁定 6]：加开关＝代码加常量＋配置里用起来；配置里未配到的组合＝空列表＝全拒。
SWITCH_READ_LINKED = "read_linked"          # 沿通信线读对方卡（read_via）
SWITCH_SEND_MESSAGE = "send_card_message"   # 沿关系向对方卡发消息

# [编程卡 2026-10-15 · C2 裁定] search 与 list_memory 回归为**预留机关**：
#   · SWITCH_SEARCH="search"——全系统仍**没有独立的 search 工具**，搜索能力由
#     web_search/fetch_web 承担（归 SWITCH_TOOLS_WEB）；在册只为给 coder 行与
#     后续拆分留位，**当前无任何工具消费**（配了不等于能用）。
#   · SWITCH_LIST_MEMORY="list_memory"——记忆开关预留位，待后续把 read_memory
#     从 SWITCH_READ 拆出时启用；**read_memory 仍挂 SWITCH_READ、不改挂**
#     （改挂＝未授权的 read 侧收窄，已随本裁定否决）。
#   沿革：2026-10-12 工具面补齐曾删死开关 search（当时同样无消费方）；
#   2026-10-15 随编程卡行（rd_project/coder 含 search/list_memory 两预留码）
#   按用户裁定恢复为预留机关。_filter_by_category 不因 search 新增过滤组。
SWITCH_SEARCH = "search"              # 预留：无独立工具，暂无消费方
SWITCH_LIST_MEMORY = "list_memory"    # 预留：暂无消费方，read_memory 仍挂 SWITCH_READ

#: 全部合法开关（配置里的值必须是它的子集，否则该开关无效）
SWITCHES: frozenset[str] = frozenset({
    SWITCH_READ,
    SWITCH_EDIT,
    SWITCH_DELETE,
    SWITCH_CREATE_CHILD,
    SWITCH_LINK,
    SWITCH_UNLINK,
    SWITCH_TOOLS_FILE,
    SWITCH_TOOLS_READ_FILE,
    SWITCH_TOOLS_WEB,
    SWITCH_SHADOW_WRITE,
    SWITCH_MANUSCRIPT,
    SWITCH_INVOKE_TOOL,
    SWITCH_PLAN,
    SWITCH_READ_LINKED,
    SWITCH_SEND_MESSAGE,
    SWITCH_SEARCH,
    SWITCH_LIST_MEMORY,
})

#: **开关包含关系**（2026-10-21 · card_004）：具备 key 的卡自动具备 value 里的全部开关。
#:
#: 只用于「宽权隐含窄权」这一个方向——有写权限的卡当然能读，
#: 但只读卡**不会**因此获得写权（方向单向，不可逆）。
#: 落地目的：给职能角色加 tools_read_file 后，编程卡/根卡不必改配置也不会掉能力。
_SWITCH_IMPLIES: dict[str, tuple[str, ...]] = {
    SWITCH_TOOLS_FILE: (SWITCH_TOOLS_READ_FILE,),
}


#: [5713 通道试走 · 用户口径 2026-10-20「卡间通信有专门的通信线」]
#: 树作用域档位——卡间通信的边界**是通信线，不是 parent 树**：
#:   · subtree（缺省，历史行为）＝只看自己子树，仅父子方向；
#:   · linked＝两卡之间已有通信线即通过（沿通信线通信、方向不限）；
#:   · any＝不判树作用域，只判类型授权（建线/解线用：平级卡可自主建线）。
SCOPE_SUBTREE = "subtree"
SCOPE_LINKED = "linked"
SCOPE_ANY = "any"

#: 树类开关：类型分权通过后追加「操作者是否在目标上游」限定（方案 §4）
TREE_SWITCHES: frozenset[str] = frozenset({
    SWITCH_EDIT,
    SWITCH_DELETE,
    SWITCH_LINK,
    SWITCH_UNLINK,
    SWITCH_CREATE_CHILD,
})


# ============================================================
# 配置读取
# ============================================================

#: 权限表路径（模块级常量 ⇒ 测试可 monkeypatch 隔离，与 card_store
#: TYPE_REGISTRY_PATH 同一套手法）
PERMISSIONS_PATH = Path(__file__).resolve().parents[2] / "config" / "permissions.json"

#: 卡面上的固化字段名（card_store 落盘/渲染与本模块读 perms 共用同一对名字，
#: 两处不得各写一套字符串字面量）
PERM_KEY = "perms"
PERM_VERSION_KEY = "perms_version"


def _load_config() -> dict[str, Any]:
    """读权限配置（每次现读，只被「算 perms」的路径调用）。

    读不到/坏 JSON ⇒ 空表 ⇒ 默认关门（不发任何权限）。
    """
    try:
        data = json.loads(PERMISSIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("权限配置不可用（默认关门）: %s: %s", PERMISSIONS_PATH, exc)
        return {}
    return data if isinstance(data, dict) else {}


def config_version() -> int:
    """当前权限配置的 version（写进卡的 perms_version）。配置不可用＝0。"""
    raw = _load_config().get("version", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def perms_for(project_class: str, card_type: str) -> list[str]:
    """按两级定位查权限开关列表。**默认关门：查不到＝空列表**。

    根卡查 ``permissions[project_class]["project"]``；子卡查
    ``permissions[project_class][card_type]``。

    返回值为 ``SWITCHES`` 的子集：不在词汇表内的配置值会被丢弃并留 warning
    （加开关＝代码加常量＋配置用起来；只改配置不生效是**有意**的）。
    """
    table = _load_config().get("permissions") or {}
    row = table.get(str(project_class or ""))
    if not isinstance(row, Mapping):
        return []
    raw = row.get(str(card_type or ""))
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        s = str(item)
        if s in SWITCHES:
            out.append(s)
        else:
            logger.warning(
                "权限配置含未知开关（已丢弃）: %s/%s → %r",
                project_class, card_type, item,
            )
    return out


def compute_perms(project_class: str, card_type: str) -> tuple[list[str], int]:
    """算出这张卡应有的 ``(perms, perms_version)``——**只在建卡/刷新时调用**。"""
    return perms_for(project_class, card_type), config_version()


# ============================================================
# 自动模式（2026-10-17 · 回灌 5700 块0.2）：权限**全开（TC1 裁案 A 的落地解释）
#
# 自动模式**不是**「按类型分权」的一种，是**绕过**它（迭代方案 §2.6）：开关打开时
# 该卡 perms 重算为全开集，子卡建卡时继承。``config/permissions.json`` 的
# 现有行**一个字不动**（红线 3）。
#
# ★ TC1 落地解释（任务书 §0.4 待裁项，建议方案 A「保守黑名单」）：
#   任务书黑名单三项 card_delete / file_write / rule_edit 与开关词汇表对不上——
#   词汇表里没有这三个码（实际对应关系：card_delete→delete、file_write→tools_file、
#   rule_edit→无对应开关）。按「不可逆」意图取：
#     · delete **排除**——删卡连带对话/影子全毁、无备份，真正不可逆；
#     · tools_file **不排除**——write_file 覆盖前自动备份到 _backups（半可逆），
#       且 tools_file 是文件家族总开关，排除会连坐 read_file/search_files（可逆）；
#     · rule_edit **无需排除**——规则修改走 draft_rule_edit（ROOT_ONLY_TOOLS）
#       ＋用户弹窗审批，流程上不可直达，本来就到不了「直接改规则」。
#   ⇒ AUTO_SWITCHES = SWITCHES - {delete}。若日后要连 file 写一并收窄，
#     改 AUTO_EXCLUDE_SWITCHES 一行即可（可配化的最小形态）。
#
# ★ 连带必改点：``scripts/refresh_perms.py`` 必须**跳过 auto_mode=1 的卡**——
#   否则任何人跑一次刷新，就会把全开权限按配置重写回类型分权集（权限无声消失，
#   红线 4）。
# ============================================================

#: 自动模式排除的开关（见上方 TC1 落地解释）。空集＝照搬全开（方案 C）。
AUTO_EXCLUDE_SWITCHES: frozenset[str] = frozenset({SWITCH_DELETE})

#: 自动模式「全开集」＝全部开关减排除项（U3：模型可操作所有功能）。
#: 用集合别名而非复制字面量——加开关时本集自动跟随（不留第二份清单）。
AUTO_SWITCHES: frozenset[str] = SWITCHES - AUTO_EXCLUDE_SWITCHES

#: 自动卡 ``perms_version`` 的**专用值**：与配置版本空间隔离（配置 version 恒 ≥ 0），
#: 一眼可辨「这张卡不是按类型分的权」。refresh_perms 见它/见 auto_mode 即跳过。
AUTO_PERMS_VERSION = -1


def auto_perms() -> tuple[list[str], int]:
    """算自动模式卡的 ``(perms, perms_version)``——**全开集 ＋ 专用版本值 -1**。

    排序只为输出稳定（``has_perm`` 读的是集合语义，顺序无关）。
    """
    return sorted(AUTO_SWITCHES), AUTO_PERMS_VERSION


# ============================================================
# 运行时判定（读卡上固化的 perms，不读配置）
# ============================================================

def has_perm(card_meta: Mapping[str, Any] | None, switch: str) -> bool:
    """这张卡是否被授予 ``switch``？

    读**卡上固化的** ``perms`` 列表（[裁定 4] 运行时照读、不现查配置）。

    False 的四种情形（全部＝默认关门，不区分、不回落）：
      - 卡不存在 / meta 为 None
      - 卡上没有 perms（从未固化过）
      - perms 里不含该开关
      - switch 不在 ``SWITCHES`` 词汇表内（不存在的开关谈不上授权）
    """
    if str(switch) not in SWITCHES:
        return False
    meta = card_meta or {}
    perms = meta.get(PERM_KEY)
    if not isinstance(perms, (list, tuple, set, frozenset)):
        return False
    granted = {str(s) for s in perms}
    if str(switch) in granted:
        return True
    # [2026-10-21] 单一方向隐含：宽权包含窄权（有 tools_file ⇒ 具备 tools_read_file）。
    # 反向不成立——只读卡不会因为这一条拿到写权。
    for g in granted:
        if str(switch) in _SWITCH_IMPLIES.get(g, ()):
            return True
    return False


def _deny_message(card_id: str, switch: str,
                  card_meta: Mapping[str, Any] | None) -> str:
    meta = card_meta or {}
    granted = meta.get(PERM_KEY)
    shown = ("|".join(sorted(str(s) for s in granted))
             if isinstance(granted, (list, tuple, set, frozenset)) else "NONE")
    return (
        f"[perm] denied card={card_id} action={switch} "
        f"card_type={meta.get('card_type', '')!r} perms={shown}"
    )


def check(actor_id: str, target_id: str, switch: str,
          cards: Mapping[str, dict[str, Any]] | None,
          *, scope: str = SCOPE_SUBTREE) -> bool:
    """统一权限关口：类型分权 →（树类开关）树作用域。通过 True，拒绝抛 PermissionError。

    ``cards`` 为卡面 meta 表（``{card_id: meta}``）。两级失败的文案各自独立
    （「分类不授权」/「越出子树」），便于模型分辨。

    ``scope``（仅对 ``TREE_SWITCHES`` 有效）＝树作用域的**档位**：
      - ``subtree``（缺省，行为逐字不变）：actor 须在 target 上游（只看自己子树）；
      - ``linked``：两卡之间已有**通信线**即通过（沿通信线，方向不限）——
        这正是「卡间通信走专门的通信线」的落地：边界是线，不是树；
      - ``any``：不判树作用域（建线/解线用——平级卡可自主建线，跨树也能建）。
    类型分权一级对三档一视同仁（放宽作用域不放宽授权）。

    打点：类型分权一条（fn="check"）、树作用域命中时再一条（fn="tree_scope"），
    两态同记（granted true/false）。
    """
    cards = cards or {}
    target = cards.get(target_id)
    allowed = has_perm(target, switch)
    prompt_log.append_perm_log({
        "fn": "check",
        "actor_id": actor_id,
        "target_id": target_id,
        "switch": switch,
        "granted": allowed,
    })
    if not allowed:
        raise PermissionError(_deny_message(target_id, switch, target))

    if switch in TREE_SWITCHES:
        if scope == SCOPE_LINKED:      # [5713] 沿通信线：有线即通，方向不限
            scope_ok = _linked_between(actor_id, target_id, cards)
        elif scope == SCOPE_ANY:       # [5713] 建线/解线：不判树作用域
            scope_ok = True
        else:
            scope_ok = _is_in_subtree(actor_id, target_id, cards)
        prompt_log.append_perm_log({
            "fn": "tree_scope",
            "actor_id": actor_id,
            "target_id": target_id,
            "switch": switch,
            "granted": scope_ok,
        })
        if not scope_ok:
            raise PermissionError(
                f"[perm] denied card={target_id} action={switch} "
                f"scope: actor={actor_id} 不在 target={target_id} 的子树上游"
            )
    return True


def _is_in_subtree(
    actor_id: str,
    target_id: str,
    cards: Mapping[str, dict[str, Any]] | None,
) -> bool:
    """target 是否落在 actor 的子树内（含 actor 自身）。

    与收口前 card_tools 写侧子树判据（原 L877，已删）逐字同口径——R3 等价
    验证 27,605 对 0 破例（docs/exploration/THINK-ECO-permgmt-R3-等价验证-
    20261009_0010.md）：自指直真；沿 target 的 parent 链上溯找 actor；
    visited 防环、悬空 parent 断链、guard<1000，全部同源。

    [重做 2026-10-12] 从权限开关里**拆出**，成为独立的树作用域函数——
    它不含任何类型分权语义；未来 edit 收走后，这一层可整层删除。
    """
    if target_id == actor_id:
        return True
    cards = cards or {}
    tgt = cards.get(target_id)
    if tgt is None:
        return False
    visited: set[str] = set()
    node = tgt.get("parent", "")
    guard = 0
    while node and guard < 1000:
        guard += 1
        if node in visited:
            break
        visited.add(node)
        if node == actor_id:
            return True
        nm = cards.get(node)
        if nm is None:
            break
        node = nm.get("parent", "")
    return False


def _linked_between(actor_id: str, target_id: str,
                    cards: Mapping[str, dict[str, Any]] | None) -> bool:
    """两卡之间是否已有**通信线**（links 边）——卡间通信的唯一边界判据。

    [5713 通道试走·用户口径「卡间通信有专门的通信线」] 通信的边界是线不是树：
    平级/兄弟卡之间只要建了线就能通，没有线的（哪怕同父同树）也不通。

    方向：**任一侧有 links 条目指向对方即成立**（已建立的边语义）。理由：一条线
    是双方的事——A 建线给 B 之后，B 的应答必须能沿同一条线回到 A，否则线只能
    单向喊话、对讲不成立。

    不参与通信的线不算：``relations``（依赖关联线）只记关系与内容，不在本判据内。
    自指不成立（自己不需要线）；缺卡即假。
    """
    if not actor_id or not target_id or actor_id == target_id:
        return False
    cards = cards or {}
    if actor_id not in cards or target_id not in cards:
        return False

    def _has_edge(src: str, dst: str) -> bool:
        meta = cards.get(src) or {}
        for rel in meta.get("links") or []:
            if isinstance(rel, dict) and str(rel.get("target") or "") == dst:
                return True
            if isinstance(rel, str) and rel == dst:   # 旧数据兼容：links 存纯字符串
                return True
        return False

    return _has_edge(actor_id, target_id) or _has_edge(target_id, actor_id)


def check_linked(actor_id: str, target_id: str,
                 cards: Mapping[str, dict[str, Any]] | None) -> bool:
    """通信线边界关口：卡间通信须**沿通信线**（两卡之间已有 links 边）。

    [5713 通道试走] 为什么不复用 ``check(..., scope=SCOPE_LINKED)``：树作用域
    那段只在 ``switch in TREE_SWITCHES`` 时才跑，而发信的开关是 read／
    send_card_message（都不在树类里）——硬塞 scope 会被整体跳过、等于没拦
    （5713 首版实测踩到：发信对任意卡都放行了）。故把线边界做成**独立关口**，
    判定只有一条：``_linked_between``。

    打点沿用 perm 口径（fn="check_linked"，switch="linked"，两态同记）。
    """
    ok = _linked_between(actor_id, target_id, cards)
    prompt_log.append_perm_log({
        "fn": "check_linked",
        "actor_id": actor_id,
        "target_id": target_id,
        "switch": "linked",
        "granted": ok,
    })
    if not ok:
        raise PermissionError(
            f"[perm] denied card={target_id} action=send_card_message "
            f"wire: {actor_id} 与 {target_id} 之间没有通信线")
    return True


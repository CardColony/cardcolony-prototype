# 卡片权限机制 · 使用公告

**适用范围**：产仓（推演agent）全部卡片操作，重点为研发项目根卡（rd_project）与子项目卡（sub_project）。

## 一、这套机制做什么

每张卡按自己的"分类"决定能做什么。分类由 `project_class`（根卡）或 `card_type`（子卡）决定，查表规则见 `src/permissions/policy.py`。

- 读、改、删、建子卡、连线、解连，都经同一个检查关口 `check` / `check_with_scope`；
- 树类操作（改、删、连、解、建子卡）额外限定在本卡子树内；
- 工具可见性按当前卡的分类过滤：没有文件权限的卡，模型看不到文件工具；没有联网权限的卡，看不到联网工具；
- 未注册的分类一律拒绝（默认关门）。

## 二、已生效的范围

- 代码位置：`src/permissions/policy.py`；工具层 `src/api/card_tools.py`（已接关口）；文件层 `src/api/file_tools.py`（已接 `check_file_access`）；提案口 `card_proposal.py`。
- 工具过滤唯一调用点：`src/api/message_runner.py` 调用 `build_tool_schemas(card_id, work_id, cards)`。
- 判定留痕：`logs/prompt_perm_audit.jsonl`，每次判定记一行。

## 三、使用时的变化

- **收紧**：连线、解线只能在自己子树内的卡之间进行；tool 分类卡不可被连线；未注册分类的卡读写均被拒。
- **放宽**：文本建卡通道的 parent 校验由"子树内"改为"parent 分类授权建子卡"。拒绝文案仍写"子树"，待定。
- **恢复**：凭证文件排除对模型文件工具重新生效。

## 四、已知未完成项（使用者须知）

1. **文件访问范围未收紧**：`FILE_SCOPE` 仍为全放行占位，文件层目前等同无锁。卡片权限不受影响，文件锁另行配置真实前缀后生效。
2. **无卡上下文时工具全部放出**：若某次调用未携带当前卡 ID，`_filter_by_category` 会返回全量工具面，与"默认关门"原则相反。当前唯一调用方总带卡 ID，尚未触发；修复为"无卡上下文则工具面为空"，待确认后改。
3. **SHADOW_WRITE 为五类额外授权**：偏离原方案，待确认保留或撤回。
4. **CARD_PROPOSAL、SEARCH、TOOLS_CARD 三位无接线点**：目前是摆设，不影响已接线的判定。
5. **研发树内工具无区分度**：rd_project 与 sub_project 授予的工具组相同，过滤对研发树等于未过滤。

## 五、反馈

使用中发现的拒绝误伤或放行异常，记录卡 ID、操作、时间，并查 `logs/prompt_perm_audit.jsonl` 对应行，提交到 card_040。

---
*本公告不替代方案正文（card_040）。版本：首版。*

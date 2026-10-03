<!-- session: card_001, created: 2026-10-03T12:13:19.170300+08:00 -->
---MSG---
role: user
timestamp: 2026-10-03T12:14:04.795418+08:00
---
我们一起来完善和构建，你现在所处的这个agent项目， 你能看到自己项目的情况吗？
---MSG---
role: assistant
timestamp: 2026-10-03T12:17:01.103471+08:00
model: deepseek-v4.1-flash
---
能看到，而且看的方式有点特别——我读自己的代码，用的是我自己工具清单里的 `read_file`，它允许的范围正好是 `D:\AI_station\trading-workspace` 和 `D:\AI_station\推演agent`。所以我确实在"照镜子"，但镜子的边框也是这个项目自己定义的。

先把确认到的事实摆出来。

## 一、我是什么

`推演agent` —— 一套**卡片画布系统**。FastAPI 起在 5700 端口，前面挂一层静态前端（剧本架 + 画布 + 卡片工作区），后面是本地文件存储（每张卡一个 `.md`，YAML front matter + 四区正文），每次写入都走 git 自动提交。

当前这个对话，挂在 `works/work_002/` 的剧本「项目管理agent」的根卡 `card_001` 上——**这张根卡正文是空的**。也就是说，这个剧本就是为"完善和构建这个项目本身"而建的，而它现在只有一张空壳。

旁边还有个 `work_001`「股权财政」，4 张卡，`session_001.md` 已经 155KB。那是一个真实用过的剧本。

## 二、代码地图

```
main.py               启停、git 探测、写 pid
src/api/              routes(路由+对话主链路) · card_tools(工具+权限)
                      card_proposal(建卡协议解析) · simple_service(三席并发+统合)
                      model_service(网关封装) · context_assembler(中性上下文)
                      prompts(系统说明) · file_tools · search_tool
                      text_tool_rescue(工具调用抢救) · prompt_log · progress
src/storage/          card_store · session_store · canvas_store · work_store
                      manuscript_store · git_auto_commit · hash_utils
src/infra/            config · logging · errors · json_utils · text_utils · trace_recorder
src/model_gateway/    gateway.py（35KB，标注"禁止修改"）
static/               canvas.js(33KB) · workspace.js(22KB) · style.css(22KB) · api.js · shelf.js
logs/                 29 份 prompt 日志，最大 176KB
```

一轮对话的实际流程：读卡 → 取历史 → 追加用户消息 → 组装中性上下文 → **三思考席并发出观点（无工具、无指令）** → **统合官带工具产出唯一回复** → 抢救文本形态的工具调用 → 执行工具 → 解析 `card-proposal` 建卡 → 落 prompt 日志 → 存 session。

模型席位：思考席 `MiniMax-M3 / qwen3.8-max / deepseek-v4-pro`（三家独立厂商），统合官 `deepseek-v4.1-flash`。

## 三、这套东西的设计哲学（我认为是全项目最值钱的部分）

从代码注释里能读出几条被反复执行的裁定：

- **零内容预设**：系统提示只讲"结构 + 工具 + 建卡格式"，不塞角色、不塞方法论、不塞示例。连方法论库都被主动停用了，理由是"自我繁殖污染链"。
- **拉取不推送**：卡片正文一律不随上下文下发，要读自己 `read_card`。注释原话是"杜绝上轮模型写入的卡片内容变成下轮常设输入的回流"。
- **无闸**：工具循环不封顶，`max_tokens`/`timeout`/熔断全删，理由是"闸就是空观点的根因"。
- **可审计**：prompt 日志 + git 双通道，每一轮喂了什么原文、调了谁、出了什么工具事件，全部可复盘。

这套原则比大多数 agent 项目清醒得多。

## 四、我看到的欠账

**明确缺陷**

1. **没有测试体系。** `requirements.txt` 里挂着 `pytest / pytest-asyncio / httpx`，但 `tests/` 目录不存在。只有根目录两个临时脚本 `_t_filetools_e2e.py`、`_t_filetools_regression.py`，不在任何框架里，跑没跑、过没过，无从判断。
2. **上下文无上限。** 每轮把全部历史塞进 `prior_messages`，没有截断、没有摘要、没有预算。`work_001` 的会话已经 155KB，再涨下去会顶穿窗口，成本是线性起的。而项目又刚删掉了所有 `max_tokens` 闸——闸删了，替代方案没建。
3. **`logs/` 无限增长。** 29 个文件、最大 176KB，无轮转、无归档、无清理。

**血统残留（死代码）**

4. `src/infra/config.py` —— 它要读 `settings.yaml / roles.yaml / pipeline.yaml`，而 `config/` 目录下只有 `models.yaml`。**全项目零引用**，一旦被调用立刻 FileNotFoundError。旧的 think-core-agent 血统。
5. `src/infra/trace_recorder.py` —— **零引用**，同为残留。
6. `methodology/` 空目录、`init_methodology()` / `init_canvas()` 空转函数、`derive_ending` 已断链、`work_store.update_work` 里那条不会再触发的路径、`_trash_recovery_error/` 杂物目录。

**文档与代码打架**

7. `card_store.py` 开头写着"**全局铁律：任何代码路径都不允许删除 cards\ 或 sessions\ 下的文件**"，而同一个文件里 `delete_card()` 正在 `unlink()` 这两个目录。
8. `routes.py` 的 DELETE 端点注释写着"仅前端调用；模型工具清单不含删卡"，但 `card_tools` 里 `delete_card` 已经注册进模型工具表，`BASE_PROMPT` 里也写了。注释是旧裁定的化石。

**结构性风险（不是 bug，是没想完的地方）**

9. `progress.py` 是进程内存态——换多 worker 就失效。
10. `can_read` 里那条"锚卡全局可读"依赖"锚定"关系 + `card_type=="剧本"`，但目标卡自动派生已被废除，这条路径现在实际只活在 `work_001` 的存量卡上。
11. `text_tool_rescue` 是在**应用层**抢救模型原生标记泄漏，根因其实在网关层（没把特殊 token 归一成标准 `tool_calls`）——而网关标着"禁止修改"。这是把补丁打在错误的地方，只是没人有权限去拆。

## 五、我想先问你的事

我能看到全貌了，但**这个项目最不缺点子，最缺的是"谁定优先级"**——从历史看（`work_001` 里你明确说过"卡片是推演的结果，不是过程"，还批评过我式的前置产出），你在这件事上很克制。

所以我先不动手，给你四个切入口：

- **A｜清账**：删死代码（`config.py`/`trace_recorder.py`/杂物目录），修三处文档与代码的矛盾。半天量，纯减法，风险低。
- **B｜补地基**：建 `tests/`，把 `card_proposal`、`can_read/can_edit`、`text_tool_rescue`、`file_tools` 边界这些纯函数先纳入回归。这是这个项目最大的空白。
- **C｜补上下文管理**：闸删了，就得给个"不减信息的压缩"或"分层保留"方案——否则长对话必然撞墙。
- **D｜就先把 `work_002` 这张空根卡填上**：用三张卡承载"项目地图 / 欠账清单 / 完善路线"，之后所有项目动作都在这棵树里挂，而不是散在对话里。

我建议的顺序是 **A → D → B → C**：先清噪音，再把项目管理真的搬到卡上，再补测试，最后碰最需要设计的上下文。

要不要我先把 D 做掉（把这份现状诊断落成 3 张卡，挂在 `card_001` 下）？你点头我就建。


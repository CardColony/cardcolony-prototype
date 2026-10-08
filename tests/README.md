# tests/ · 步骤一回归网（card_012）

为步骤二（数据／运行时改造）与步骤三（边界与交付）搭的回归安全网。
**只新增测试，不改被测代码。**

## 运行

```
cd D:\AI_station\推演agent
python -m pytest
```

离线可跑、**不需要任何 API key**、全程零外网：
不设 `MOCK_LLM` 的用例走真 `MockGateway`（`gateway.py:598`），
端到端用例把网关换成替身（`test_offline_e2e.py`）。

依赖已在 `requirements.txt:9-11` 声明（pytest / pytest-asyncio / httpx），无需新增。

## 文件

| 文件 | 覆盖 |
|---|---|
| `conftest.py` | 隔离层（两条铁律）＋ `work` 夹具（空剧本） |
| `test_card_tree.py` | 不变量一：卡片 CRUD ＋ **权限由树机械算出** |
| `test_snapshot_roundtrip.py` | 不变量二：快照 → 恢复往返一致 |
| `test_file_boundaries.py` | 不变量三：文件工具四道机械兜底 |
| `test_proposal_parse.py` | 解析面：建卡协议双形态 ＋ 观点清洗 |
| `test_offline_e2e.py` | 端到端：HTTP → 上下文组装 → 工具循环 → 落卡 |

## 两条铁律

1. **不碰真实数据目录。** `works/`、快照根、`logs/`、`file_tools` 的允许范围与
   备份目录，全部在 autouse fixture 里重定向进 pytest `tmp_path`。手法与既有
   离线预检先例 `scripts/preflight_shadow_offline.py:30-40` 同族。
2. **不 import `main`。** `main.py:83-85` 的 `startup_init()` 在**导入期**执行：
   建 `logs/`、`works/`，跑 `init_git()`（`git add -A` ＋ commit，要求机器有 git
   二进制），写 `.server.pid`。端到端用例自行用 `routes.router` 装配 app，
   经 `httpx.ASGITransport` 走真实 HTTP 链路。

## 故意不覆盖（避免范围膨胀）

真实网关的重试／降级／429（要真 key 与真网络）、`search_tool`（外网）、
前端 UI 与 `static/api.js`、文件锁超时（423）与并发竞态、快照保留策略的时间维度。
以上属步骤二／步骤三，另立卡。

## 首轮实测状态

**未实跑。** 本会话只有文件读写工具，没有执行工具 → 无法运行 `pytest`，
本网的正确性尚未经一次真实运行验证。**请执行上面那条命令确认全绿**；
若首跑报错，最可能的落点是各模块的路径常量改写点（`conftest.sandbox`）。

## 建网时测出的问题（只记录，不在本步修）

1. `routes._run_message`：`tool_call_id` 每轮从 `call_0` 重新编号，多轮工具调用时
   id 会重复——真实 API 下同一会话内重复 id 会被拒（400）。mock／替身不校验，故首轮没暴露。
2. `data_snapshot.snapshot_sync` 返回的 `dir` 字段是 `SNAPSHOT_ROOT/<stamp>`，
   而真实批次落在 `SNAPSHOT_ROOT/<work_id>/<stamp>`——字段与真实路径不一致。
3. `restore_from_snapshot` 的默认 `target = BASE_DIR / rel_path`，`rel_path`
   必须自带 `works/<work_id>/` 前缀才落得到原处，否则会写出一个平行的 `works/` 树。
4. `file_tools.BASE_DIR / ALLOWED_ROOTS / BACKUP_DIR` 是模块级常量（其中 `BASE_DIR`
   是硬编码绝对路径 `D:\AI_station`）→ 测试只能 monkeypatch。步骤二改成注入后，
   `conftest.sandbox` 可相应简化，断言不动。
5. 快照是**全量拷贝**（每次写卡都复制整个 `works/` 下全部文件）——步骤二的增量与加锁项。
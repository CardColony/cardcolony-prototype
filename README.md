# 推演agent

一个**卡片式多模型推演系统**：以卡片为最小工作单元，用多个独立模型做分析、一个统合模型收敛成结论，
所有结论、证据链、调用记录全部落盘可回溯。用于把一件复杂的事（如「股权财政」走向剧本）沿因果链
一层层推透、留痕、可翻案。

## 它解决什么

复杂项目的失败通常不在单轮能力，而在过程中**丢掉的结论、复活的错误、被吞掉的判断**。
本系统用四条机制防这些：

1. **结论区是卡片间唯一接口** —— 卡内推理过程留在各自的独立对话里，只有沉淀后的结论跨卡流动，
   历史结论不淹没过程、被否定的方向不会复活。
2. **权限由卡片树机械决定** —— 读侧全盘可读（存在即放行），写侧严格限自己子树；
   **建卡只能建自己的子卡片**（parent 须落在本卡子树内，缺省＝本卡自身）——
   越权由代码机械拒绝，与 edit/delete 同一判据。
3. **全量留痕可对账** —— 每次模型调用的输入输出全文落 `logs/`、每次卡片改动入 git、
   每次工具调用记 `tool_events`；「说了做」与「真做了」可三方对账定案。
4. **机械兜底接住模型缺陷** —— 工具调用被模型以文本形态泄漏（已知五种形态）、日志/解析异常等，
   一律在代码层接住，不依赖模型自觉。

## 能力面

| 层 | 内容 |
|---|---|
| 卡片树 | 建卡／编辑正文／建线／解线／删卡（用户侧按钮＋模型侧工具，护栏：根卡与有子卡者拒删） |
| 推演链 | 三思考席（MiniMax-M3 / qwen3.8-max / deepseek-v4-pro）并行分析 → 统合官（deepseek-v4.1-flash）收敛成唯一结论；三席无工具权限 |
| 工具层（18 件） | 卡片：create_card／read_card／read_memory／edit_card／delete_card／read_shadow／update_shadow／link_cards／unlink_cards／list_manuscript／read_manuscript／search_manuscript；联网：web_search／fetch_web；**本地文件：list_files／read_file／search_files／write_file** |
| 本地文件能力 | 边界＝`D:\AI_station` 下的 `trading-workspace` 与 `推演agent` 两个目录；凭证类文件（`.env`/`*.pem`/`*.key`/`credentials.json`/`id_rsa` 等精确名单）不可读、不可写、不可搜索；四道兜底：写前强制备份原文到 `_backups/`、二进制拒写、单次写入上限 5MB、依赖目录（.venv/site-packages/__pycache__ 等）剪枝 |
| 文本泄漏抢救 | 模型把工具调用写进正文（DeepSeek 系特殊标记泄漏）时，机械解析并真实执行；解析不出则回注「未识别未执行」告知模型 |
| 实时反馈 | 发消息后可见：三席分析进度／统合推理／工具循环轮次／建卡，逐阶段上报 |

## 起停

```bat
start.bat          :: 启动服务（后端 5700）
stop.bat           :: 停止服务
```

**重启（改代码后装载）只走 `restart_safe.py`** —— 它是唯一许可的重启方式：先判定「有无调用在跑」
（progress 活跃态＋连接信号复核），有则拒载，绝不杀掉正在执行的调用。

```bat
python restart_safe.py --check   :: 只判定，不重启
python restart_safe.py           :: 判定通过后重启
```

访问：**http://127.0.0.1:5700/**

## 数据在哪

| 路径 | 内容 |
|---|---|
| `works/<work_id>/cards/*.md` | 卡片本体（Markdown ＋ front matter） |
| `works/<work_id>/sessions/*.md` | 每张卡背后独立对话的完整记录 |
| `works/<work_id>/canvas.json` | 画布布局与连线 |
| `logs/prompt_*.json` | **每次模型调用的输入输出全文**（system_prompt／user_prompt／response／tool_events／error） |
| `_backups/<原路径>/<时间戳>/` | 文件工具覆盖写之前的原文备份（不依赖 git 的兜底） |
| `methodology/` | 方法论文档 |

## 版本线

```bat
git log --oneline          :: 全部提交（卡片增删改由 auto-commit 自动提交，带 [auto][work_xxx] 前缀）
git tag                    :: 里程碑
git checkout <commit> -- <file>   :: 取回任一历史版本
python restore_card.py     :: 把某张卡恢复到 git 任一历史版本
```

## 目录结构

```
main.py                 服务入口（FastAPI，端口 5700）
src/api/                路由、提示词、工具层、上下文组装、解析器、进度上报
  ├ routes.py           全部 HTTP 端点（prefix=/api）
  ├ card_tools.py       卡片工具 schema 与执行分发
  ├ file_tools.py       本地文件工具（四件＋边界＋四道兜底）
  ├ text_tool_rescue.py 文本形态工具调用抢救器
  ├ card_proposal.py    建卡提案解析（多形态）
  ├ context_assembler.py 卡片上下文组装（卡片清单＋当前卡）
  └ simple_service.py   三席分析＋统合
src/model_gateway/      模型网关（多端点、重试、日志）
src/storage/            卡片/剧本/会话/画布存储、git 自动提交
config/models.yaml      模型席位配置（改模型只改这里）
```

## 边界与已知限制

- **它慢**：一轮真实推演 1–8 分钟（三席并行＋统合＋工具循环），适合「把事想透」，不适合「快点干完」。
- **推演质量＝模型的分析质量**：卡片、权限、留痕只保证错误不扩散、可回溯，不保证推理本身正确。
- **文件能力不设删除**：模型没有删文件工具（删除不可逆）；不需要的文件由人或系统外清理。
- **模型自述不是状态依据**：任何「已完成 X」的声称，以 `logs/`、磁盘、git 三方对账为准。

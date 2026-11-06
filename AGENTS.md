# 项目规则

## 服务重启必须请示

**禁止未经用户明确同意就重启/杀死 5700 服务进程**（main.py / .server.pid）。
改完代码需要重启生效时：**报告改动＋问一句"要不要现在重启"**，等用户说重启再动。
- 服务是活的：在跑的卡、在途模型回合、断点续跳都会被打断。
- 前端静态文件（static/ 下 JS/CSS/HTML）不用重启，让用户刷新即可——
  只有后端代码（src/、config/）变更才需要重启。
- 用户多次强调过这条，不得"顺手"重启验证。

## 验证方式

- 测试：`PYTHONPATH=tests python -m pytest tests/xxx.py -q`
- 语法：`python -m py_compile <file>` / `node --check <js>`
- 验证通过 ≠ 可以重启；重启永远是请示项。

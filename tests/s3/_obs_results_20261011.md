# S3 DOM/网络观察记录（mock 载体）· 2026-10-11

> 执行约束：沿用用户令「永远不要拿真实项目做测试」——**全部 DOM/网络观察在纯 mock 载体完成**
> （tests/s2/mock_server.mjs，内存假数据、一次性进程、端口 18790，观察结束已停止）。
> 零 works 写入（施工窗 works 树的变更全部来自生产实弹活动：work_011 建 21:56、work_002
> session 22:17——与 S3 观察无交集，S3 足迹仅 static/templates/tests）。
> 观察工具：tests/s3/obs_nav_s3.js（MutationObserver＋fetch 全通道计数＋判据步骤 runner），
> 载体浏览器＝ZCode IAB（1680×1100），注入式 evaluate 驱动。
> 观察结论是 mock 载体行为；**真实页面上的最终视觉与手感仍需用户打开页面确认**。

## 判据结果（main 28/28 ＋ main3 2/2 ＋ deep 11/11 全绿）

| 判据 | 结果 | 证据（mock 载体实测） |
|---|---|---|
| V3-A1 点击判型（R-20） | ✅ | 点项目行→URL 不变＋work_m2 卡行展开；再点→折叠清零；点有子卡行→URL 不变＋a1 可见性翻转→回翻；点叶子→hash=#work_m1/card_m_b＋对话区切卡（wsHeadTitle 非空） |
| V3-L5 唯一高亮（R-34/26） | ✅ | qsa('.nav-card.current') 恰 1（card_m_b）；当前项目行 .current 次强在场；deep 页复核同（恰 1，card_m3_l4） |
| V3-F2 搜索独立层（R-24） | ✅ | 输入「假卡」→结果层出现、含卡标题＋项目名（跨项目命中 假卡乙-X/假项目乙/假卡丙-L4）；**navTree mutation=0**（树零触碰）；点同项目结果→切卡＋结果层关 |
| V3-F3 搜索零网络（R-24） | ✅ | 搜索窗 children=0、navFull=0、version=0（other 通道=环境 60s 定时器 badge/probe，与搜索无关，数值如实记录 0~3 浮动） |
| V3-A3 幽灵卡灰态（R-21） | ✅ | /__test/ghostcard 注册 card_gone（快照永不返回、getCard 应答）；注入幽灵行点击→**.deleted 灰态＋URL 不变**；hash 指向幽灵卡再点击→**选中清空（hash 摘除）＋面包屑回父卡**（假项目甲›假卡甲-A，bypassDepth 生效） |
| V3-A5 收起全部（R-28） | ✅ | 展开非路径 work_m2→点「收起全部」→work_m2 卡行收起；**当前卡祖先链 a→a1 保留展开**；prefs.expanded[work_m1] 仅剩路径条目 {card_m_a:"manual"}（source 标记保留＝S4 判据数据未破坏）；prefs.projects=[work_m1] |
| V3-A6 多项目并看（R-29） | ✅ | work_m1＋work_m2 并存展开；reload 后两组行集原样（prefs 按 wid 独立）；**发现并修复 S2 缺口**：refresh 只灌当前 work 根段——跨项目展开态 reload 后「▾ 无行」，workspace.js 新增 restoreOpenSegments() 零网络从快照补灌后通过 |
| V3-L2 面包屑（R-27） | ✅ | 深树 l4 打开→面包屑 5 段（假项目丙›L1›L2›L3›L4）；点父段 L3→hash=#work_m3/card_m3_l3＋卡打开；浅树（work_m1，depth≤1）面包屑槽 display:none 不占空间 |
| V3-L3 吸顶（R-18） | ✅ | 收容器（maxHeight 260px）逼出滚动条：scroll 后 .nav-crumb.sticky＋position:absolute 悬浮；回顶→回文档流槽位（static）；同一 DOM（无第二套 UI）。※IAB 伪象：程序化 scrollTop 赋值不触发 scroll 事件，验证经显式 dispatch（监听器＋阈值数学由该路径证实）；真实滚轮滚动需用户确认 |
| V3-L6 定位三件套（R-26） | ✅ | 先展开非路径 work_m1（3 行）→点「定位」→非路径收起（3→0）；祖先链 l1..l4 全展开；滚到当前卡可视区（inView=true，scrollTop>0）；current 恰 1 |
| R-31 引导线（视觉＋几何） | ✅ | mock 补 depth4 卡（card_m3_l5）：行挂 .nav-guide＋backgroundImage=repeating-linear-gradient（computed）＋backgroundSize=16px（=depth4×档距4）＋paddingLeft=66px（10+4×14 封顶，不再随深吃宽）；点该行 URL 不变（判型正确）；截图留证 |
| R-33 徽章 | ✅ | 项目行聚合（N卡＋相对时间）；card_m_a（branch_active 1h 前）行内「活跃」徽章在场（截图留证）；左栏渲染路径无类型色点类（grep data-type=0） |
| R-35 非正常态 | ✅（S2 既有＋S3 空态） | 失败「加载失败·点此重试」→loadChildren 重新调用（S2 既有接线在场）；空项目「空·去新建」由 patchProjectContent 按 isProjectEmpty 渲染（data-add 复用建卡委托）；加载转圈 S2 既有 |

## 观察中发现并修复的两个实现缺口

1. **跨项目展开态 reload 后丢失（S2 缺口，S3 修复）**：S2 refresh() 首帧只对当前 work 灌根段
   （snapshotRootsWithOrphans(workId)），其余 prefs 里「开着」的项目 reload 后只有 ▸/▾ 却无子行
   （childrenStore 是内存态）。S3 在 workspace.js renderNav 后加 restoreOpenSegments()：
   开着的项目根段＋已展开分支段凡缺失一律从 /api/nav 全量快照补灌（**零网络**，孤儿契约与
   S2 同口径），R-29「切走再回原样」由此闭合。
2. **观察套件起跑态踩 rail 空树**：裸加载落画布态，.nav-tree display:none → 虚拟列表
   clientHeight=0 → 零行挂载（S2 记忆中「行数 0 现象」的复现与定性——生产行为正常，
   悬停浮层/进对话态时 ResizeObserver 重切出行）。观察套件改为带 hash 加载进对话态。

## 如实记录的边界（未验/伪象，需用户确认）

- IAB 对程序化 scrollTop 赋值不触发 scroll 事件（log 空、scrollTop 实际已变）——吸顶联动
  在 mock 上经显式 dispatch 验证；真实滚轮/触摸滚动的吸顶手感需用户在真浏览器确认（V3-L3 口径）。
- mock getCard 返回通用名「假卡」（不含真实标题）——「切卡成功」类断言以 hash 复合键为准。
- 搜索结果层与最近访问的视觉密度/手感、面包屑槽对 260px 栏宽的占用感——截图静态形态正常，
  动态手感需用户确认。
- 环境 60s 定时器（badge/probe）会偶发进入观察窗的其他通道计数（与搜索无关，已按通道拆分记录）。

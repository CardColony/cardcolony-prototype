# S2 DOM/网络观察记录(mock 载体)· 2026-10-11

> 执行约束:用户中途令「永远不要拿真实项目做测试」——**全部 DOM/网络观察在纯 mock 载体完成**
> (tests/s2/mock_server.mjs,内存假数据、一次性进程、端口 18777→18778→18779→18780,已全部停止)。
> 曾短暂打开过 5700 页面(只读、未点击、localStorage 零写入、验证后立即关闭),此后零接触生产。
> 观察工具:tests/s2/obs_nav_dom.js(MutationObserver + fetch 计数)。
> 观察结论是 mock 载体行为;**真实页面上的最终视觉与行为仍需用户打开页面确认**。

## 判据结果

| 判据 | 结果 | 证据(mock 载体实测) |
|---|---|---|
| V-V5 默认折叠(R-23) | ✅ | hash=`#work_m1/card_m_a1`(复合键);cards=[a,a1,b]=当前项目根层直系+祖先链(a)system 展开;work_m2 仅项目行;prefs 落盘 `{"expanded":{"work_m1":{"card_m_a":"system"}},"projects":["work_m1"],"autoOrganize":true}` |
| V-R2 折叠即卸载(R-14) | ✅ | 折叠 card_m_a:removes=[card_m_a1]、其余行零记录、零网络 |
| V-R1 重展增量挂载(R-13) | ✅ | 重展:adds=[card_m_a1]、children 恰 1 次(折叠即丢重拉) |
| V-R2 重展一致性 | ✅ | 行集一致;top 几何 a@30px/a1@68px/b@106px(前缀和正确;DOM 顺序≠视觉顺序是 absolute 定位正常形态) |
| V-V4 网络计数(R-22) | ✅ | 折叠→展开→折叠:children=1;快速双击(展开+折叠):仍 1(去重);叶节点 twist.none=true、children 不增;点行进对话链路正常 |
| V-V6 无变化路径 | ✅ | 静置 65s:version 请求 2 次、/api/nav 全量 0 次、DOM mutation 0 条(零开销坐实) |
| V-V6/V-R3 变化路径 | ✅* | retitle+指纹变→65s 内 probe:version 1 次、全量重拉恰 1 次、变化 work 行 ctitle 更新至最新值、无整树重建(adds/removes≠全行数)。*轻微瑕疵见下 |
| V-R4 悬停跳过(R-19) | 机制已闭 | S2 无 hover 置位者(属 S3 交互族,R-F 不夹带),真机无自然触发路径;机制由 tests/s2/virtual.test.js R-19a/b/c 单测闭合 |
| V-R3 服务端改标题真机 | ✅* | 同 V-V6 变化路径(mock 控制端点改标题);仅变化行被 patch。*见瑕疵 |

## 观察中发现并修复的两个实现缺陷

1. **折叠祖先级联丢段后,重展时子孙 system 段无人补拉**(▾ 却无子行)→ nav_render.js 增
   `ensureExpandedSegments()`:展开动作完成后对「有展开条目且段缺失且非 loading/error」的
   节点惰性补拉(仍守 R-22 展开才拉)。
2. **mock 指纹截断缺陷(非生产缺陷)**:mock_server 指纹取 JSON 前 16 字符,恒为
   `{"card_id":"car`,标题变化不进指纹 → probe 恒判「无变化」。改全长 sha256(生产
   /api/nav/version 的 by_work 是 64 位全长 sha256,实测无此问题)。

## 如实记录的轻微瑕疵(未修,需用户确认是否接受)

- 变化路径 probe 后,未变化 work 的项目行被「卸载+重挂」一次(remove+add 各 1,终态正确、
  无视觉差异)。疑似 ResizeObserver 在 render 后补触发一次 reslice 所致。未变化 work 的行
  内容字段无任何修改(title/pname/ptime 均零 mutation),仅元素壳重建一次。
- 第一轮观察(5700 页面,用户令之前)曾发现行数 0 的现象未及排查即被用户令中止;mock 载体
  上同场景(视口正常)未复现,疑与当时 IAB 视口/加载时序有关。真实页面首开请用户留意首帧。

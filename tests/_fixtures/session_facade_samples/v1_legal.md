---MSG---
role: user
timestamp: 2026-10-01T10:00:00+08:00
pair_id: aaa111
---
你好
---MSG---
role: assistant
timestamp: 2026-10-01T10:00:05+08:00
model: m1
pair_id: aaa111
---
答复一
---MSG---
role: user
timestamp: 2026-10-01T10:01:00+08:00
pair_id: bbb222
---
含分隔符正文：
\---MSG---
上面那行是转义的
---MSG---
role: assistant
timestamp: 2026-10-01T10:01:10+08:00
model: m1
---
答二无pair

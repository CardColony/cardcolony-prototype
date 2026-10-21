# -*- coding: utf-8 -*-
"""把 75 条 known-red 逐条归族（三类红判据的机械落地）。输入是本轮两轮整套跑的 junit 与 7cdc6ac 基线清单。
用法: python _c16_classify.py   -> 打印分族计数并写 _c16_classes.json（固定顺序，可 diff）"""
import json, os, re, collections
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, '..', '..', '..'))   # .worktrees/5713
red = json.load(open(os.path.join(HERE, '_c16_full1_red.json'), encoding='utf-8'))
m = json.load(open(os.path.join(BASE, '_flake_ev', 'matrix5.json'), encoding='utf-8'))
sr = sorted(m['stable_red'])

def cat(nid):
    f, msg = nid.split('::')[0], red.get(nid, {}).get('msg', '')
    if 'GOAL_TOOL_DRIVER_REQUIRED' in msg:            return '1-契约迁移·直红'
    if 'await_children' in msg and 'schema' in msg:   return '1-契约迁移·契约面'
    if 'goal tools require' in msg:                   return '1-契约迁移·下游'
    if f in ('tests.test_auto_await_explicit', 'tests.test_auto_await_resume',
             'tests.test_auto_await_livelock'):       return '1-契约迁移·下游'
    if f == 'tests.test_auto_mode' and ("'active' == 'complete'" in msg or '没跑起来' in msg or '互斥' in msg):
                                                      return '1-契约迁移·驱动'
    if re.search(r"t2[0-9]-[0-9]", msg):              return '2-常量·提示词版本'
    if 'glm-' in msg or 'mock:' in msg:               return '2-常量·模型名'
    if 'parallel' in msg or 'serial' in msg:          return '2-常量·执行模式配置'
    if 'model_override' in f or re.search(r"'m2' == 'm1'", msg):
                                                      return '3-语义滞后·模型优先级'
    if 'human_stop' in msg:                           return '4-真坏·human_stop'
    if 'snapshot' in f:                               return '4-真坏·snapshot_restore'
    if '字号' in msg:                                  return '4-真坏·CSS字号'
    if re.search(r'assert \d+ == \d+', msg) or 'Extra items' in msg \
       or 'AUTO_PERMS' in msg or '工具清单' in msg or '工具面' in msg:
                                                      return '2-常量·计数/集合'
    return '2-常量·逐字文案'

out = {n: cat(n) for n in sr}
cnt = collections.Counter(out.values())
for k, v in sorted(cnt.items()):
    print(f'{v:3d}  {k}')
print('total', sum(cnt.values()))
json.dump(out, open(os.path.join(HERE, '_c16_classes.json'), 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1, sort_keys=True)

/* ═══════════════════════════════════════════════════════════════
   data.js — 数据层（照 v3 定版 prototype-canvas-page-v3.html 移植）
   15 卡三层示意数据 / 归档 1 / 通信线 3 / 全局模型配置
   字段口径与 canvas-page-interaction-spec-v1.md §12 一致
   ═══════════════════════════════════════════════════════════════ */

const STATE_LABEL = { run:'运行中', queued:'排队中', done:'已完成', blocked:'受阻', paused:'已暂停' };

const KINDS = {
  master : { n:'Master Agent Card', s:'Master', ic:'crown',   struct:1 },
  manager: { n:'Manager Agent Card',s:'Manager',ic:'branch',  struct:1 },
  unit   : { n:'Unit Agent Card',   s:'Unit',   ic:'cube',    struct:1 },
  worker : { n:'worker 卡',         s:'worker', ic:'gear',    struct:0 },
  qa     : { n:'QA 卡',             s:'QA',     ic:'shield',  struct:0 },
  pm     : { n:'PM 卡',             s:'PM',     ic:'pen',     struct:0 },
  ui     : { n:'UI 卡',             s:'UI',     ic:'monitor', struct:0 },
};
const KORDER = ['master','manager','unit','worker','qa','pm','ui'];

/* 模型清单＝端点报什么名显示什么名（原样透传，前端不配第二套简称） */
const MODELS = ['claude-opus-4-5','claude-sonnet-4-5','gpt-5.2','gemini-3-pro','deepseek-v4'];
const modelName = id => String(id||'');
const GMODEL = { master:'claude-opus-4-5', manager:'claude-opus-4-5', unit:'claude-sonnet-4-5',
                 worker:'claude-sonnet-4-5', qa:'claude-sonnet-4-5', pm:'claude-sonnet-4-5', ui:'gpt-5.2' };

const NODES = {
  root:{ title:'底层重构', kind:'master', code:'card_001', state:'run', parent:null,
    facts:[['轮次',14],['阶段','收口'],['接口',5],['受阻',1]],
    meta:'更新 10:31 · 6 张子卡',
    last:{ who:'card_001', text:'画布页交互定稿，走视觉化之前先看信息与交互。', time:'10:31' } },
  prod:{ title:'产品与交互需求', kind:'manager', code:'card_074', state:'run', parent:'root',
    facts:[['阶段',2],['子卡',3],['轮次',5]],
    meta:'更新 10:24 · 交互语言定型',
    last:{ who:'card_074', text:'交互语言按信息分层写，不做视觉装饰。', time:'10:24' } },
  pmDoc:{ title:'画布页需求文档', kind:'pm', code:'card_346', state:'done', parent:'prod',
    facts:[['条目',8],['评审',2]],
    meta:'收口 10:18 · 8 条判据',
    last:{ who:'card_346', text:'8 条判据带验法写入正稿，评审两轮通过。', time:'10:18' } },
  arch:{ title:'页面架构及 UI/UE', kind:'manager', code:'card_257', state:'run', parent:'root',
    facts:[['阶段',3],['子卡',3],['轮次',7]],
    meta:'更新 10:40 · 返工中',
    last:{ who:'card_257', text:'7 条缺陷已逐条修，手势与出口各自闭环。', time:'10:40' } },
  qa:{ title:'验收与证据基线', kind:'manager', code:'card_091', state:'done', parent:'root',
    facts:[['轮次',12],['通过',12],['证据',4]],
    meta:'收口 09:12 · 12/12 通过',
    last:{ who:'card_091', text:'证据包已归档，验收结果可按条复核。', time:'09:12' } },
  ext:{ title:'外部接入与联调', kind:'manager', code:'card_118', state:'blocked', parent:'root',
    facts:[['接口',3],['等谁','外方'],['卡点',2]],
    meta:'停住 08:50 · 受阻待处理',
    last:{ who:'card_118', text:'三方签名未回，自动推进停下等人接手。', time:'08:50' },
    reason:{ code:'BLK-02', text:'三方签字未回，自动推进停下等人接手' } },
  protoA:{ title:'高保真原型 · 画布页', kind:'ui', code:'card_322', state:'run', parent:'prod',
    facts:[['版本','v2'],['缺陷',4],['轮次',6]],
    meta:'更新 10:41 · React 形态重写中',
    last:{ who:'card_322', text:'四个缺陷修完，正在跑全功能自检。', time:'10:41' },
    live:'正在写的这段是卡片页的悬停摘要改成长文自适应，流式输出时只占一行加光标……' },
  protoB:{ title:'高保真原型 · 对话页', kind:'ui', code:'card_319', state:'paused', parent:'prod',
    facts:[['版本','v1'],['轮次',2]],
    meta:'停住 09:58 · 等 token 就绪',
    last:{ who:'card_319', text:'先画对话页骨架，令牌落地再细化。', time:'09:58' } },
  tokens:{ title:'设计令牌与变量', kind:'worker', code:'card_305', state:'done', parent:'prod',
    facts:[['令牌',33],['组件',11]],
    meta:'收口 09:40 · 已定稿',
    last:{ who:'card_305', text:'深浅两套令牌已对齐，组件覆盖 11 项。', time:'09:40' } },
  spec:{ title:'画布页交互规格', kind:'unit', code:'card_290', state:'done', parent:'arch',
    facts:[['条目',32],['版本','v3']],
    meta:'收口 09:20 · 已定稿',
    last:{ who:'card_290', text:'动作与覆盖层口径都收敛了，只剩密度参数。', time:'09:20' } },
  lib:{ title:'组件与状态库', kind:'unit', code:'card_291', state:'run', parent:'arch',
    facts:[['组件',18],['缺',2]],
    meta:'更新 10:05 · 补状态壳',
    last:{ who:'card_291', text:'按钮与输入框骨架收完，还缺空态壳。', time:'10:05' } },
  base:{ title:'绘图底座与几何', kind:'unit', code:'card_132', state:'paused', parent:'arch',
    facts:[['函数',2],['轮次',6]],
    meta:'停住 08:31 · 等上游',
    last:{ who:'card_132', text:'布局与缩放要一起改，等上游接口定稿。', time:'08:31' } },
  evid:{ title:'验收证据包', kind:'worker', code:'card_096', state:'done', parent:'qa',
    facts:[['条数',12],['证据',12]],
    meta:'收口 09:12 · 12/12',
    last:{ who:'card_096', text:'每条都留原始输出，可按条独立复核。', time:'09:12' } },
  qaRun:{ title:'画布页 v2 验收', kind:'qa', code:'card_345', state:'run', parent:'qa',
    facts:[['条目',9],['过',7],['差',2]],
    meta:'更新 10:52 · 逐条复验中',
    last:{ who:'card_345', text:'9 条判据过了 7 条，剩两条等人工点验。', time:'10:52' } },
  trace:{ title:'交互轨迹留档', kind:'worker', code:'card_331', state:'paused', parent:'spec',
    facts:[['录制',4],['缺',1]],
    meta:'停住 10:48 · 等重录',
    last:{ who:'card_331', text:'合成事件录不全，有一条要人工补一次。', time:'10:48' } },
};
/* type 派生：只由 kind 决定，不在数据里各写一遍 */
Object.keys(NODES).forEach(k => { NODES[k].type = KINDS[NODES[k].kind].s; if(!NODES[k].model) NODES[k].model = GMODEL[NODES[k].kind]; });
Object.keys(NODES).forEach((k,i) => { if(NODES[k].ord==null) NODES[k].ord = i; });

/* 归档卡：不进画布、不占槽位；收成父卡旁计数入口 */
const ARCHIVED = [{ code:'card_021', title:'构建与发布', kind:'manager', parent:'root',
  at:'11-03 14:20', reason:'ARCH-04', why:'发布线并入「页面架构及 UI/UE」，本卡留存过程' }];

/* 通信线：一次真实消息传递；ago 秒前；演示窗口 12 秒 */
const COMM_TRAVEL = 0.62, COMM_HOLD = 1.35, COMM_FADE = 0.85;
const PULSE_LIFE = COMM_TRAVEL + COMM_HOLD + COMM_FADE;
const COMMS = [
  { a:'prod',  b:'ext',  ago:0.3, n:1, txt:'card_074 → card_118：依赖清单补齐三项，请重排期' },
  { a:'arch',  b:'qa',   ago:1.1, n:1, txt:'card_257 → card_091：三条交互口径已提升，请按新判据复核' },
  { a:'protoA',b:'protoB',ago:26, n:1, txt:'card_322 → card_319：组件表定稿，可对齐' },
];

/* 依赖关联线（第三类线，v3 无示例；两维可区分：紫点线＋两端菱形） */
const DEPS = [
  { a:'protoA', b:'tokens', kind:'依赖', detail:'组件全态取自已定稿令牌' },
  { a:'qaRun',  b:'evid',   kind:'依赖', detail:'验收判据对照证据包' },
];

/* 规则卡（右缘「规则卡」浮窗内容） */
const RULES = [
  { id:'R-01', t:'覆盖层不锁主视野',   d:'浮窗开着时主画布仍可拖拽缩放；只有输入型短层可短暂接管且带可见取消。' },
  { id:'R-02', t:'每个状态有鼠标出口', d:'任何可进入状态至少一个鼠标可达出口；Esc 只是加速键，不是唯一出口。' },
  { id:'R-03', t:'不设空转中间态',     d:'不出现「既不能做 A 也不能做 B」的无动作状态。' },
  { id:'R-28', t:'通信线流星语义',     d:'线＝刚有信息过去那一瞬，不是常驻拓扑；过期即消失是如实的。' },
];

/* 事件流（右缘「事件」浮窗） */
let evSeq = 0;
const EVENTS = [
  { id:++evSeq, k:'arch',  m:'「构建与发布」并入页面架构线，卡片已归档',       who:'card_001', time:'15:24' },
  { id:++evSeq, k:'state', m:'「高保真原型 · 画布页」回到运行中，进入 v2 重写', who:'card_322', time:'15:20' },
  { id:++evSeq, k:'new',   m:'「设计令牌与变量」建卡，挂在产品与交互需求下',   who:'card_074', time:'14:58' },
  { id:++evSeq, k:'new',   m:'「画布页交互规格」建卡，挂在页面架构下',         who:'card_257', time:'14:52' },
];
const EVK = { arch:'归档', state:'状态', new:'建卡', model:'模型', say:'回复' };

/* 对话（B3 所在浮窗） */
const CHATS = {
  protoA: { title:'高保真原型 · 画布页', code:'card_322', msgs:[
    { who:'card_322', me:false, text:'收到四条缺陷，正在 React 版里逐个修。' },
    { who:'card_257', me:true,  text:'修复后每个功能跑一遍再交付。' },
  ]},
};

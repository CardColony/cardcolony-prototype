/**
 * 契约形态的画布快照（§4.5 canvas_view）。
 * 用途：无后端时的本地样本；字段与 cardcolony-data CanvasView 逐字段对齐，
 *      接入真数据后仅把该对象替换为 GET /projects/{id}/canvas 的 payload。
 *
 * 六态覆盖（FLOW_STATE 全量）：目标进行中 / 模型自声明本轮达成 / 待验收 /
 *   阻塞或暂停 / 修正中 / 上级验收通过 —— 各至少一处。
 * 生命周期覆盖：active / archived（archived 必带 lifecycle_reason_code 且 is_operable=false）。
 */
(function (root) {
  'use strict';

  function node(o) {
    return {
      card_id: o.id,
      title: o.title,
      card_type: o.type,
      card_state: o.state || 'active',
      entity_lifecycle: o.life || 'active',
      task_summary: o.flow ? {
        flow_state: o.flow,
        display_word: { primary_word: o.word || o.flow, qualifiers: o.quals || [] },
        budget_state: o.budget || '充足'
      } : null,
      run_summary: o.run ? {
        run_id: o.run,
        run_state: o.runState,
        activity: o.activity
      } : null,
      position: { x: o.x, y: o.y },
      parent_id: o.parent || null,
      lifecycle_reason_code: o.life === 'archived' ? (o.reason || null) : null,
      is_operable: o.life === 'archived' || o.life === 'deleted' || o.life === 'migrated' ? false : true
    };
  }

  var N = [
    // ── 根 ────────────────────────────────────────────────
    node({
      id: 'card_001', title: '底层重构', type: 'project', x: 620, y: 60,
      flow: '目标进行中', word: '推进中', x_: 0,
      run: 'run_001', runState: 'waiting_model', activity: 'running'
    }),
    // ── 第二层 ────────────────────────────────────────────
    node({
      id: 'card_074', title: '产品与交互需求 · 总负责人', type: 'sub_project', parent: 'card_001',
      x: 320, y: 240, flow: '目标进行中', word: '推进中',
      run: 'run_014', runState: 'waiting_tool', activity: 'running'
    }),
    node({
      id: 'card_109', title: '后端与数据契约', type: 'sub_project', parent: 'card_001',
      x: 620, y: 240, flow: '待验收', word: '待验收'
    }),
    node({
      id: 'card_150', title: '工程与发布', type: 'sub_project', parent: 'card_001',
      x: 920, y: 240, flow: '阻塞或暂停', word: '受阻',
      quals: ['EXTERNAL_CONDITION']
    }),
    node({
      id: 'card_163', title: '验收与证据基线', type: 'sub_project', parent: 'card_001',
      x: 1220, y: 240, flow: '修正中', word: '修正中'
    }),
    // ── 第三层：card_074 之下 ─────────────────────────────
    node({
      id: 'card_257', title: '功能线 1 · 页面架构及 UI/UE', type: 'sub_project', parent: 'card_074',
      x: 180, y: 420, flow: '目标进行中', word: '推进中'
    }),
    node({
      id: 'card_390', title: '交互规格与判据', type: 'topic', parent: 'card_074',
      x: 400, y: 420, flow: '上级验收通过', word: '已通过',
      state: 'completed'
    }),
    node({
      id: 'card_430', title: '组件与状态库', type: 'topic', parent: 'card_074',
      x: 620, y: 420, flow: '模型自声明本轮达成', word: '待复核'
    }),
    // ── 第三层：card_109 之下 ─────────────────────────────
    node({
      id: 'card_287', title: '数据层 cardcolony-data', type: 'tool', parent: 'card_109',
      x: 840, y: 420, flow: '上级验收通过', word: '已通过', state: 'completed'
    }),
    node({
      id: 'card_197', title: '前端契约 v764', type: 'topic', parent: 'card_109',
      x: 1060, y: 420, flow: '待验收', word: '待验收'
    }),
    // ── third 之下（第四层演示）────────────────────────────
    node({
      id: 'card_464', title: '画布页 · 视觉与实现', type: 'ui', parent: 'card_257',
      x: 60, y: 600, flow: '目标进行中', word: '推进中',
      run: 'run_046', runState: 'waiting_tool', activity: 'running'
    }),
    node({
      id: 'card_470', title: '会话页 · 时间线', type: 'ui', parent: 'card_257',
      x: 300, y: 600, flow: '模型自声明本轮达成', word: '待复核'
    }),
    node({
      id: 'card_471', title: '画布页验收', type: 'qa_card', parent: 'card_257',
      x: 180, y: 780, flow: '阻塞或暂停', word: '受阻', quals: ['USER_DECISION']
    }),
    // ── 归档演示（非 active，必带原因码）──────────────────
    node({
      id: 'card_005', title: '早期交互稿 · 归档', type: 'tool', parent: 'card_074',
      x: 1000, y: 780, life: 'archived', reason: 'ARCH-04',
      flow: '上级验收通过', word: '已通过', state: 'completed'
    }),
    // ── 完成态演示 ────────────────────────────────────────
    node({
      id: 'card_300', title: '设计令牌基线', type: 'topic', parent: 'card_430',
      x: 760, y: 600, flow: '上级验收通过', word: '已通过', state: 'completed'
    })
  ];

  var E = [
    { relation_id: 'rel_p_001_074', from: 'card_001', to: 'card_074', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_001_109', from: 'card_001', to: 'card_109', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_001_150', from: 'card_001', to: 'card_150', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_001_163', from: 'card_001', to: 'card_163', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_074_257', from: 'card_074', to: 'card_257', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_074_390', from: 'card_074', to: 'card_390', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_074_430', from: 'card_074', to: 'card_430', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_074_005', from: 'card_074', to: 'card_005', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_109_287', from: 'card_109', to: 'card_287', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_109_197', from: 'card_109', to: 'card_197', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_257_464', from: 'card_257', to: 'card_464', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_257_470', from: 'card_257', to: 'card_470', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_257_471', from: 'card_257', to: 'card_471', type: '父子', effective: true, revision: 1 },
    { relation_id: 'rel_p_430_300', from: 'card_430', to: 'card_300', type: '父子', effective: true, revision: 1 },

    { relation_id: 'rel_c_464_471', from: 'card_464', to: 'card_471', type: '通信', effective: true, revision: 1 },
    { relation_id: 'rel_c_430_464', from: 'card_430', to: 'card_464', type: '通信', effective: true, revision: 1 },
    { relation_id: 'rel_c_197_287', from: 'card_197', to: 'card_287', type: '通信', effective: true, revision: 1 },
    { relation_id: 'rel_c_163_109', from: 'card_163', to: 'card_109', type: '通信', effective: true, revision: 1 },

    { relation_id: 'rel_d_464_197', from: 'card_464', to: 'card_197', type: '依赖关联', effective: true, revision: 1 },
    { relation_id: 'rel_d_257_163', from: 'card_257', to: 'card_163', type: '依赖关联', effective: true, revision: 1 }
  ];

  root.CC_FIXTURE = {
    project_id: 'work_027',
    snapshot_seq: 1,
    canvas_view_version: 1,
    nodes: N,
    edges: E,
    layout_version: 1,
    projection_version: 1
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);

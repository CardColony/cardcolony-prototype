/**
 * 枚举取值表 —— 逐字抄自 card_197 契约 §1.9（源：cardcolony-data/src/model/enums.ts）。
 * 前端不得新增取值；运行时校验以 cardcolony-data 的 ENUM_VALUES 为唯一依据，
 * 本文件只是「零构建环境下的镜像」，接入真数据层后应改为 import。
 */
(function (root) {
  'use strict';

  var FLOW_STATE = [
    '目标进行中',
    '模型自声明本轮达成',
    '待验收',
    '阻塞或暂停',
    '修正中',
    '上级验收通过'
  ];

  var CARD_STATE = ['active', 'completed'];
  var ENTITY_LIFECYCLE = ['active', 'archived', 'deleted', 'migrated'];
  var ACTIVITY = ['running', 'halted', 'completed', 'idle'];
  var RUN_STATE = [
    'queued', 'starting', 'waiting_context', 'waiting_model',
    'waiting_tool', 'completing', 'completed', 'failed', 'interrupted'
  ];
  var EDGE_TYPE = ['父子', '通信', '依赖关联'];
  var CARD_TYPE = [
    'project', 'sub_project', 'tool', 'coder', 'qa_card', 'pm_card',
    'review', 'rule', 'role', 'topic', 'function'
  ];
  var BUDGET_STATE = ['充足', '已耗尽'];
  var ACCEPTANCE_CONCLUSION = ['无', '验收驳回', '上级验收通过'];
  var STAGE_FLAG = ['无', '已提交', '已撤回'];
  var DELIVERY_FLAG = ['未交付', '已交付'];
  var BLOCK_REASON_CLASS = [
    'EXTERNAL_CONDITION', 'USER_DECISION', 'DEPENDENT_PARTY',
    'USER_PAUSED', 'GOVERNMENT_HALT'
  ];
  var RESYNC_REASON = [
    'hole_detected', 'version_mismatch', 'ring_evicted',
    'projection_restarted', 'scope_revoked', 'buffer_overflow'
  ];
  var INCREMENT_OPERATION = [
    'session.append', 'session.patch', 'task.patch', 'card.patch',
    'relation.upsert', 'relation.remove',
    'canvas.node_upsert', 'canvas.node_patch', 'canvas.node_remove',
    'canvas.edge_upsert', 'canvas.edge_remove',
    'review.patch', 'resync_required'
  ];
  var STREAM_NOTICE_CODE = [
    'backpressure_paused', 'backpressure_closed',
    'scope_revoked', 'projection_restarted'
  ];

  root.CC_ENUMS = {
    FLOW_STATE: FLOW_STATE,
    CARD_STATE: CARD_STATE,
    ENTITY_LIFECYCLE: ENTITY_LIFECYCLE,
    ACTIVITY: ACTIVITY,
    RUN_STATE: RUN_STATE,
    EDGE_TYPE: EDGE_TYPE,
    CARD_TYPE: CARD_TYPE,
    BUDGET_STATE: BUDGET_STATE,
    ACCEPTANCE_CONCLUSION: ACCEPTANCE_CONCLUSION,
    STAGE_FLAG: STAGE_FLAG,
    DELIVERY_FLAG: DELIVERY_FLAG,
    BLOCK_REASON_CLASS: BLOCK_REASON_CLASS,
    RESYNC_REASON: RESYNC_REASON,
    INCREMENT_OPERATION: INCREMENT_OPERATION,
    STREAM_NOTICE_CODE: STREAM_NOTICE_CODE
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);

/**
 * 状态层（§8.8.1 三层中的 state 层）—— 唯一权威状态持有者，不含任何 DOM 操作。
 *
 * 职责：
 *   1. 持有视图快照（canvas / card / session / task / review），对外只读吐出；
 *   2. 应用 §3.5 十三种增量，逐条做四版本校验（§8.2.2）；
 *   3. 重同步状态机（§8.2.3，五步、至多一次），失败不静默；
 *   4. 变更后原子替换快照对象并发通知 —— 渲染层只订阅、不反写。
 *
 * 红线（§8.2.6）：前端不持有业务权威状态。本层所有数据均来自服务端快照与增量，
 * 前端只做投影与索引，不推导、不自造枚举值。
 */
(function (root) {
  'use strict';

  var EN = root.CC_ENUMS;

  /** 四版本字段名（§8.2.2）：任一增量都带，缺一即判违例 */
  var VERSION_KEYS = ['view_version', 'projection_version', 'snapshot_seq', 'layout_version'];

  function nowSeq() { return Date.now(); }

  function create(initial, opts) {
    opts = opts || {};

    var state = {
      canvas: cloneCanvas(initial.canvas),
      cards: initial.cards || {},          // card_id -> CardReading（§4.1）
      session: initial.session || null,     // §4.3
      status: 'live',                       // live | suspect | pulling | swapping | resubscribing | failed
      pendingOps: []
    };

    var listeners = [];
    var resyncGuard = { running: false, attempts: 0 };
    var log = [];

    /* ---------- 只读对外 ---------- */

    function getState() { return state; }
    function getLog() { return log.slice(-200); }

    function subscribe(fn) {
      listeners.push(fn);
      return function () {
        var i = listeners.indexOf(fn);
        if (i >= 0) listeners.splice(i, 1);
      };
    }

    function emit(reason) {
      var snap = state;
      for (var i = 0; i < listeners.length; i++) {
        try { listeners[i](snap, reason); } catch (e) {
          // 渲染层异常不得污染状态层
          if (root.console) console.error('[store] listener threw', e);
        }
      }
    }

    function note(level, msg, detail) {
      log.push({ at: new Date().toISOString(), level: level, msg: msg, detail: detail || null });
      if (level === 'error' && root.console) console.warn('[store]', msg, detail || '');
    }

    /* ---------- 快照 ---------- */

    function cloneCanvas(c) {
      return {
        project_id: c.project_id,
        snapshot_seq: c.snapshot_seq,
        canvas_view_version: c.canvas_view_version,
        layout_version: c.layout_version,
        projection_version: c.projection_version,
        nodes: (c.nodes || []).map(function (n) { return shallow(n); }),
        edges: (c.edges || []).map(function (e) { return shallow(e); })
      };
    }

    function shallow(o) {
      var r = {}, k;
      for (k in o) if (Object.prototype.hasOwnProperty.call(o, k)) r[k] = o[k];
      return r;
    }

    /** 快照原子替换：用于首屏与重同步成功 */
    function replaceSnapshot(next) {
      state.canvas = cloneCanvas(next.canvas || next);
      if (next.cards) state.cards = next.cards;
      if (next.session) state.session = next.session;
      state.status = 'live';
      resyncGuard.running = false;
      resyncGuard.attempts = 0;
      emit('snapshot');
    }

    /* ---------- 四版本校验（§8.2.2） ---------- */

    /**
     * 逐增量校验四版本。返回 { ok, reason }。
     * 判定：快照序号必须**恰好递进 +1**；view/projection/layout 三者不得回退。
     * 任一不符 ⇒ 触发重同步（reason=hole_detected 或 version_mismatch）。
     */
    function checkVersions(frame) {
      for (var i = 0; i < VERSION_KEYS.length; i++) {
        if (frame[VERSION_KEYS[i]] === undefined || frame[VERSION_KEYS[i]] === null) {
          return { ok: false, reason: 'version_mismatch', detail: 'missing ' + VERSION_KEYS[i] };
        }
      }
      var c = state.canvas;
      // 快照序号：必须严格 +1（跳号即空洞）
      if (frame.snapshot_seq !== c.snapshot_seq + 1) {
        return { ok: false, reason: 'hole_detected', detail: 'seq ' + c.snapshot_seq + ' -> ' + frame.snapshot_seq };
      }
      // 视图/投影/布局版本：单调不减
      if (frame.view_version < c.canvas_view_version) {
        return { ok: false, reason: 'version_mismatch', detail: 'view ' + frame.view_version + ' < ' + c.canvas_view_version };
      }
      if (frame.projection_version < c.projection_version) {
        return { ok: false, reason: 'version_mismatch', detail: 'projection rollback' };
      }
      if (frame.layout_version < c.layout_version) {
        return { ok: false, reason: 'version_mismatch', detail: 'layout rollback' };
      }
      return { ok: true };
    }

    function commitVersions(frame) {
      var c = state.canvas;
      c.snapshot_seq = frame.snapshot_seq;
      c.canvas_view_version = frame.view_version;
      c.projection_version = frame.projection_version;
      c.layout_version = frame.layout_version;
    }

    /* ---------- 增量应用（§3.5 十三种） ---------- */

    /**
     * frame: { operation, payload, view_version, projection_version, snapshot_seq, layout_version }
     * 返回 { ok: true } 或 { ok: false, reason }
     */
    function applyIncrement(frame) {
      var op = frame && frame.operation;
      if (!op) { note('error', 'increment without operation'); return { ok: false, reason: 'bad_frame' }; }
      if (EN.INCREMENT_OPERATION.indexOf(op) < 0) {
        note('error', 'unknown increment operation: ' + op);
        return { ok: false, reason: 'bad_frame' };
      }

      if (op === 'resync_required') {
        note('warn', 'server requested resync: ' + (frame.payload && frame.payload.reason));
        requestResync((frame.payload && frame.payload.reason) || 'projection_restarted',
          (frame.payload && frame.payload.resume_cursor) || null);
        return { ok: true, defer: true };
      }

      var v = checkVersions(frame);
      if (!v.ok) {
        note('warn', 'version check failed: ' + v.reason + ' (' + (v.detail || '') + ')');
        requestResync(v.reason, null);
        return { ok: false, reason: v.reason };
      }

      var next;
      try {
        next = reduce(op, frame.payload);
      } catch (e) {
        note('error', 'reduce threw on ' + op, String(e && e.message || e));
        requestResync('hole_detected', null);
        return { ok: false, reason: 'reduce_failed' };
      }
      if (!next) return { ok: false, reason: 'unhandled' };

      commitVersions(frame);
      emit(op);
      return { ok: true };
    }

    /** 纯函数式 reduce：返回 true 表示已改动 */
    function reduce(op, p) {
      var c = state.canvas;
      switch (op) {
        case 'canvas.node_upsert': {
          var n = p.node || p;
          var idx = idxOfNode(n.card_id);
          if (idx >= 0) c.nodes[idx] = shallow(n); else c.nodes.push(shallow(n));
          return true;
        }
        case 'canvas.node_patch': {
          var id = p.card_id || (p.node && p.node.card_id);
          var i2 = idxOfNode(id);
          if (i2 < 0) { note('warn', 'node_patch for unknown node ' + id); return false; }
          var patch = p.patch || p.node || {};
          c.nodes[i2] = merge(c.nodes[i2], patch);
          return true;
        }
        case 'canvas.node_remove': {
          var i3 = idxOfNode(p.card_id);
          if (i3 < 0) return false;
          c.nodes.splice(i3, 1);
          // 关联边一并移除（服务端也会下发 edge_remove，此处幂等）
          c.edges = c.edges.filter(function (e) { return e.from !== p.card_id && e.to !== p.card_id; });
          return true;
        }
        case 'canvas.edge_upsert': {
          var e1 = p.edge || p;
          var j = idxOfEdge(e1.relation_id);
          if (j >= 0) c.edges[j] = shallow(e1); else c.edges.push(shallow(e1));
          return true;
        }
        case 'canvas.edge_remove': {
          var before = c.edges.length;
          c.edges = c.edges.filter(function (e) { return e.relation_id !== p.relation_id; });
          return c.edges.length !== before;
        }
        case 'card.patch': {
          var cid = p.card_id;
          var cur = state.cards[cid] || {};
          state.cards[cid] = merge(cur, p.patch || {});
          return true;
        }
        case 'task.patch': {
          var tid = p.task_id;
          var host = p.host_card_id;
          if (host && state.cards[host]) state.cards[host].__task = merge(state.cards[host].__task || {}, p.patch || {});
          return true;
        }
        case 'session.append': {
          if (!state.session) state.session = { items: [] };
          state.session.items = (state.session.items || []).concat(p.items || []);
          return true;
        }
        case 'session.patch': {
          if (!state.session) state.session = { items: [] };
          var items = state.session.items || [];
          var target = p.entry_id || p.item_id;
          for (var k = 0; k < items.length; k++) {
            if (items[k].entry_id === target) { items[k] = merge(items[k], p.patch || {}); return true; }
          }
          return false;
        }
        case 'relation.upsert': {
          var rel = p.relation || p;
          var ri = idxOfEdge(rel.relation_id);
          if (ri >= 0) c.edges[ri] = shallow(rel); else c.edges.push(shallow(rel));
          return true;
        }
        case 'relation.remove': {
          var b2 = c.edges.length;
          c.edges = c.edges.filter(function (e) { return e.relation_id !== p.relation_id; });
          return c.edges.length !== b2;
        }
        case 'review.patch': {
          state.reviews = state.reviews || {};
          state.reviews[p.batch_id] = merge(state.reviews[p.batch_id] || {}, p.patch || {});
          return true;
        }
        default:
          note('warn', 'unhandled operation ' + op);
          return false;
      }
    }

    function idxOfNode(id) {
      var ns = state.canvas.nodes;
      for (var i = 0; i < ns.length; i++) if (ns[i].card_id === id) return i;
      return -1;
    }
    function idxOfEdge(rid) {
      var es = state.canvas.edges;
      for (var i = 0; i < es.length; i++) if (es[i].relation_id === rid) return i;
      return -1;
    }
    function merge(a, b) {
      var r = shallow(a), k;
      for (k in b) if (Object.prototype.hasOwnProperty.call(b, k)) r[k] = b[k];
      return r;
    }

    /* ---------- 重同步状态机（§8.2.3：五步、至多一次） ---------- */

    /**
     * 五步：suspect（标记脏）→ pulling（拉全量快照）→ swapping（原子替换）
     *       → resubscribing（重建订阅）→ live（恢复）。
     * 「至多一次」：同一轮重同步中若再次触发，只记录不重入；失败进入 failed 并由 UI 明示。
     */
    function requestResync(reason, resumeCursor) {
      if (resyncGuard.running) {
        note('warn', 'resync already running, coalesced: ' + reason);
        return;
      }
      resyncGuard.running = true;
      resyncGuard.attempts += 1;

      state.status = 'suspect';
      note('info', 'resync requested: ' + reason);
      emit('status');

      // 1) suspect → pulling
      state.status = 'pulling';
      emit('status');

      var pull = opts.pullSnapshot;
      if (typeof pull !== 'function') {
        // 无后端：本地重放同一快照，保持 UI 可用并留痕
        state.status = 'swapping';
        emit('status');
        resyncGuard.running = false;
        state.status = 'live';
        note('warn', 'no pullSnapshot wired; local snapshot kept (reason=' + reason + ')');
        emit('resync_local');
        return;
      }

      Promise.resolve().then(function () { return pull(reason); }).then(function (snap) {
        if (!snap) throw new Error('empty snapshot');
        // 2) pulling → swapping（原子替换）
        state.status = 'swapping';
        emit('status');
        replaceSnapshot(snap);
        // 3) swapping → resubscribing
        state.status = 'resubscribing';
        emit('status');
        if (opts.resubscribe) opts.resubscribe(resumeCursor);
        // 4) → live
        state.status = 'live';
        emit('status');
        note('info', 'resync completed');
      }).catch(function (err) {
        resyncGuard.running = false;
        state.status = 'failed';
        note('error', 'resync failed: ' + String(err && err.message || err));
        emit('status');
      });
    }

    /* ---------- 用户操作（§5.2 十六项，回调注入） ---------- */

    /**
     * 操作统一走这里：乐观性为零 —— 等 operation_result 终态再改状态（§5.5）。
     * 返回 Promise<OperationResult>。
     */
    function dispatchOperation(action, params) {
      if (typeof opts.sendOperation !== 'function') {
        note('warn', 'no backend wired; operation dropped: ' + action);
        return Promise.resolve({ status: 'rejected', error_code: 'projection_unavailable', action: action });
      }
      return Promise.resolve(opts.sendOperation(action, params || {})).then(function (res) {
        if (res && res.status === 'applied' && res.result_event_anchor) {
          note('info', 'operation applied: ' + action);
        }
        return res;
      });
    }

    /* ---------- 派生查询（只读，不改状态） ---------- */

    function nodeById(id) {
      var ns = state.canvas.nodes;
      for (var i = 0; i < ns.length; i++) if (ns[i].card_id === id) return ns[i];
      return null;
    }

    function flowDistribution() {
      var dist = {};
      var i;
      for (i = 0; i < EN.FLOW_STATE.length; i++) dist[EN.FLOW_STATE[i]] = 0;
      var ns = state.canvas.nodes;
      for (i = 0; i < ns.length; i++) {
        var n = ns[i];
        if (n.task_summary) dist[n.task_summary.flow_state] = (dist[n.task_summary.flow_state] || 0) + 1;
      }
      return dist;
    }

    function lifecycleDistribution() {
      var dist = {};
      var ns = state.canvas.nodes;
      for (var i = 0; i < ns.length; i++) {
        var l = ns[i].entity_lifecycle;
        dist[l] = (dist[l] || 0) + 1;
      }
      return dist;
    }

    function edgesByType() {
      var r = {};
      var es = state.canvas.edges;
      for (var i = 0; i < es.length; i++) {
        var t = es[i].type;
        r[t] = (r[t] || 0) + 1;
      }
      return r;
    }

    return {
      getState: getState,
      getLog: getLog,
      subscribe: subscribe,
      applyIncrement: applyIncrement,
      replaceSnapshot: replaceSnapshot,
      requestResync: requestResync,
      dispatchOperation: dispatchOperation,
      nodeById: nodeById,
      flowDistribution: flowDistribution,
      lifecycleDistribution: lifecycleDistribution,
      edgesByType: edgesByType
    };
  }

  root.CCStore = { create: create, VERSION_KEYS: VERSION_KEYS };
})(typeof globalThis !== 'undefined' ? globalThis : this);

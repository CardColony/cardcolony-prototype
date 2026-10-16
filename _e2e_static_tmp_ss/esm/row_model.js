/* esm/row_model.js —— [S2/R-10] 导航行对象零状态工厂。
 *
 * 行对象只持不可变 card 引用与展开上下文（kind/depth/wid，构造时定死）；
 * 六字段（expanded/source/loading/error/loaded/selected）全部由 props(store)
 * 实时投影自三层状态——对行对象 Object.freeze 后 props 仍正确（零状态铁证，V-S4）。
 */
import { NAV_ROW_HEIGHT, NAV_PHEAD_HEIGHT } from "./state_layers.js";

export function createRowModel(card, store, ctx = {}) {
  const row = {
    card,                                   // 服务端快照对象，渲染期不修改
    kind: ctx.kind || "card",               // "card" | "project"
    depth: ctx.depth || 0,
    wid: ctx.wid || (card && card.work_id) || "",
    // [左栏接线 步2] 行几何上下文（expandRows 投影时定死，行对象仍零语义状态）：
    //   guides＝祖先非末子列贯穿线；first/last＝可见兄弟位次（拐弯线/出头线判据）；
    //   lastInProject＝该项目最后一行（四圆角卡片底边判据）
    guides: ctx.guides || [],
    first: !!ctx.first,
    last: !!ctx.last,
    lastInProject: !!ctx.lastInProject,
    // [定稿] 搜索镜片态（构造期定死，行对象仍零状态）：
    //   searching=true 时这一行是「搜索投影」而非用户摆的树——展开态一律视为展开
    //   （命中路径要显出），且**不写回 prefs**；matched=true 时标题标黄。
    searching: !!ctx.searching,
    matched: !!ctx.matched,
    searchKids: !!ctx.searchKids,
    rowHeight() { return this.kind === "project" ? NAV_PHEAD_HEIGHT : NAV_ROW_HEIGHT; },

    key() {
      return this.kind === "project"
        ? "p:" + this.card.work_id
        : "c:" + this.wid + ":" + this.card.card_id;
    },

    // 实时投影六字段（全来自 store 三层，行对象自身无任何状态）
    props(s) {
      const s2 = s || store;
      const cid = this.card.card_id;
      if (this.kind === "project") {
        const sel = s2.select.get();
        const open = s2.prefs.getProjectOpen(this.wid);
        return {
          expanded: this.searching || open,
          source: open ? "manual" : null,
          loading: false,
          error: null,
          loaded: true,
          selected: !!sel && sel.wid === this.wid,
        };
      }
      const sel = s2.select.get();
      const src = s2.prefs.getExpanded(this.wid, cid);
      return {
        expanded: this.searching || !!src,
        source: src || null,
        loading: s2.temp.isLoading(cid),
        error: s2.temp.getError(cid),
        loaded: s2.temp.isLoaded(cid),
        selected: !!sel && sel.cid === cid && sel.wid === this.wid,
      };
    },
  };
  return row;
}

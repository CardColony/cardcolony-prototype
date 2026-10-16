/* esm/nav_async.js —— [S2/R-22] 导航子卡异步加载：展开才拉、请求去重、三态就地、折叠即丢。
 *
 * - pendingLoads Map 去重：同节点重复展开共用同一 Promise，不发第二次（V-V4）。
 * - child_count=0 的叶节点零请求，直接置 loaded（叶形态 .nav-twist.none 由渲染层按
 *   child_count 判定，V-V4「空子卡节点 0 次且叶形」）。
 * - 折叠即丢：fold() 丢弃该段 children 数据 + loaded 清 false，下次展开重拉。
 */
export function createNavAsync(store, api) {
  const pendingLoads = new Map();   // "wid:cid" → Promise

  async function loadChildren(row) {
    const cid = row.card.card_id;
    const wid = row.wid;
    if (store.temp.isLoaded(cid)) return;
    if ((row.card.child_count || 0) === 0) {   // 叶：零网络（V-V4）
      store.temp.loaded(cid, true);
      return;
    }
    const k = wid + ":" + cid;
    if (pendingLoads.has(k)) return pendingLoads.get(k);   // 去重（V-V4）
    store.temp.loading(cid, true);
    store.temp.error(cid, null);
    const p = api.getNavChildren(wid, cid)
      .then(d => {
        store.snapshot.setChildren(wid, cid, (d && d.cards) || []);
        store.temp.loaded(cid, true);
      })
      .catch(e => {
        store.temp.error(cid, (e && e.message) || "加载失败");
      })
      .finally(() => {
        store.temp.loading(cid, false);
        pendingLoads.delete(k);
      });
    pendingLoads.set(k, p);
    return p;
  }

  // 折叠即丢（R-14）：children 数据丢弃（含后代段级联）、loaded 清 false
  function fold(row) {
    const cid = row.card.card_id;
    store.snapshot.dropChildren(row.wid, cid);
    store.temp.loaded(cid, false);
    store.temp.error(cid, null);
  }

  // 该节点是否在加载中（渲染层判定加载中态用）
  function isPending(row) {
    return pendingLoads.has(row.wid + ":" + row.card.card_id);
  }

  return { loadChildren, fold, isPending };
}

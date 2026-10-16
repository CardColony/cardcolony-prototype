/* esm/main_chat.js —— [分页 2026-10-16] 对话页唯一入口。
 *
 * 装配序：store → api → util → stream → workspace（workspace 即对话页业务模块，
 * 内部自建 nav_system 与规则面板）。画布不进本页——画布在 /work/{wid} 独立页面。
 * 卡号来自服务端注入的路径参数（window.__CARD_ID__），URL hash 机制已退役。 */
import { store } from "./store.js?v=30";
import { API } from "./api.js?v=35";
import "./util.js?v=30";
import "../stream.js?v=35";
import "../workspace.js?v=35";

window.API = API;   // 调试便利（非功能依赖；与 api.js 薄壳同属兼容层）

/* 项目统筹（coordination）画布页挂载入口：work.html 专用。
 * 逻辑在 coordination.js（mountCanvasCoordination）；本文件只是挂载点，
 * 独立模块——加载失败不影响画布其余功能。 */
import { mountCanvasCoordination } from "./coordination.js";

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => mountCanvasCoordination());
} else {
  mountCanvasCoordination();
}

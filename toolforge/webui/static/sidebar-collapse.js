/* ToolForge Flow Studio — sidebar collapse/expand.
 *
 * Scope contract (see HTML spec):
 *   - Owns: `.pane.collapsed` class on the inventory/inspector asides,
 *     toggle button glyph + aria-expanded, localStorage persistence,
 *     keyboard shortcuts.
 *   - Co-owns CSS vars --left-w / --right-w on .grid, but only while a
 *     pane is collapsed: we set them inline to RAIL_PX, and remove the
 *     inline override on expand so the resize agent's value (set on the
 *     same element) takes over again. We never write the var when the
 *     pane is expanded.
 */

const LS_LEFT  = "tf.left.collapsed";
const LS_RIGHT = "tf.right.collapsed";
const RAIL_PX  = "28px";

const GLYPH_OPEN_LEFT   = "«"; // pane is open, click to collapse leftward
const GLYPH_CLOSED_LEFT = "»"; // pane is collapsed, click to expand rightward
const GLYPH_OPEN_RIGHT  = "»"; // pane is open, click to collapse rightward
const GLYPH_CLOSED_RIGHT = "«";

function readBool(key) {
  try { return localStorage.getItem(key) === "1"; } catch { return false; }
}
function writeBool(key, v) {
  try { localStorage.setItem(key, v ? "1" : "0"); } catch { /* ignore */ }
}

function applyState(pane, btn, grid, collapsed, side) {
  if (!pane || !btn) return;
  pane.classList.toggle("collapsed", collapsed);
  btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
  btn.setAttribute(
    "aria-label",
    `${collapsed ? "Expand" : "Collapse"} ${side === "left" ? "left" : "right"} panel`,
  );
  if (side === "left") {
    btn.textContent = collapsed ? GLYPH_CLOSED_LEFT : GLYPH_OPEN_LEFT;
  } else {
    btn.textContent = collapsed ? GLYPH_CLOSED_RIGHT : GLYPH_OPEN_RIGHT;
  }
  if (grid) {
    const varName = side === "left" ? "--left-w" : "--right-w";
    if (collapsed) {
      grid.style.setProperty(varName, RAIL_PX);
    } else {
      // Hand the var back to the resize agent.
      grid.style.removeProperty(varName);
    }
  }
}

function init() {
  const grid      = document.querySelector("main.grid");
  const leftPane  = document.getElementById("pane-inventory");
  const rightPane = document.getElementById("pane-inspector");
  const leftBtn   = document.getElementById("collapse-left");
  const rightBtn  = document.getElementById("collapse-right");

  if (!leftPane || !rightPane || !leftBtn || !rightBtn) return;

  let leftCollapsed  = readBool(LS_LEFT);
  let rightCollapsed = readBool(LS_RIGHT);

  applyState(leftPane,  leftBtn,  grid, leftCollapsed,  "left");
  applyState(rightPane, rightBtn, grid, rightCollapsed, "right");

  function toggleLeft() {
    leftCollapsed = !leftCollapsed;
    writeBool(LS_LEFT, leftCollapsed);
    applyState(leftPane, leftBtn, grid, leftCollapsed, "left");
  }
  function toggleRight() {
    rightCollapsed = !rightCollapsed;
    writeBool(LS_RIGHT, rightCollapsed);
    applyState(rightPane, rightBtn, grid, rightCollapsed, "right");
  }

  leftBtn.addEventListener("click", (e) => { e.preventDefault(); toggleLeft(); });
  rightBtn.addEventListener("click", (e) => { e.preventDefault(); toggleRight(); });

  // Keyboard: Ctrl+\ left, Ctrl+Shift+\ right.
  // event.code === "Backslash" is layout-safe; key may be "\\" or "|".
  window.addEventListener("keydown", (e) => {
    if (!e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.code !== "Backslash" && e.key !== "\\" && e.key !== "|") return;
    // Skip when typing in an input/textarea/contenteditable.
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    e.preventDefault();
    if (e.shiftKey) toggleRight(); else toggleLeft();
  });

  // Tiny global API for debugging / future hooks.
  window.tfSidebar = {
    toggleLeft, toggleRight,
    get leftCollapsed()  { return leftCollapsed; },
    get rightCollapsed() { return rightCollapsed; },
  };
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init, { once: true });
} else {
  init();
}

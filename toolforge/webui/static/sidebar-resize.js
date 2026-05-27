/* ToolForge Flow Studio — drag-to-resize sidebar widths.
 *
 * Exposes window.toolforgeSidebarResize = { setWidth, reset, getWidth }
 * Reads/writes localStorage: tf.left.w, tf.right.w
 * Drives CSS vars: --left-w, --right-w on <main class="grid">
 *
 * Handles are absolute-positioned overlays inside main.grid.
 * Repositioned on pointermove + on window resize.
 *
 * Bounds:
 *   left  pane  200..520  (default 320)
 *   right pane  240..560  (default 340)
 *
 * Coordinated with sidebar-collapse: when a pane has .collapsed, the
 * corresponding handle is hidden via the `hidden` attr. A MutationObserver
 * keeps this in sync without coupling the two modules.
 */

const SIDES = {
  left: {
    storageKey: 'tf.left.w',
    cssVar: '--left-w',
    paneSelector: '.pane.inventory',
    min: 200,
    max: 520,
    def: 320,
  },
  right: {
    storageKey: 'tf.right.w',
    cssVar: '--right-w',
    paneSelector: '.pane.inspector',
    min: 240,
    max: 560,
    def: 340,
  },
};

const KEYBOARD_STEP = 16;

const clamp = (n, lo, hi) => Math.min(Math.max(n, lo), hi);

function readStored(side) {
  const cfg = SIDES[side];
  try {
    const raw = localStorage.getItem(cfg.storageKey);
    if (!raw) return null;
    const n = parseInt(raw, 10);
    if (!Number.isFinite(n)) return null;
    return clamp(n, cfg.min, cfg.max);
  } catch {
    return null;
  }
}

function writeStored(side, value) {
  const cfg = SIDES[side];
  try {
    localStorage.setItem(cfg.storageKey, String(Math.round(value)));
  } catch {
    /* quota/private-mode — drop silently */
  }
}

function clearStored(side) {
  const cfg = SIDES[side];
  try { localStorage.removeItem(cfg.storageKey); } catch { /* noop */ }
}

function init() {
  const grid = document.querySelector('main.grid');
  if (!grid) return;

  const handles = {
    left: grid.querySelector('.resize-handle[data-side="left"]'),
    right: grid.querySelector('.resize-handle[data-side="right"]'),
  };
  if (!handles.left || !handles.right) return;

  const panes = {
    left: grid.querySelector(SIDES.left.paneSelector),
    right: grid.querySelector(SIDES.right.paneSelector),
  };

  const widths = {
    left: readStored('left') ?? SIDES.left.def,
    right: readStored('right') ?? SIDES.right.def,
  };

  const MIN_CANVAS = 320;

  function effectiveMax(side) {
    const cfg = SIDES[side];
    const otherSide = side === 'left' ? 'right' : 'left';
    const otherPane = panes[otherSide];
    const otherCollapsed = otherPane?.classList.contains('collapsed');
    const otherW = otherCollapsed ? 28 : (widths[otherSide] ?? SIDES[otherSide].def);
    const avail = window.innerWidth - otherW - MIN_CANVAS;
    return Math.max(cfg.min, Math.min(cfg.max, avail));
  }

  function applyWidth(side, w, { persist = true } = {}) {
    const cfg = SIDES[side];
    const clamped = clamp(w, cfg.min, effectiveMax(side));
    widths[side] = clamped;
    grid.style.setProperty(cfg.cssVar, `${clamped}px`);
    handles[side].setAttribute('aria-valuenow', String(Math.round(clamped)));
    if (persist) writeStored(side, clamped);
    positionHandles();
  }

  function positionHandles() {
    // Position each handle on the seam between panes. Use the actual rendered
    // pane geometry so we stay correct regardless of CSS-var rounding or any
    // future layout changes (collapse, viewport resize, etc.).
    const gridRect = grid.getBoundingClientRect();
    for (const side of ['left', 'right']) {
      const pane = panes[side];
      const handle = handles[side];
      if (!pane || !handle) continue;
      const collapsed = pane.classList.contains('collapsed');
      handle.hidden = collapsed;
      if (collapsed) continue;
      const r = pane.getBoundingClientRect();
      const seam = side === 'left' ? r.right : r.left;
      handle.style.left = `${seam - gridRect.left}px`;
    }
  }

  // Drag state
  const drag = { side: null, startX: 0, startW: 0, pointerId: null };

  function onPointerDown(side, ev) {
    if (ev.button !== undefined && ev.button !== 0) return;
    drag.side = side;
    drag.startX = ev.clientX;
    drag.startW = widths[side];
    drag.pointerId = ev.pointerId;
    handles[side].classList.add('dragging');
    handles[side].setPointerCapture?.(ev.pointerId);
    document.body.classList.add('resizing');
    ev.preventDefault();
  }

  function onPointerMove(ev) {
    if (!drag.side) return;
    const dx = ev.clientX - drag.startX;
    // Right pane grows as cursor moves LEFT (handle is on its left edge).
    const delta = drag.side === 'left' ? dx : -dx;
    applyWidth(drag.side, drag.startW + delta, { persist: false });
  }

  function onPointerUp(ev) {
    if (!drag.side) return;
    const side = drag.side;
    try { handles[side].releasePointerCapture?.(ev.pointerId); } catch { /* noop */ }
    handles[side].classList.remove('dragging');
    document.body.classList.remove('resizing');
    writeStored(side, widths[side]);
    drag.side = null;
    drag.pointerId = null;
  }

  function onDblClick(side) {
    clearStored(side);
    applyWidth(side, SIDES[side].def, { persist: false });
  }

  function onKey(side, ev) {
    const cfg = SIDES[side];
    let next = null;
    // Left-handle convention: ArrowRight grows the LEFT pane, ArrowLeft shrinks.
    // Right-handle convention: ArrowLeft grows the RIGHT pane, ArrowRight shrinks.
    if (ev.key === 'ArrowLeft') {
      next = widths[side] + (side === 'right' ? KEYBOARD_STEP : -KEYBOARD_STEP);
    } else if (ev.key === 'ArrowRight') {
      next = widths[side] + (side === 'right' ? -KEYBOARD_STEP : KEYBOARD_STEP);
    } else if (ev.key === 'Home') {
      next = cfg.min;
    } else if (ev.key === 'End') {
      next = cfg.max;
    } else if (ev.key === 'Enter' || ev.key === ' ') {
      onDblClick(side);
      ev.preventDefault();
      return;
    } else {
      return;
    }
    ev.preventDefault();
    applyWidth(side, next);
  }

  for (const side of ['left', 'right']) {
    const h = handles[side];
    h.addEventListener('pointerdown', (ev) => onPointerDown(side, ev));
    h.addEventListener('dblclick', () => onDblClick(side));
    h.addEventListener('keydown', (ev) => onKey(side, ev));
  }
  window.addEventListener('pointermove', onPointerMove);
  window.addEventListener('pointerup', onPointerUp);
  window.addEventListener('pointercancel', onPointerUp);
  window.addEventListener('resize', () => {
    // Re-clamp on viewport shrink so panes don't overflow.
    applyWidth('left', widths.left, { persist: false });
    applyWidth('right', widths.right, { persist: false });
    positionHandles();
  });

  // Re-position when panes get collapsed/expanded by the sibling module.
  // Also re-apply our stored width on expand, because the collapse module
  // calls removeProperty on the inline CSS var when expanding — without
  // this, the grid would snap back to the CSS default (320/340) instead of
  // the user's last dragged width.
  const collapsedState = { left: panes.left?.classList.contains('collapsed'),
                           right: panes.right?.classList.contains('collapsed') };
  const mo = new MutationObserver(() => {
    for (const side of ['left', 'right']) {
      const pane = panes[side];
      if (!pane) continue;
      const nowCollapsed = pane.classList.contains('collapsed');
      const wasCollapsed = collapsedState[side];
      if (wasCollapsed && !nowCollapsed) {
        // Re-assert our width on expand.
        applyWidth(side, widths[side], { persist: false });
      }
      collapsedState[side] = nowCollapsed;
    }
    positionHandles();
  });
  for (const side of ['left', 'right']) {
    if (panes[side]) {
      mo.observe(panes[side], { attributes: true, attributeFilter: ['class'] });
    }
  }

  // Keep handles glued to pane seam whenever pane geometry changes
  // (font load, viewport resize, programmatic width change). Without this,
  // any width change outside the drag path leaves handles floating off-seam.
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => positionHandles());
    for (const side of ['left', 'right']) {
      if (panes[side]) ro.observe(panes[side]);
    }
  }

  // Initial apply (no persist — restoring stored value isn't a write)
  applyWidth('left', widths.left, { persist: false });
  applyWidth('right', widths.right, { persist: false });
  // Position once after layout settles (handles font-load shifts etc.)
  requestAnimationFrame(positionHandles);

  // Public API for other modules (e.g. collapse) to read/set widths.
  window.toolforgeSidebarResize = {
    setWidth: (side, w) => applyWidth(side, w),
    getWidth: (side) => widths[side],
    reset: (side) => onDblClick(side),
    reposition: positionHandles,
  };
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init, { once: true });
} else {
  init();
}

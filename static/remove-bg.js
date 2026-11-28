/* Remove Background: three ways to cut out the subject, plus touch-up brushes.
 *
 *   🎨 Colour  Tap the background. Every similar colour touching it turns
 *              transparent (all in the browser, instant). A slider sets how
 *              similar, and more taps add more colours.
 *   ⬚ Box      Drag a box around the subject. The server cuts it out with
 *              OpenCV GrabCut (/api/remove-bg/box, no AI). Brush marks made
 *              before a new box is drawn are sent along as hints.
 *   ✨ AI      The original AI model (/api/remove-bg).
 *
 * In every mode the Keep and Erase brushes paint over the result. Two fingers
 * (or the + / − buttons, or Ctrl + scroll) zoom and move the picture, and the
 * brush gets finer as you zoom in. Background colour, edge clean-up and edge
 * softness are applied in the browser, and the download is exactly the preview.
 *
 * The whole editor works on one "working copy" of the photo, at most WORK_MAX
 * pixels on its longest side. Masks are plain Uint8Arrays of that size.
 */
(() => {
  'use strict';
  const kit = window.pdfSuiteImageKit;
  if (!kit) return;
  const { el, errorFrom, download, setStatus, loadImage, compareSwitch, previewHeader, busyBadge } = kit;
  const $ = id => document.getElementById(id);
  const WORK_MAX = 2000;
  const PHONE = window.matchMedia('(max-width: 900px)');
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  const CSS = `
  .rb-editor[hidden] { display: none !important; }
  .rb-editor { display: flex; flex-direction: column; gap: 12px; margin-bottom: 16px; }
  .rb-modes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; padding: 4px; background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 14px; }
  .rb-modes button { border: 0; background: transparent; border-radius: 10px; min-height: 52px; padding: 6px 4px; font: inherit; font-size: 13px; font-weight: 650; color: #475569; cursor: pointer; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px; line-height: 1.15; }
  .rb-modes button .rb-ico { font-size: 18px; }
  .rb-modes button[aria-pressed="true"] { background: #fff; color: var(--accent, #4f46e5); box-shadow: 0 1px 3px rgba(15,23,42,.14); }
  .rb-hint { margin: 0; font-size: 13.5px; line-height: 1.45; color: #1e293b; background: var(--accent-soft, #eef2ff); border-radius: 12px; padding: 10px 12px; }
  .rb-stage { position: relative; overflow: hidden; touch-action: none; user-select: none; -webkit-user-select: none; -webkit-touch-callout: none; display: flex; align-items: center; justify-content: center; min-height: 240px; border-radius: 12px; border: 1px solid #e2e8f0; }
  .rb-layer { position: relative; transform-origin: 0 0; line-height: 0; }
  .rb-layer[hidden] { display: none; }
  .rb-layer canvas { display: block; }
  .rb-box { position: absolute; border: 2px dashed var(--accent, #4f46e5); background: rgba(79,70,229,.08); box-shadow: 0 0 0 1px rgba(255,255,255,.8); pointer-events: none; }
  .rb-box[hidden], .rb-cursor[hidden] { display: none; }
  .rb-cursor { position: absolute; border-radius: 50%; border: 2px solid #fff; box-shadow: 0 0 0 1px rgba(15,23,42,.6), inset 0 0 0 1px rgba(15,23,42,.6); pointer-events: none; transform: translate(-50%, -50%); }
  .rb-cursor.erase { background: rgba(239,68,68,.18); }
  .rb-cursor.keep { background: rgba(16,185,129,.18); }
  .rb-zoom { display: flex; justify-content: flex-end; align-items: center; gap: 6px; margin-top: 8px; }
  .rb-zoom[hidden] { display: none; }
  .rb-zoom small { margin-right: auto; font-size: 12px; color: #94a3b8; }
  .rb-zoom button { width: 40px; height: 40px; border-radius: 12px; border: 1px solid #e2e8f0; background: #fff; font: inherit; font-size: 19px; font-weight: 700; color: #334155; cursor: pointer; }
  .rb-tools { display: grid; grid-template-columns: repeat(auto-fit, minmax(70px, 1fr)); gap: 6px; }
  .rb-tools button, .rb-row button { min-height: 46px; border-radius: 12px; border: 1px solid #e2e8f0; background: #fff; font: inherit; font-size: 13px; font-weight: 650; color: #334155; cursor: pointer; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1px; padding: 4px; }
  .rb-tools button .rb-ico { font-size: 16px; }
  .rb-tools button[aria-pressed="true"] { border-color: var(--accent, #4f46e5); background: var(--accent-soft, #eef2ff); color: var(--accent, #4f46e5); box-shadow: inset 0 0 0 1px var(--accent, #4f46e5); }
  .rb-tools button[hidden], .rb-opt[hidden] { display: none; }
  .rb-row { display: flex; gap: 6px; }
  .rb-row button { flex: 1; flex-direction: row; gap: 6px; min-height: 40px; }
  .rb-row button:disabled { opacity: .45; cursor: default; }
  .rb-opt label { display: flex; justify-content: space-between; font-size: 13px; font-weight: 600; margin-bottom: 4px; }
  .rb-opt input[type=range] { width: 100%; height: 28px; }
  .rb-opt small { display: block; font-size: 11.5px; color: #94a3b8; margin-top: 2px; }
  .rb-check { display: flex !important; justify-content: flex-start !important; align-items: center; gap: 8px; font-weight: 600; font-size: 13px; margin-top: 6px; }
  .rb-check input { width: 20px; height: 20px; }
  @media (max-width: 900px) { .rb-zoom button { width: 44px; height: 44px; } }
  /* While editing, the main button must not float over the picture. */
  .tp #runRemoveBgBtn.rb-inline { position: static !important; box-shadow: none !important; }
  `;

  function setup() {
    const input = $('removeBgFileInput'), img = $('removeBgPreviewImg'), placeholder = $('removeBgPreviewPlaceholder');
    const runBtn = $('runRemoveBgBtn'), status = $('removeBgStatus');
    if (!input || !img || !runBtn || !status) return;
    document.head.append(el('style', { text: CSS }));

    const controls = runBtn.parentElement;
    const stage = img.parentElement;
    const previewCard = stage.parentElement;
    const previewCardDisplay = previewCard.style.display;
    const note = [...controls.children].find(n => n !== status && /AI processing/.test(n.textContent));

    // ---------- the picture: stage > layer (zoomed) > canvas + overlays ----------
    stage.classList.add('rb-stage');
    stage.style.minHeight = '';
    stage.style.flex = 'none';      // keep the picture at the top, not centred in a tall column
    const canvas = el('canvas', { 'aria-label': 'Picture. Tap or draw on it to choose what to remove.' });
    const ctx = canvas.getContext('2d');
    const canFilter = typeof ctx.filter === 'string';
    const boxEl = el('div', { class: 'rb-box', hidden: '' });
    const cursor = el('div', { class: 'rb-cursor', hidden: '' });
    const layer = el('div', { class: 'rb-layer', hidden: '' }, [canvas, boxEl, cursor]);
    const zoomBtn = (text, label, onclick) => el('button', { type: 'button', text, 'aria-label': label, title: label, onclick });
    const zoomBar = el('div', { class: 'rb-zoom', hidden: '' }, [
      el('small', { text: PHONE.matches ? 'Pinch with two fingers to zoom' : 'Ctrl + scroll to zoom' }),
      zoomBtn('−', 'Zoom out', () => zoomAt(1 / 1.6)),
      zoomBtn('+', 'Zoom in', () => zoomAt(1.6)),
      zoomBtn('⤢', 'Fit the picture', () => setView(1, 0, 0)),
    ]);
    stage.append(layer);
    const badge = busyBadge(stage);
    const switcher = compareSwitch(() => schedule());
    const heading = previewCard.querySelector('h3');
    let headerRow = null;
    if (heading) headerRow = previewHeader(heading, 'Live preview', switcher);
    const previewBlock = el('div', { style: 'display:flex;flex-direction:column' });
    if (headerRow) previewBlock.append(headerRow);
    previewBlock.append(stage, zoomBar);

    // ---------- controls ----------
    const modeBtn = (value, icon, text) => el('button', { type: 'button', 'data-mode': value }, [el('span', { class: 'rb-ico', 'aria-hidden': 'true', text: icon }), text]);
    const modeBtns = [modeBtn('colour', '🎨', 'Tap colour'), modeBtn('box', '⬚', 'Draw box'), modeBtn('ai', '✨', 'Auto (AI)')];
    const modes = el('div', { class: 'rb-modes', role: 'group', 'aria-label': 'How to remove the background' }, modeBtns);
    const hint = el('p', { class: 'rb-hint', 'aria-live': 'polite' });
    const slot = el('div');   // the picture goes here on phones

    const toolBtn = (value, icon, text) => el('button', { type: 'button', 'data-tool': value }, [el('span', { class: 'rb-ico', 'aria-hidden': 'true', text: icon }), el('span', { text })]);
    const mainTool = toolBtn('main', '💧', 'Pick');
    const toolBtns = [mainTool, toolBtn('keep', '🖌️', 'Keep'), toolBtn('erase', '🧽', 'Erase'), toolBtn('pan', '✋', 'Move')];
    const tools = el('div', { class: 'rb-tools', role: 'group', 'aria-label': 'Tools' }, toolBtns);
    const undoBtn = el('button', { type: 'button', text: '↶ Undo' });
    const resetBtn = el('button', { type: 'button', text: '⟲ Start over' });
    const row = el('div', { class: 'rb-row' }, [undoBtn, resetBtn]);

    const range = (id, min, max, step, value) => el('input', { type: 'range', id, min, max, step, value });
    const tolerance = range('removeBgTolerance', 0, 100, 1, 30);
    const toleranceVal = el('span', { text: '30' });
    const connected = el('input', { type: 'checkbox', id: 'removeBgConnected', checked: '' });
    const colourOpts = el('div', { class: 'rb-opt' }, [
      el('label', { for: tolerance.id }, [el('span', { text: 'How much to remove' }), toleranceVal]),
      tolerance,
      el('small', { text: 'Move right if some background is left, left if part of the subject disappears.' }),
      el('label', { class: 'rb-check', for: connected.id }, [connected, el('span', { text: 'Only the area I tapped (not the same colour elsewhere)' })]),
    ]);
    const brushSize = range('removeBgBrush', 10, 120, 2, 44);
    const brushVal = el('span', { text: '44' });
    const brushOpts = el('div', { class: 'rb-opt' }, [
      el('label', { for: brushSize.id }, [el('span', { text: 'Brush size' }), brushVal]),
      brushSize,
      el('small', { text: 'Zoom in with two fingers for small details. Faint areas are what will be removed.' }),
    ]);

    // Finishing options (shown once there is a result).
    const label = text => el('span', { text, style: 'font-size:13px;font-weight:600;color:#0f172a' });
    const choiceStyle = 'flex:1 1 72px;min-width:0;padding:8px 6px;font-size:13px';
    const bgChoices = [['transparent', 'Transparent'], ['white', 'White'], ['colour', 'Colour'], ['blur', 'Blurred']]
      .filter(([value]) => value !== 'blur' || canFilter)
      .map(([value, text]) => el('button', { type: 'button', class: 'btn', 'data-bg': value, style: choiceStyle, text }));
    const colour = el('input', { type: 'color', value: '#4f46e5', 'aria-label': 'Background colour',
      style: 'width:44px;height:38px;padding:2px;border:1px solid #e2e8f0;border-radius:8px;background:#fff;cursor:pointer' });
    const cleanup = range('removeBgCleanup', 0, 100, 1, 0);
    const soften = range('removeBgSoften', 0, 10, 0.5, 0);
    const cleanupVal = el('span', { text: '0' }), softenVal = el('span', { text: '0 px' });
    const rangeRow = (title, inputEl, value, help) => el('div', { class: 'rb-opt' }, [
      el('label', { for: inputEl.id }, [el('span', { text: title }), value]), inputEl, el('small', { text: help }),
    ]);
    const finish = el('div', { style: 'display:none;flex-direction:column;gap:14px;padding-top:14px;border-top:1px solid #e2e8f0' }, [
      el('div', {}, [label('Background'), el('div', { style: 'display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;align-items:center' }, [...bgChoices, colour])]),
      rangeRow('Edge clean-up', cleanup, cleanupVal, 'Removes a faint halo left around the subject.'),
      rangeRow('Edge softness', soften, softenVal, 'Feathers the outline so it blends into a new background.'),
    ]);
    const editor = el('div', { class: 'rb-editor', hidden: '' }, [modes, hint, slot, tools, colourOpts, brushOpts, row, finish]);
    runBtn.before(editor);

    // ---------- state ----------
    let file = null, W = 0, H = 0, work = null, src = null, workBlob = null;
    let cutout = null, cctx = null, out = null;
    let mode = 'box', tool = 'main', background = 'transparent';
    let ops = [];                  // history: {kind:'stroke'|'seed'|'box', ...}
    let strokes = null;            // 0 = no mark, 1 = keep, 2 = erase
    let colourAlpha = null, boxAlpha = null, aiAlpha = null, base = null;
    let frame = 0, view = { s: 1, x: 0, y: 0 }, fit = 1;
    let boxController = null, boxToken = 0, aiBusy = false, loadToken = 0;

    const seeds = () => ops.filter(o => o.kind === 'seed');
    const lastBox = () => { for (let i = ops.length - 1; i >= 0; i--) if (ops[i].kind === 'box') return ops[i].rect; return null; };
    const hasStrokes = () => ops.some(o => o.kind === 'stroke');
    const hasResult = () => !!base || hasStrokes();
    const brushing = () => tool === 'keep' || tool === 'erase';
    const activeTool = () => tool !== 'main' ? tool : mode === 'colour' ? 'pick' : mode === 'box' ? 'box' : 'pan';

    // ---------- placing the picture: right column on computers, inline on phones ----------
    function place() {
      if (PHONE.matches) { slot.append(previewBlock); previewCard.style.display = 'none'; }
      else { previewCard.append(previewBlock); previewCard.style.display = previewCardDisplay; }
      layout();
    }
    PHONE.addEventListener?.('change', place);

    function layout() {
      if (!W) return;
      const sw = stage.clientWidth - 2;
      const maxH = PHONE.matches ? Math.min(window.innerHeight * 0.5, 520) : Math.min(window.innerHeight * 0.62, 480);
      fit = Math.min(sw / W, maxH / H, 3);
      canvas.style.width = `${W * fit}px`;
      canvas.style.height = `${H * fit}px`;
      setView(view.s, view.x, view.y);
    }
    if (window.ResizeObserver) new ResizeObserver(() => layout()).observe(stage);
    window.addEventListener('resize', layout);

    // ---------- zoom and pan ----------
    function setView(s, x, y) {
      s = clamp(s, 1, 8);
      const sw = stage.clientWidth, sh = stage.clientHeight;
      const lw = W * fit * s, lh = H * fit * s;
      const ox = layer.offsetLeft, oy = layer.offsetTop;
      x = lw <= sw ? (sw - lw) / 2 - ox : clamp(x, sw - ox - lw, -ox);
      y = lh <= sh ? (sh - lh) / 2 - oy : clamp(y, sh - oy - lh, -oy);
      if (s === 1) { x = 0; y = 0; }
      view = { s, x, y };
      layer.style.transform = s === 1 ? '' : `translate(${x}px, ${y}px) scale(${s})`;
    }
    // Layer's top-left on screen when not zoomed.
    function origin() {
      const r = stage.getBoundingClientRect();
      return { x: r.left + stage.clientLeft + layer.offsetLeft, y: r.top + stage.clientTop + layer.offsetTop };
    }
    function zoomAt(factor, cx, cy) {
      const r = stage.getBoundingClientRect();
      if (cx === undefined) { cx = r.left + r.width / 2; cy = r.top + r.height / 2; }
      const o = origin();
      const qx = (cx - o.x - view.x) / view.s, qy = (cy - o.y - view.y) / view.s;
      const s = clamp(view.s * factor, 1, 8);
      setView(s, cx - o.x - s * qx, cy - o.y - s * qy);
      updateCursor();
    }
    stage.addEventListener('wheel', e => {
      if (!W || !(e.ctrlKey || e.metaKey)) return;    // plain scrolling still scrolls the page
      e.preventDefault();
      zoomAt(Math.exp(-e.deltaY / 300), e.clientX, e.clientY);
    }, { passive: false });

    // ---------- loading a photo ----------
    async function loadFile(chosen) {
      const mine = ++loadToken;
      file = chosen;
      boxController?.abort();
      setStatus(status, '', 'info');
      let bitmap;
      try {
        bitmap = await createImageBitmap(chosen, { imageOrientation: 'from-image' });
      } catch {
        try { bitmap = await loadImage(URL.createObjectURL(chosen)); }
        catch {
          setStatus(status, '❌ This image could not be opened. Try a JPG or PNG photo.', 'error');
          return;
        }
      }
      if (mine !== loadToken) return;
      const bw = bitmap.width || bitmap.naturalWidth, bh = bitmap.height || bitmap.naturalHeight;
      const ratio = Math.min(1, WORK_MAX / Math.max(bw, bh));
      W = Math.max(1, Math.round(bw * ratio)); H = Math.max(1, Math.round(bh * ratio));
      work = el('canvas'); work.width = W; work.height = H;
      const wctx = work.getContext('2d', { willReadFrequently: true });
      wctx.drawImage(bitmap, 0, 0, W, H);
      bitmap.close?.();
      src = wctx.getImageData(0, 0, W, H).data;
      cutout = el('canvas'); cutout.width = W; cutout.height = H;
      cctx = cutout.getContext('2d');
      out = new ImageData(new Uint8ClampedArray(src), W, H);
      strokes = new Uint8Array(W * H);
      ops = []; colourAlpha = boxAlpha = aiAlpha = base = null;
      mask = null; maskKey = '';
      canvas.width = W; canvas.height = H;
      img.style.display = 'none'; placeholder.style.display = 'none';
      layer.hidden = false; zoomBar.hidden = false; boxEl.hidden = true;
      editor.hidden = false;
      runBtn.classList.add('rb-inline');
      switcher.reset();
      view = { s: 1, x: 0, y: 0 };
      place();
      setMode(mode);
      workBlob = await new Promise(resolve => work.toBlob(resolve, 'image/jpeg', 0.92));
    }
    input.onchange = e => { const chosen = e.target.files[0]; if (chosen) loadFile(chosen); };

    // ---------- masks ----------
    function stamp(x, y, r, val, dirty) {
      const y0 = Math.max(0, Math.floor(y - r)), y1 = Math.min(H - 1, Math.ceil(y + r));
      for (let yy = y0; yy <= y1; yy++) {
        const dy = yy - y, span = r * r - dy * dy;
        if (span < 0) continue;
        const dx = Math.sqrt(span);
        const x0 = Math.max(0, Math.ceil(x - dx)), x1 = Math.min(W - 1, Math.floor(x + dx));
        if (x0 <= x1) strokes.fill(val, yy * W + x0, yy * W + x1 + 1);
      }
      if (dirty) {
        dirty[0] = Math.min(dirty[0], Math.floor(x - r)); dirty[1] = Math.min(dirty[1], Math.floor(y - r));
        dirty[2] = Math.max(dirty[2], Math.ceil(x + r) + 1); dirty[3] = Math.max(dirty[3], Math.ceil(y + r) + 1);
      }
    }
    function strokeLine(op, from, dirty) {
      const pts = op.pts, val = op.tool === 'keep' ? 1 : 2;
      for (let i = Math.max(0, from); i < pts.length; i += 2) {
        if (i < 2) { stamp(pts[0], pts[1], op.r, val, dirty); continue; }
        const ax = pts[i - 2], ay = pts[i - 1], bx = pts[i], by = pts[i + 1];
        const steps = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / Math.max(1, op.r * 0.35)));
        for (let k = 1; k <= steps; k++) stamp(ax + (bx - ax) * k / steps, ay + (by - ay) * k / steps, op.r, val, dirty);
      }
    }
    function rebuildStrokes() {
      strokes.fill(0);
      for (const op of ops) if (op.kind === 'stroke') strokeLine(op, 0, null);
    }

    // Colour mode: pixels close to a picked colour become transparent. Pixels
    // just outside the limit fade, so the edge stays smooth.
    let fillStack = null, visited = null;
    function computeColour() {
      const picked = seeds();
      if (!picked.length) return null;
      const n = W * H, alpha = new Uint8Array(n).fill(255);
      const limit = 6 + Number(tolerance.value) * 1.9, band = Math.max(6, limit * 0.3);
      const in2 = limit * limit, out2 = (limit + band) * (limit + band);
      const shade = (i, c) => {
        const p = i * 4, dr = src[p] - c[0], dg = src[p + 1] - c[1], db = src[p + 2] - c[2];
        return dr * dr + dg * dg + db * db;
      };
      const fade = d2 => Math.round((Math.sqrt(d2) - limit) / band * 255);
      if (!connected.checked) {
        for (let i = 0; i < n; i++) {
          let best = Infinity;
          for (const s of picked) { const d = shade(i, s.c); if (d < best) best = d; }
          if (best <= in2) alpha[i] = 0;
          else if (best < out2) alpha[i] = Math.min(alpha[i], fade(best));
        }
        return alpha;
      }
      if (!fillStack || fillStack.length < n) { fillStack = new Int32Array(n); visited = new Uint8Array(n); }
      for (const s of picked) {
        visited.fill(0, 0, n);
        let top = 0;
        const start = Math.floor(s.y) * W + Math.floor(s.x);
        fillStack[top++] = start; visited[start] = 1;
        while (top) {
          const i = fillStack[--top];
          const d = shade(i, s.c);
          if (d > in2) {
            if (d < out2) alpha[i] = Math.min(alpha[i], fade(d));
            continue;        // edge pixel: fade it, but don't spread past it
          }
          alpha[i] = 0;
          const x = i % W;
          if (x > 0 && !visited[i - 1]) { visited[i - 1] = 1; fillStack[top++] = i - 1; }
          if (x < W - 1 && !visited[i + 1]) { visited[i + 1] = 1; fillStack[top++] = i + 1; }
          if (i >= W && !visited[i - W]) { visited[i - W] = 1; fillStack[top++] = i - W; }
          if (i + W < n && !visited[i + W]) { visited[i + W] = 1; fillStack[top++] = i + W; }
        }
      }
      return alpha;
    }
    function sampleColour(x, y) {
      let r = 0, g = 0, b = 0, count = 0;
      for (let yy = Math.max(0, y - 2); yy <= Math.min(H - 1, y + 2); yy++)
        for (let xx = Math.max(0, x - 2); xx <= Math.min(W - 1, x + 2); xx++) {
          const p = (yy * W + xx) * 4; r += src[p]; g += src[p + 1]; b += src[p + 2]; count++;
        }
      return [r / count, g / count, b / count];
    }

    // Final alpha = brush marks, else the current mode's mask, else fully kept.
    function compose(x0 = 0, y0 = 0, x1 = W, y1 = H) {
      if (!out) return;
      x0 = clamp(x0, 0, W); x1 = clamp(x1, 0, W); y0 = clamp(y0, 0, H); y1 = clamp(y1, 0, H);
      if (x1 <= x0 || y1 <= y0) return;
      const px = out.data;
      for (let y = y0; y < y1; y++) {
        for (let i = y * W + x0, end = y * W + x1; i < end; i++) {
          const s = strokes[i];
          px[i * 4 + 3] = s === 1 ? 255 : s === 2 ? 0 : base ? base[i] : 255;
        }
      }
      cctx.putImageData(out, 0, 0, x0, y0, x1 - x0, y1 - y0);
      maskKey = '';
      schedule();
    }
    function setBase() {
      base = mode === 'colour' ? colourAlpha : mode === 'box' ? boxAlpha : aiAlpha;
      compose();
      updateUI();
    }

    // ---------- Box mode: ask the server ----------
    function hintsBlob() {
      if (!hasStrokes()) return Promise.resolve(null);
      const c = el('canvas'); c.width = W; c.height = H;
      const hctx = c.getContext('2d');
      const data = hctx.createImageData(W, H), px = data.data;
      for (let i = 0; i < W * H; i++) {
        const v = strokes[i] === 1 ? 255 : strokes[i] === 2 ? 0 : 128;
        px[i * 4] = px[i * 4 + 1] = px[i * 4 + 2] = v; px[i * 4 + 3] = 255;
      }
      hctx.putImageData(data, 0, 0);
      return new Promise(resolve => c.toBlob(resolve, 'image/png'));
    }
    async function runBox() {
      const rect = lastBox();
      boxController?.abort();
      if (!rect) { boxAlpha = null; badge.show(false); if (mode === 'box') setBase(); return; }
      const mine = ++boxToken;
      boxController = new AbortController();
      badge.show(true);
      try {
        if (!workBlob) workBlob = await new Promise(resolve => work.toBlob(resolve, 'image/jpeg', 0.92));
        const fd = new FormData();
        fd.append('file', workBlob, 'photo.jpg');
        for (const k of ['x', 'y', 'w', 'h']) fd.append(k, String(Math.round(rect[k])));
        const hints = await hintsBlob();
        if (hints) fd.append('hints', hints, 'hints.png');
        const res = await fetch('/api/remove-bg/box', { method: 'POST', body: fd, signal: boxController.signal });
        if (!res.ok) throw new Error(await errorFrom(res, 'The cut-out failed.'));
        const maskImg = await loadImage(URL.createObjectURL(await res.blob()));
        if (mine !== boxToken) return;
        const c = el('canvas'); c.width = W; c.height = H;
        const mctx = c.getContext('2d', { willReadFrequently: true });
        mctx.drawImage(maskImg, 0, 0, W, H);
        URL.revokeObjectURL(maskImg.src);
        const px = mctx.getImageData(0, 0, W, H).data;
        const alpha = new Uint8Array(W * H);
        for (let i = 0; i < alpha.length; i++) alpha[i] = px[i * 4];
        boxAlpha = alpha;
        if (status.textContent.startsWith('❌')) setStatus(status, '', 'info');
        if (mode === 'box') setBase();
      } catch (err) {
        if (err.name === 'AbortError') return;
        setStatus(status, '❌ ' + err.message, 'error');
      } finally {
        if (mine === boxToken) badge.show(false);
      }
    }

    // ---------- AI mode ----------
    async function runAi() {
      if (!file || aiBusy) return;
      aiBusy = true; updateUI();
      runBtn.textContent = 'Removing background (AI)…';
      setStatus(status, '', 'info');
      badge.show(true);
      const fd = new FormData(); fd.append('file', file);
      try {
        const res = await fetch('/api/remove-bg', { method: 'POST', body: fd });
        if (!res.ok) throw new Error(await errorFrom(res, 'Background removal failed.'));
        const result = await loadImage(URL.createObjectURL(await res.blob()));
        const c = el('canvas'); c.width = W; c.height = H;
        const actx = c.getContext('2d', { willReadFrequently: true });
        actx.drawImage(result, 0, 0, W, H);
        URL.revokeObjectURL(result.src);
        const px = actx.getImageData(0, 0, W, H).data;
        const alpha = new Uint8Array(W * H);
        for (let i = 0; i < alpha.length; i++) alpha[i] = px[i * 4 + 3];
        aiAlpha = alpha;
        if (mode === 'ai') setBase();
      } catch (err) {
        setStatus(status, '❌ ' + err.message, 'error');
      } finally {
        aiBusy = false; badge.show(false); updateUI();
      }
    }

    // ---------- rendering (with background, clean-up and softness) ----------
    let mask = null, maskKey = '', feather = null, ghost = true;
    function processedCutout() {
      const key = cleanup.value;
      if (mask && maskKey === key) return mask;
      mask = mask || el('canvas');
      if (mask.width !== W || mask.height !== H) { mask.width = W; mask.height = H; }
      const mctx = mask.getContext('2d', { willReadFrequently: true });
      mctx.clearRect(0, 0, W, H);
      mctx.drawImage(cutout, 0, 0);
      const threshold = Number(key) / 100 * 0.6;
      if (threshold > 0) {
        const data = mctx.getImageData(0, 0, W, H), px = data.data;
        for (let i = 3; i < px.length; i += 4) {
          const a = px[i] / 255;
          px[i] = a <= threshold ? 0 : Math.round(Math.min(1, (a - threshold) / (1 - threshold)) * 255);
        }
        mctx.putImageData(data, 0, 0);
      }
      maskKey = key;
      return mask;
    }
    function featheredCutout() {
      const sharp = processedCutout();
      const softness = Number(soften.value);
      if (!(softness > 0) || !canFilter) return sharp;
      feather = feather || el('canvas');
      if (feather.width !== W || feather.height !== H) { feather.width = W; feather.height = H; }
      const fctx = feather.getContext('2d');
      fctx.clearRect(0, 0, W, H);
      fctx.drawImage(sharp, 0, 0);
      fctx.globalCompositeOperation = 'destination-in';
      fctx.filter = `blur(${softness * Math.max(1, Math.max(W, H) / 1000)}px)`;
      fctx.drawImage(sharp, 0, 0);
      fctx.filter = 'none';
      fctx.globalCompositeOperation = 'source-over';
      return feather;
    }
    function render() {
      frame = 0;
      if (!work) return;
      ctx.save();
      ctx.clearRect(0, 0, W, H);
      if (switcher.before || !hasResult()) { ctx.drawImage(work, 0, 0); ctx.restore(); return; }
      if (background === 'white' || background === 'colour') {
        ctx.fillStyle = background === 'white' ? '#ffffff' : colour.value;
        ctx.fillRect(0, 0, W, H);
      } else if (background === 'blur') {
        const radius = Math.max(4, Math.round(Math.max(W, H) / 60));
        ctx.filter = `blur(${radius}px)`;
        ctx.drawImage(work, -radius * 2, -radius * 2, W + radius * 4, H + radius * 4);
        ctx.filter = 'none';
      }
      // While brushing, show the removed parts faintly so you can see where to paint back.
      if (ghost && brushing()) { ctx.globalAlpha = 0.3; ctx.drawImage(work, 0, 0); ctx.globalAlpha = 1; }
      ctx.drawImage(featheredCutout(), 0, 0);
      ctx.restore();
    }
    const schedule = () => { if (!frame) frame = requestAnimationFrame(render); };

    // ---------- UI state ----------
    const HINTS = {
      colour: [
        'Tap the background you want to remove.',
        'Tap any background that is left to remove it too. Use the slider below if too much or too little is removed.',
      ],
      box: [
        'Drag a box around the subject, a little bigger than it.',
        'Not quite right? Paint over spots with Keep or Erase. Drawing a new box uses your marks as hints.',
      ],
      ai: [
        'Press Remove background below and AI finds the subject. It takes a few seconds.',
        'Touch up any spots with Keep or Erase.',
      ],
    };
    function updateUI() {
      for (const b of modeBtns) b.setAttribute('aria-pressed', String(b.dataset.mode === mode));
      const done = mode === 'colour' ? seeds().length > 0 : mode === 'box' ? !!lastBox() : !!aiAlpha;
      hint.textContent = HINTS[mode][done ? 1 : 0];
      mainTool.hidden = mode === 'ai';
      mainTool.querySelector('.rb-ico').textContent = mode === 'colour' ? '💧' : '⬚';
      mainTool.lastChild.textContent = mode === 'colour' ? 'Pick' : 'Box';
      if (mode === 'ai' && tool === 'main') tool = 'pan';
      for (const b of toolBtns) b.setAttribute('aria-pressed', String(b.dataset.tool === tool));
      colourOpts.hidden = mode !== 'colour';
      brushOpts.hidden = !brushing();
      undoBtn.disabled = !ops.some(undoable);
      resetBtn.disabled = !hasResult() && !ops.length;
      finish.style.display = hasResult() ? 'flex' : 'none';
      if (note) note.style.display = mode === 'ai' && !aiAlpha ? '' : 'none';
      boxEl.hidden = !(mode === 'box' && lastBox() && !hasResult());
      const t = activeTool();
      stage.style.cursor = t === 'pick' || t === 'box' ? 'crosshair' : t === 'pan' ? 'grab' : 'none';
      if (!brushing()) cursor.hidden = true;
      if (aiBusy) { runBtn.disabled = true; return; }
      if (mode === 'ai' && !aiAlpha) { runBtn.textContent = 'Remove background (AI)'; runBtn.disabled = !file; }
      else { runBtn.textContent = 'Download PNG'; runBtn.disabled = !hasResult(); }
      schedule();
    }
    const undoable = o => o.kind === 'stroke' || (o.kind === 'seed' && mode === 'colour') || (o.kind === 'box' && mode === 'box');

    function setMode(next) {
      mode = next;
      tool = mode === 'ai' ? 'pan' : 'main';
      if (mode === 'colour') colourAlpha = computeColour();
      if (mode === 'box' && lastBox() && !boxAlpha) runBox();
      showBox(lastBox());
      setBase();
    }
    modeBtns.forEach(b => b.onclick = () => { if (b.dataset.mode !== mode) setMode(b.dataset.mode); });
    toolBtns.forEach(b => b.onclick = () => { tool = b.dataset.tool; updateUI(); });

    undoBtn.onclick = () => {
      for (let i = ops.length - 1; i >= 0; i--) {
        if (!undoable(ops[i])) continue;
        const [op] = ops.splice(i, 1);
        if (op.kind === 'stroke') rebuildStrokes();
        if (op.kind === 'seed') colourAlpha = computeColour();
        if (op.kind === 'box') { showBox(lastBox()); runBox(); }
        setBase();
        return;
      }
    };
    resetBtn.onclick = () => {
      const kind = mode === 'colour' ? 'seed' : mode === 'box' ? 'box' : '';
      ops = ops.filter(o => o.kind !== 'stroke' && o.kind !== kind);
      rebuildStrokes();
      if (mode === 'colour') colourAlpha = null;
      if (mode === 'box') { boxController?.abort(); boxAlpha = null; badge.show(false); showBox(null); }
      if (mode === 'ai') aiAlpha = null;
      tool = mode === 'ai' ? 'pan' : 'main';
      setView(1, 0, 0);
      setBase();
    };

    let colourFrame = 0;
    tolerance.oninput = () => {
      toleranceVal.textContent = tolerance.value;
      if (!colourFrame) colourFrame = requestAnimationFrame(() => { colourFrame = 0; colourAlpha = computeColour(); setBase(); });
    };
    connected.onchange = () => { colourAlpha = computeColour(); setBase(); };
    brushSize.oninput = () => { brushVal.textContent = brushSize.value; updateCursor(); };

    function paintChoices() {
      for (const btn of bgChoices) {
        const on = btn.dataset.bg === background;
        btn.classList.toggle('btn-primary', on);
        btn.setAttribute('aria-pressed', String(on));
      }
      colour.style.outline = background === 'colour' ? '2px solid #4f46e5' : 'none';
    }
    bgChoices.forEach(btn => btn.onclick = () => { background = btn.dataset.bg; paintChoices(); schedule(); });
    colour.oninput = () => { background = 'colour'; paintChoices(); schedule(); };
    cleanup.oninput = () => { cleanupVal.textContent = cleanup.value; maskKey = ''; schedule(); };
    soften.oninput = () => { softenVal.textContent = `${soften.value} px`; schedule(); };
    paintChoices();

    // ---------- touch, pen and mouse on the picture ----------
    const pointers = new Map();
    let gesture = null, lastPointer = null;
    function toImage(e) {
      const r = canvas.getBoundingClientRect();
      return { x: (e.clientX - r.left) / r.width * W, y: (e.clientY - r.top) / r.height * H, k: W / r.width };
    }
    function showBox(rect) {
      if (!rect) { boxEl.hidden = true; return; }
      boxEl.style.left = `${rect.x / W * 100}%`; boxEl.style.top = `${rect.y / H * 100}%`;
      boxEl.style.width = `${rect.w / W * 100}%`; boxEl.style.height = `${rect.h / H * 100}%`;
      boxEl.hidden = false;
    }
    function updateCursor(e = lastPointer) {
      if (!e || !brushing() || !W) { cursor.hidden = true; return; }
      const p = toImage(e);
      if (p.x < 0 || p.y < 0 || p.x > W || p.y > H) { cursor.hidden = true; return; }
      const d = Number(brushSize.value) / view.s;       // layer pixels
      cursor.style.width = cursor.style.height = `${d}px`;
      cursor.style.left = `${p.x / W * 100}%`; cursor.style.top = `${p.y / H * 100}%`;
      cursor.className = `rb-cursor ${tool}`;
      cursor.hidden = false;
    }
    function cancelGesture() {
      if (gesture?.type === 'stroke') {
        ops = ops.filter(o => o !== gesture.op);
        rebuildStrokes(); compose(); updateUI();
      }
      if (gesture?.type === 'box') showBox(lastBox());
      gesture = null;
    }
    function startPinch() {
      const [a, b] = [...pointers.values()];
      gesture = { type: 'pinch', d0: Math.hypot(a.x - b.x, a.y - b.y) || 1, m0: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, v0: { ...view }, o: origin() };
    }
    function movePinch() {
      const [a, b] = [...pointers.values()];
      const g = gesture, m = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const s = clamp(g.v0.s * Math.hypot(a.x - b.x, a.y - b.y) / g.d0, 1, 8);
      const qx = (g.m0.x - g.o.x - g.v0.x) / g.v0.s, qy = (g.m0.y - g.o.y - g.v0.y) / g.v0.s;
      setView(s, m.x - g.o.x - s * qx, m.y - g.o.y - s * qy);
    }

    stage.addEventListener('pointerdown', e => {
      if (!W || e.button > 0) return;
      e.preventDefault();
      try { stage.setPointerCapture(e.pointerId); } catch {}
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      lastPointer = e;
      if (pointers.size === 2) { cancelGesture(); startPinch(); return; }
      if (pointers.size > 2 || switcher.before) return;
      const p = toImage(e), t = activeTool();
      if (t === 'pick') gesture = { type: 'tap', sx: e.clientX, sy: e.clientY, p };
      else if (t === 'pan') gesture = { type: 'pan', sx: e.clientX, sy: e.clientY, v0: { ...view } };
      else if (t === 'box') { gesture = { type: 'box', a: p, sx: e.clientX, sy: e.clientY }; showBox({ x: clamp(p.x, 0, W), y: clamp(p.y, 0, H), w: 0, h: 0 }); }
      else {
        const op = { kind: 'stroke', tool: t, r: Math.max(1, Number(brushSize.value) / 2 * p.k), pts: [p.x, p.y] };
        ops.push(op);
        gesture = { type: 'stroke', op };
        const dirty = [Infinity, Infinity, -Infinity, -Infinity];
        strokeLine(op, 0, dirty);
        compose(...dirty);
        updateCursor(e);
      }
    });
    stage.addEventListener('pointermove', e => {
      if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      lastPointer = e;
      if (e.pointerType === 'mouse' || gesture?.type === 'stroke') updateCursor(e);
      if (!gesture) return;
      if (gesture.type === 'pinch') { if (pointers.size >= 2) movePinch(); return; }
      if (!pointers.has(e.pointerId)) return;
      const moved = Math.hypot(e.clientX - (gesture.sx ?? e.clientX), e.clientY - (gesture.sy ?? e.clientY));
      if (gesture.type === 'tap' && moved > 10) gesture = { type: 'pan', sx: gesture.sx, sy: gesture.sy, v0: { ...view } };
      if (gesture.type === 'pan') setView(gesture.v0.s, gesture.v0.x + e.clientX - gesture.sx, gesture.v0.y + e.clientY - gesture.sy);
      else if (gesture.type === 'box') {
        const p = toImage(e), a = gesture.a;
        const x0 = clamp(Math.min(a.x, p.x), 0, W), y0 = clamp(Math.min(a.y, p.y), 0, H);
        const x1 = clamp(Math.max(a.x, p.x), 0, W), y1 = clamp(Math.max(a.y, p.y), 0, H);
        gesture.rect = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
        showBox(gesture.rect);
      } else if (gesture.type === 'stroke') {
        const p = toImage(e), op = gesture.op, from = op.pts.length;
        op.pts.push(p.x, p.y);
        const dirty = [Infinity, Infinity, -Infinity, -Infinity];
        strokeLine(op, from, dirty);
        compose(...dirty);
      }
    });
    function endPointer(e) {
      if (!pointers.delete(e.pointerId)) return;
      const g = gesture;
      if (!g) return;
      if (g.type === 'pinch') { if (pointers.size < 2) gesture = pointers.size ? { type: 'none' } : null; return; }
      if (g.type === 'none') { if (!pointers.size) gesture = null; return; }
      gesture = null;
      if (e.type === 'pointercancel') { gesture = g; cancelGesture(); return; }
      if (g.type === 'tap') {
        const x = Math.floor(g.p.x), y = Math.floor(g.p.y);
        if (x < 0 || y < 0 || x >= W || y >= H) return;
        ops.push({ kind: 'seed', x, y, c: sampleColour(x, y) });
        colourAlpha = computeColour();
        setBase();
      } else if (g.type === 'box') {
        const r = g.rect, k = toImage(e).k;
        if (!r || r.w / k < 16 || r.h / k < 16) { showBox(lastBox()); setStatus(status, lastBox() ? '' : 'Drag your finger to draw a box around the subject.', 'info'); return; }
        setStatus(status, '', 'info');
        ops.push({ kind: 'box', rect: r });
        runBox();
        updateUI();
      } else if (g.type === 'stroke') {
        updateUI();
        if (e.pointerType !== 'mouse') cursor.hidden = true;
      }
    }
    stage.addEventListener('pointerup', endPointer);
    stage.addEventListener('pointercancel', endPointer);
    stage.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse' && !pointers.size) cursor.hidden = true; });
    stage.addEventListener('contextmenu', e => { if (W) e.preventDefault(); });

    // ---------- main button: AI cut-out, or download ----------
    runBtn.onclick = () => {
      if (!file) return;
      if (mode === 'ai' && !aiAlpha) { runAi(); return; }
      if (!hasResult()) return;
      const wasBefore = switcher.before;
      if (wasBefore) switcher.reset();
      ghost = false; render();
      canvas.toBlob(blob => {
        ghost = true; render();
        if (!blob) { setStatus(status, '❌ The image could not be saved.', 'error'); return; }
        download(blob, 'no-bg.png');
        setStatus(status, '✅ Downloaded no-bg.png', 'ok');
      }, 'image/png');
    };

    updateUI();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setup);
  else setup();
})();

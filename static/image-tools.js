/* Live previews for Enhance Image and Remove Background.
 *
 * Kept separate from index.html on purpose: it finds the existing tool views by
 * their element ids, adds the preview controls, and takes over their upload and
 * button handlers. Nothing is downloaded until the person presses Download.
 *
 *  - Enhance Image: every slider change re-renders a smaller copy of the photo
 *    on the server (the exact same Pillow adjustments the download uses), so the
 *    preview is what the downloaded file will look like.
 *  - Remove Background: the AI cut-out runs once; background colour, edge
 *    clean-up and edge softness are then applied in the browser instantly, and
 *    the download is exactly the canvas shown in the preview.
 */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const PREVIEW_MAX = 1200;     // matches ENHANCE_PREVIEW_MAX on the server
  const DEBOUNCE_MS = 180;

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === 'style') node.style.cssText = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    for (const child of [].concat(children)) if (child) node.append(child);
    return node;
  }
  async function errorFrom(res, fallback) {
    try { const data = await res.json(); return data.error || fallback; } catch { return fallback; }
  }
  function download(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = el('a', { href: url, download: filename });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }
  function setStatus(node, text, kind) {
    node.textContent = text;
    node.style.color = kind === 'error' ? '#dc2626' : kind === 'ok' ? '#059669' : '#64748b';
  }
  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('This image could not be displayed in the browser.'));
      img.src = src;
    });
  }

  // "After | Before" switch shown above a preview.
  function compareSwitch(onChange) {
    const style = 'border:none;background:transparent;padding:5px 12px;border-radius:999px;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer;color:#475569';
    const after = el('button', { type: 'button', style, 'aria-pressed': 'true', text: 'After' });
    const before = el('button', { type: 'button', style, 'aria-pressed': 'false', text: 'Before' });
    const wrap = el('div', { role: 'group', 'aria-label': 'Compare with the original',
      style: 'display:inline-flex;gap:2px;padding:3px;border-radius:999px;background:#f1f5f9;border:1px solid #e2e8f0' }, [after, before]);
    let showingBefore = false;
    const paint = () => {
      for (const [btn, on] of [[after, !showingBefore], [before, showingBefore]]) {
        btn.setAttribute('aria-pressed', String(on));
        btn.style.background = on ? '#fff' : 'transparent';
        btn.style.color = on ? '#4f46e5' : '#475569';
        btn.style.boxShadow = on ? '0 1px 3px rgba(15,23,42,.12)' : 'none';
      }
    };
    after.onclick = () => { showingBefore = false; paint(); onChange(false); };
    before.onclick = () => { showingBefore = true; paint(); onChange(true); };
    paint();
    return { node: wrap, reset() { showingBefore = false; paint(); }, get before() { return showingBefore; } };
  }

  // Header row: title + compare switch, replacing the old preview heading.
  function previewHeader(heading, title, switcher) {
    const row = el('div', { style: 'display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px;flex-wrap:wrap' });
    heading.textContent = title;
    heading.style.marginBottom = '0';
    heading.replaceWith(row);
    row.append(heading, switcher.node);
    return row;
  }

  function busyBadge(box) {
    box.style.position = 'relative';
    const badge = el('div', { 'aria-live': 'polite', text: 'Updating preview…',
      style: 'position:absolute;top:10px;right:10px;background:rgba(15,23,42,.72);color:#fff;font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px;display:none' });
    box.append(badge);
    return { show(on) { badge.style.display = on ? 'block' : 'none'; } };
  }

  // Shrink the photo once in the browser so each preview request is small.
  async function previewSource(file) {
    try {
      const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
      const ratio = Math.min(1, PREVIEW_MAX / Math.max(bitmap.width, bitmap.height));
      const canvas = el('canvas');
      canvas.width = Math.max(1, Math.round(bitmap.width * ratio));
      canvas.height = Math.max(1, Math.round(bitmap.height * ratio));
      canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      bitmap.close?.();
      // JPEG keeps each request small; PNG keeps transparent pixels for PNG/WebP/GIF.
      const keepAlpha = /png|webp|gif/i.test(file.type);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, keepAlpha ? 'image/png' : 'image/jpeg', 0.92));
      return blob || file;
    } catch {
      return file;  // the server downsizes it for the preview anyway
    }
  }

  // ======================= Enhance Image =======================
  function setupEnhance() {
    const input = $('enhanceFileInput'), img = $('enhancePreviewImg'), placeholder = $('enhancePreviewPlaceholder');
    const runBtn = $('runEnhanceBtn'), status = $('enhanceStatus'), fileName = $('enhanceFileName');
    if (!input || !img || !runBtn) return;
    const names = ['Brightness', 'Contrast', 'Color', 'Sharpness'];
    const sliders = names.map(n => $(`slider${n}`));
    if (sliders.some(s => !s)) return;

    const box = img.parentElement;
    const badge = busyBadge(box);
    const switcher = compareSwitch(showBefore => show(showBefore));
    const heading = box.parentElement.querySelector('h3');
    if (heading) previewHeader(heading, 'Live preview', switcher);
    const hint = el('p', { style: 'margin-top:10px;font-size:12.5px;color:#64748b;text-align:center',
      text: 'The preview updates as you move the sliders. Nothing is downloaded until you press Apply & Download.' });
    box.after(hint);

    const resetBtn = el('button', { type: 'button', class: 'btn', style: 'width:100%;margin-bottom:10px', text: 'Reset sliders' });
    runBtn.before(resetBtn);

    let file = null, source = null, originalUrl = '', resultUrl = '', controller = null, timer = 0, token = 0;

    const params = () => Object.fromEntries(names.map((n, i) => [n.toLowerCase(), sliders[i].value]));
    const isNeutral = () => sliders.every(s => Number(s.value) === 1);
    function show(before) {
      const url = before || !resultUrl ? originalUrl : resultUrl;
      if (!url) return;
      img.src = url; img.style.display = 'block'; placeholder.style.display = 'none';
      img.alt = before ? 'Original image' : 'Preview of the enhanced image';
    }
    function setResult(url) {
      if (resultUrl && resultUrl !== originalUrl) URL.revokeObjectURL(resultUrl);
      resultUrl = url;
      if (!switcher.before) show(false);
    }

    async function refresh() {
      if (!file) return;
      if (isNeutral()) { controller?.abort(); badge.show(false); setResult(originalUrl); return; }
      controller?.abort();
      controller = new AbortController();
      const mine = ++token;
      const fd = new FormData();
      fd.append('file', source || file, 'preview.png');
      for (const [key, value] of Object.entries(params())) fd.append(key, value);
      badge.show(true);
      try {
        const res = await fetch('/api/enhance-image/preview', { method: 'POST', body: fd, signal: controller.signal });
        if (!res.ok) throw new Error(await errorFrom(res, 'The preview could not be updated.'));
        const blob = await res.blob();
        if (mine !== token) return;
        setResult(URL.createObjectURL(blob));
        if (status.dataset.preview === 'error') { setStatus(status, '', 'info'); delete status.dataset.preview; }
      } catch (err) {
        if (err.name === 'AbortError') return;
        setStatus(status, '❌ ' + err.message, 'error'); status.dataset.preview = 'error';
      } finally {
        if (mine === token) badge.show(false);
      }
    }
    const schedule = () => { clearTimeout(timer); timer = setTimeout(refresh, DEBOUNCE_MS); };

    names.forEach((name, i) => {
      sliders[i].oninput = e => { $(`val${name}`).textContent = parseFloat(e.target.value).toFixed(1); schedule(); };
    });
    resetBtn.onclick = () => {
      names.forEach((name, i) => { sliders[i].value = '1.0'; $(`val${name}`).textContent = '1.0'; });
      schedule();
    };

    input.onchange = async e => {
      const chosen = e.target.files[0];
      if (!chosen) return;
      file = chosen; source = null; token++; controller?.abort();
      fileName.textContent = file.name;
      runBtn.disabled = false;
      setStatus(status, '', 'info');
      switcher.reset();
      if (originalUrl) URL.revokeObjectURL(originalUrl);
      originalUrl = URL.createObjectURL(file);
      resultUrl = '';
      show(false);
      source = await previewSource(file);
      refresh();
    };
    // Full-resolution download. This file replaces index.html's onchange, so the
    // original click handler never saw the picked file and did nothing.
    runBtn.onclick = async () => {
      if (!file) return;
      runBtn.disabled = true; runBtn.textContent = 'Processing…'; setStatus(status, '', 'info');
      const fd = new FormData();
      fd.append('file', file);
      Object.entries(params()).forEach(([k, v]) => fd.append(k, v));
      try {
        const res = await fetch('/api/enhance-image', { method: 'POST', body: fd });
        if (!res.ok) throw new Error(await errorFrom(res, 'Enhancement failed.'));
        const blob = await res.blob();
        if (window.pdfSuiteSaveFile) await window.pdfSuiteSaveFile(blob, 'enhanced.jpg'); else download(blob, 'enhanced.jpg');
        setStatus(status, '✅ Enhanced image downloaded.', 'ok');
      } catch (err) {
        setStatus(status, '❌ ' + err.message, 'error');
      } finally {
        runBtn.disabled = false; runBtn.textContent = 'Apply & Download';
      }
    };
  }

  // ======================= Remove Background =======================
  function setupRemoveBg() {
    const input = $('removeBgFileInput'), img = $('removeBgPreviewImg'), placeholder = $('removeBgPreviewPlaceholder');
    const runBtn = $('runRemoveBgBtn'), status = $('removeBgStatus'), fileName = $('removeBgFileName');
    if (!input || !img || !runBtn) return;

    const box = img.parentElement;
    const canvas = el('canvas', { 'aria-label': 'Preview of the image without its background',
      style: 'max-width:100%;max-height:400px;display:none;object-fit:contain' });
    img.after(canvas);
    const ctx = canvas.getContext('2d');
    const canFilter = typeof ctx.filter === 'string';
    const switcher = compareSwitch(() => render());
    const heading = box.parentElement.querySelector('h3');
    if (heading) previewHeader(heading, 'Live preview', switcher);

    // ---- controls (hidden until the cut-out is ready) ----
    const label = text => el('span', { text, style: 'font-size:13px;font-weight:600;color:#0f172a' });
    const choiceStyle = 'flex:1 1 72px;min-width:0;padding:8px 6px;font-size:13px';
    const bgChoices = [['transparent', 'Transparent'], ['white', 'White'], ['colour', 'Colour'], ['blur', 'Blurred']]
      .filter(([value]) => value !== 'blur' || canFilter)
      .map(([value, text]) => el('button', { type: 'button', class: 'btn', 'data-bg': value, style: choiceStyle, text }));
    const colour = el('input', { type: 'color', value: '#4f46e5', 'aria-label': 'Background colour',
      style: 'width:44px;height:38px;padding:2px;border:1px solid #e2e8f0;border-radius:8px;background:#fff;cursor:pointer' });
    const slider = (id, min, max, step, value) => el('input', { type: 'range', id, min, max, step, value, style: 'width:100%' });
    const cleanup = slider('removeBgCleanup', 0, 100, 1, 0);
    const soften = slider('removeBgSoften', 0, 10, 0.5, 0);
    const cleanupVal = el('span', { text: '0' }), softenVal = el('span', { text: '0 px' });
    const rangeRow = (title, input, value, help) => el('div', {}, [
      el('label', { for: input.id, style: 'display:flex;justify-content:space-between;font-size:13px;font-weight:600;margin-bottom:4px' }, [el('span', { text: title }), value]),
      input,
      el('div', { text: help, style: 'font-size:11.5px;color:#94a3b8;margin-top:2px' }),
    ]);
    const downloadBtn = el('button', { type: 'button', class: 'btn btn-success', style: 'width:100%;padding:12px;font-size:16px', text: 'Download PNG' });
    const panel = el('div', { style: 'display:none;flex-direction:column;gap:14px;margin-top:18px;padding-top:18px;border-top:1px solid #e2e8f0' }, [
      el('div', {}, [label('Background'), el('div', { style: 'display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;align-items:center' }, [...bgChoices, colour])]),
      rangeRow('Edge clean-up', cleanup, cleanupVal, 'Removes faint halo left around the subject.'),
      rangeRow('Edge softness', soften, softenVal, 'Feathers the outline so it blends into a new background.'),
      downloadBtn,
    ]);
    runBtn.after(panel);

    let file = null, originalUrl = '', original = null, cutout = null, mask = null, maskKey = '', background = 'transparent', frame = 0;

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

    // Cut-out with the edge clean-up applied to its alpha channel.
    function processedCutout() {
      const key = cleanup.value;
      if (mask && maskKey === key) return mask;
      const w = cutout.naturalWidth, h = cutout.naturalHeight;
      mask = mask || el('canvas');
      mask.width = w; mask.height = h;
      const mctx = mask.getContext('2d', { willReadFrequently: true });
      mctx.clearRect(0, 0, w, h);
      mctx.drawImage(cutout, 0, 0);
      const threshold = Number(key) / 100 * 0.6;   // up to 60% of the alpha range
      if (threshold > 0) {
        const data = mctx.getImageData(0, 0, w, h);
        const px = data.data;
        for (let i = 3; i < px.length; i += 4) {
          const a = px[i] / 255;
          px[i] = a <= threshold ? 0 : Math.round(Math.min(1, (a - threshold) / (1 - threshold)) * 255);
        }
        mctx.putImageData(data, 0, 0);
      }
      maskKey = key;
      return mask;
    }

    // Soften only the outline: keep the subject sharp and multiply its alpha
    // by a blurred copy of itself, so the edge fades inwards.
    let feather = null;
    function featheredCutout(w, h) {
      const sharp = processedCutout();
      const softness = Number(soften.value);
      if (!(softness > 0) || !canFilter) return sharp;
      feather = feather || el('canvas');
      feather.width = w; feather.height = h;
      const fctx = feather.getContext('2d');
      fctx.clearRect(0, 0, w, h);
      fctx.drawImage(sharp, 0, 0);
      fctx.globalCompositeOperation = 'destination-in';
      fctx.filter = `blur(${softness * Math.max(1, Math.max(w, h) / 1000)}px)`;
      fctx.drawImage(sharp, 0, 0);
      fctx.filter = 'none';
      fctx.globalCompositeOperation = 'source-over';
      return feather;
    }

    function render() {
      frame = 0;
      if (switcher.before || !cutout) {
        canvas.style.display = 'none';
        if (originalUrl) { img.src = originalUrl; img.style.display = 'block'; placeholder.style.display = 'none'; }
        return;
      }
      const w = cutout.naturalWidth, h = cutout.naturalHeight;
      canvas.width = w; canvas.height = h;
      ctx.save();
      ctx.clearRect(0, 0, w, h);
      if (background === 'white' || background === 'colour') {
        ctx.fillStyle = background === 'white' ? '#ffffff' : colour.value;
        ctx.fillRect(0, 0, w, h);
      } else if (background === 'blur' && original) {
        const radius = Math.max(4, Math.round(Math.max(w, h) / 60));
        ctx.filter = `blur(${radius}px)`;
        // Draw a little larger so the blurred edges don't fade to transparent.
        ctx.drawImage(original, -radius * 2, -radius * 2, w + radius * 4, h + radius * 4);
        ctx.filter = 'none';
      }
      ctx.drawImage(featheredCutout(w, h), 0, 0);
      ctx.restore();
      img.style.display = 'none'; placeholder.style.display = 'none';
      canvas.style.display = 'block';
    }
    const schedule = () => { if (!frame) frame = requestAnimationFrame(render); };

    function resetResult() {
      cutout = null; mask = null; maskKey = '';
      panel.style.display = 'none';
      canvas.style.display = 'none';
      switcher.reset();
    }

    input.onchange = async e => {
      const chosen = e.target.files[0];
      if (!chosen) return;
      file = chosen; resetResult();
      fileName.textContent = file.name;
      runBtn.disabled = false;
      setStatus(status, '', 'info');
      if (originalUrl) URL.revokeObjectURL(originalUrl);
      originalUrl = URL.createObjectURL(file);
      original = null;
      try { original = await loadImage(originalUrl); render(); }
      catch { img.style.display = 'none'; placeholder.style.display = 'block'; placeholder.textContent = 'Preview will appear after processing'; }
    };

    runBtn.onclick = async () => {
      if (!file) return;
      runBtn.disabled = true; runBtn.textContent = 'Removing background (AI)…';
      setStatus(status, '', 'info');
      const fd = new FormData(); fd.append('file', file);
      try {
        const res = await fetch('/api/remove-bg', { method: 'POST', body: fd });
        if (!res.ok) throw new Error(await errorFrom(res, 'Background removal failed.'));
        const url = URL.createObjectURL(await res.blob());
        const loaded = await loadImage(url);
        resetResult();
        cutout = loaded;
        panel.style.display = 'flex';
        render();
        setStatus(status, '✅ Done. Adjust the result, then press Download PNG.', 'ok');
      } catch (err) {
        setStatus(status, '❌ ' + err.message, 'error');
      } finally {
        runBtn.disabled = false; runBtn.textContent = 'Remove Background';
      }
    };

    downloadBtn.onclick = () => {
      if (!cutout) return;
      const wasBefore = switcher.before;
      if (wasBefore) { switcher.reset(); }
      render();
      canvas.toBlob(blob => {
        if (!blob) { setStatus(status, '❌ The image could not be saved.', 'error'); return; }
        download(blob, 'no-bg.png');
        setStatus(status, '✅ Downloaded no-bg.png', 'ok');
      }, 'image/png');
    };
  }

  function init() { setupEnhance(); setupRemoveBg(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

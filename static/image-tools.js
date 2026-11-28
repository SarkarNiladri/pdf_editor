/* Live preview for Enhance Image, plus small helpers shared with
 * static/remove-bg.js (Remove Background).
 *
 * Kept separate from index.html on purpose: it finds the existing tool views by
 * their element ids, adds the preview controls, and takes over their upload and
 * button handlers. Nothing is downloaded until the person presses Download.
 *
 *  - Enhance Image: every slider change re-renders a smaller copy of the photo
 *    on the server (the exact same Pillow adjustments the download uses), so the
 *    preview is what the downloaded file will look like.
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
    if (window.pdfSuiteSaveFile) { window.pdfSuiteSaveFile(blob, filename); return; }
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

  function init() { setupEnhance(); }
  // Shared with static/remove-bg.js.
  window.pdfSuiteImageKit = { el, errorFrom, download, setStatus, loadImage, compareSwitch, previewHeader, busyBadge };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

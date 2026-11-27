/* PDF Suite: the shared 3-step layout for every single-purpose tool page.
 *
 *   1. Pick a file     2. Choose this tool's options     3. Result
 *
 * A tool view opts in with data attributes, and keeps all of its own element
 * ids and handlers (static/image-tools.js and the tests rely on them):
 *
 *   <div data-tool-page data-input="protectFileInput" data-zone="protectUploadZone"
 *        data-run="runProtectBtn" data-status="protectStatus">
 *     ...the first <h2> and the <p> after it become the coloured header band...
 *
 * This file adds: the header band with a back button and a steps bar, a file
 * card with "Change" in place of the upload box once a file is picked, the main
 * button pinned to the bottom on phones, error messages shown as a card, and a
 * result card (Download again, Share, Do another, Continue with…) after every
 * download made through window.pdfSuiteSaveFile.
 */
(() => {
  const $ = id => (id ? document.getElementById(id) : null);
  const ids = v => (v || '').split(',').map(s => s.trim()).filter(Boolean);
  const fmtSize = n => n >= 1048576 ? (n / 1048576).toFixed(n >= 10485760 ? 0 : 1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
  const el = (tag, attrs = {}, kids = []) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'text') n.textContent = v;
      else if (k === 'class') n.className = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v);
    }
    for (const kid of [].concat(kids)) if (kid) n.append(kid);
    return n;
  };

  // ---------- remember every result so the page can offer it again ----------
  const nativeSave = window.pdfSuiteSaveFile;
  window.pdfSuiteSaveFile = async function (blob, filename) {
    const how = nativeSave ? await nativeSave(blob, filename) : fallbackSave(blob, filename);
    document.dispatchEvent(new CustomEvent('pdfsuite:saved', { detail: { blob, filename, how } }));
    return how;
  };
  function fallbackSave(blob, filename) {
    const url = URL.createObjectURL(blob); const a = el('a', { href: url, download: filename });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 4000);
    return 'downloaded';
  }

  // Tools to suggest after a result, by result type. `open` runs in the home page.
  const NEXT = {
    pdf: [['✏️', 'Edit PDF', 'showEditor'], ['🗜️', 'Compress', 'showConverter', 'compress-pdf'],
          ['🔒', 'Protect', 'showProtect'], ['🔗', 'Merge', 'showConverter', 'merge-pdf']],
    image: [['✨', 'Enhance', 'showEnhance'], ['✂️', 'Remove BG', 'showRemoveBg'],
            ['🖼️', 'Images to PDF', 'showConverter', 'images-to-pdf'], ['🔄', 'Convert image', 'showConverter', 'image-to-image']],
  };
  function openTool(fn, arg) {
    let host = window;
    try { if (window.top !== window && typeof window.parent[fn] === 'function') host = window.parent; } catch {}
    if (typeof host[fn] === 'function') host[fn](arg);
    else location.href = '/';
  }
  function goHome() {
    if (window.top !== window) { try { window.parent.goHome(); return; } catch {} }
    if (typeof window.goHome === 'function') window.goHome(); else location.href = '/';
  }

  // ---------- one tool page ----------
  function setup(root) {
    if (root.dataset.toolPageReady) return;
    // The converter page uses this layout only when it is opened for one tool.
    if (root.dataset.toolPage === 'single' && !document.documentElement.classList.contains('single-tool')) return;
    root.dataset.toolPageReady = '1';
    const inputs = ids(root.dataset.input).map($).filter(Boolean);
    const zone = $(root.dataset.zone);
    const run = $(root.dataset.run);
    const status = $(root.dataset.status);
    let sourceBytes = 0;

    // Header band built from the panel's own title and description.
    const head = root.dataset.head ? $(root.dataset.head) : null;
    const h = head ? head.querySelector('h2') : root.querySelector('h2');
    const p = h && h.nextElementSibling && h.nextElementSibling.tagName === 'P' ? h.nextElementSibling : null;
    const steps = el('div', { class: 'tp-steps', 'aria-hidden': 'true' }, [el('span'), el('span'), el('span')]);
    const stepText = el('span', { class: 'tp-step-text' });
    const band = el('header', { class: 'tp-band' }, [
      el('div', { class: 'tp-band-row' }, [
        el('button', { type: 'button', class: 'tp-back', 'aria-label': 'Back to all tools', text: '‹', onclick: goHome }),
        el('div', { class: 'tp-titles' }, [
          el('h1', { class: 'tp-title', text: h ? h.textContent.trim() : document.title }),
          p ? el('p', { class: 'tp-desc', text: p.textContent.trim() }) : null,
        ]),
      ]),
      el('div', { class: 'tp-progress' }, [steps, stepText]),
    ]);
    if (h) h.remove();
    if (p) p.remove();
    if (head) head.remove();
    root.prepend(band);
    root.classList.add('tp');

    const labels = ['Step 1 of 3 · Choose a file', 'Step 2 of 3 · Options', 'Done'];
    function setStep(n) {
      root.dataset.step = String(n);
      [...steps.children].forEach((s, i) => s.classList.toggle('on', i < n));
      stepText.textContent = labels[n - 1];
    }
    setStep(1);

    // File card that replaces the upload box once something is picked.
    const fileCard = el('div', { class: 'tp-file', hidden: '' });
    if (zone) zone.after(fileCard);
    function showFiles(files) {
      if (!files || !files.length) return;
      sourceBytes = [...files].reduce((n, f) => n + (f.size || 0), 0);
      const first = files[0];
      const kind = /pdf$/i.test(first.name) || first.type === 'application/pdf' ? 'PDF'
        : (first.type || '').startsWith('image/') ? 'IMG' : (first.name.split('.').pop() || 'FILE').toUpperCase().slice(0, 4);
      fileCard.replaceChildren(
        el('div', { class: 'tp-file-icon', text: kind }),
        el('div', { class: 'tp-file-text' }, [
          el('b', { text: files.length > 1 ? `${files.length} files` : first.name }),
          el('small', { text: files.length > 1 ? `${[...files].map(f => f.name).join(', ')} · ${fmtSize(sourceBytes)}` : fmtSize(first.size) }),
        ]),
        el('button', { type: 'button', class: 'tp-change', text: 'Change', onclick: () => { inputs[0] && inputs[0].click(); } }),
      );
      fileCard.hidden = false;
      if (zone) zone.classList.add('tp-picked');
      clearResult();
      setStep(2);
    }
    inputs.forEach(input => input.addEventListener('change', () => showFiles(input.files)));
    if (zone) zone.addEventListener('drop', e => showFiles(e.dataTransfer && e.dataTransfer.files));
    if (run) run.classList.add('tp-run');

    // Errors and progress messages shown as a card.
    if (status) {
      status.classList.add('tp-status');
      const classify = () => {
        const t = status.textContent.trim();
        const bad = /^❌|^⚠️/.test(t) || status.classList.contains('error');
        status.classList.toggle('tp-bad', !!t && bad);
        status.classList.toggle('tp-empty', !t);
        // Tools whose result is shown on the page (summary, ATS score) finish here.
        if (/^✅/.test(t) && !root.classList.contains('tp-done')) setStep(3);
      };
      new MutationObserver(classify).observe(status, { childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ['class'] });
      classify();
    }

    // Result card.
    const result = el('section', { class: 'tp-result', hidden: '', 'aria-live': 'polite' });
    (status || run || fileCard).after(result);
    function clearResult() { result.hidden = true; result.replaceChildren(); root.classList.remove('tp-done'); }
    function showResult({ blob, filename }) {
      const isPdf = /\.pdf$/i.test(filename), isImage = /\.(png|jpe?g|webp|gif|bmp|tiff?)$/i.test(filename);
      const share = (() => {
        try { return navigator.canShare && navigator.canShare({ files: [new File([blob], filename, { type: blob.type })] }); } catch { return false; }
      })();
      const sizeLine = sourceBytes && /compress/i.test(filename)
        ? el('p', { class: 'tp-size' }, [el('s', { text: fmtSize(sourceBytes) }), ' → ', el('b', { text: fmtSize(blob.size) })])
        : el('p', { class: 'tp-size', text: fmtSize(blob.size) });
      const next = NEXT[isPdf ? 'pdf' : isImage ? 'image' : ''] || [];
      result.replaceChildren(...[
        el('div', { class: 'tp-ok', 'aria-hidden': 'true', text: '✓' }),
        el('h2', { class: 'tp-result-title', text: 'Your file is ready' }),
        el('p', { class: 'tp-result-name', text: filename }),
        sizeLine,
        el('button', { type: 'button', class: 'tp-btn tp-primary', text: '⬇ Download again', onclick: () => fallbackSave(blob, filename) }),
        share ? el('button', { type: 'button', class: 'tp-btn', text: 'Share…', onclick: () => navigator.share({ files: [new File([blob], filename, { type: blob.type })], title: filename }).catch(() => {}) }) : null,
        next.length ? el('p', { class: 'tp-next-label', text: 'Continue with another tool' }) : null,
        next.length ? el('div', { class: 'tp-next' }, next.map(([icon, label, fn, arg]) =>
          el('button', { type: 'button', onclick: () => openTool(fn, arg) }, [el('span', { 'aria-hidden': 'true', text: icon }), label]))) : null,
        el('button', { type: 'button', class: 'tp-btn tp-again', text: 'Start again with another file', onclick: () => {
          clearResult(); if (inputs[0]) { inputs[0].value = ''; inputs[0].click(); }
        } }),
      ].filter(Boolean));
      result.hidden = false;
      root.classList.add('tp-done');
      setStep(3);
      result.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    document.addEventListener('pdfsuite:saved', e => {
      if (root.offsetParent === null && getComputedStyle(root).position !== 'fixed') return;   // not the visible tool
      showResult(e.detail);
    });
    root.pdfSuiteToolPage = { setStep, showFiles };
  }

  const init = () => document.querySelectorAll('[data-tool-page]').forEach(setup);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  window.pdfSuiteToolPages = { init };
})();

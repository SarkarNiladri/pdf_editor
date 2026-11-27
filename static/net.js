/* PDF Suite: reliable uploads on phones, shared by every page.
 *
 * Android Chrome can refuse to send a file picked from Downloads/Drive when the
 * upload starts some time after the pick (net::ERR_UPLOAD_FILE_CHANGED), which
 * surfaces as the bare "TypeError: Failed to fetch". To avoid that:
 *  - every file chosen in an <input type=file> (or dropped on the page) is read
 *    into memory straight away;
 *  - uploads to /api/ send that in-memory copy instead of the original handle;
 *  - a request that fails at the network level is retried once, and if it still
 *    fails the error says what happened instead of "Failed to fetch";
 *  - such failures are reported to /api/client-log (no file contents) so they
 *    show up in the server logs.
 * Must load before the page's own scripts (not deferred).
 */
(() => {
  if (window.__pdfSuiteNet) return;
  window.__pdfSuiteNet = true;

  const nativeFetch = window.fetch.bind(window);
  const copies = new WeakMap();   // File -> Promise<File | Error>

  function memoryCopy(file) {
    if (!(file instanceof File)) return Promise.resolve(file);
    let p = copies.get(file);
    if (!p) {
      p = file.arrayBuffer()
        .then(buf => new File([buf], file.name, { type: file.type || guessType(file.name), lastModified: Date.now() }))
        .catch(err => Object.assign(new Error('read-failed'), { cause: err }));
      copies.set(file, p);
    }
    return p;
  }

  function guessType(name) {
    const ext = (name.split('.').pop() || '').toLowerCase();
    return ({ pdf: 'application/pdf', jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp' })[ext] || 'application/octet-stream';
  }

  // Read files the moment they are picked or dropped, while the phone still allows it.
  const remember = files => { for (const f of files || []) memoryCopy(f); };
  document.addEventListener('change', e => {
    const t = e.target;
    if (t && t.tagName === 'INPUT' && t.type === 'file') remember(t.files);
  }, true);
  document.addEventListener('drop', e => remember(e.dataTransfer && e.dataTransfer.files), true);

  async function swapFiles(form) {
    const out = new FormData();
    for (const [key, value] of form.entries()) {
      if (value instanceof File) {
        const copy = await memoryCopy(value);
        if (copy instanceof Error) {
          throw friendly('read', `Couldn't read "${value.name}" from your device. Pick the file again, or save it to Downloads first and pick it from there.`);
        }
        out.append(key, copy, value.name);
      } else {
        out.append(key, value);
      }
    }
    return out;
  }

  function friendly(code, message) {
    const err = new Error(message);
    err.code = code;
    err.friendly = true;
    return err;
  }

  function sizeOf(body) {
    if (!body) return 0;
    if (typeof body === 'string') return body.length;
    if (body instanceof Blob) return body.size;
    if (body instanceof FormData) {
      let n = 0;
      for (const [, v] of body.entries()) n += v instanceof Blob ? v.size : String(v).length;
      return n;
    }
    return 0;
  }

  function report(info) {
    try {
      const payload = JSON.stringify(Object.assign({
        page: location.pathname + location.search,
        ua: navigator.userAgent,
        online: navigator.onLine,
        standalone: window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true,
      }, info));
      if (navigator.sendBeacon) navigator.sendBeacon('/api/client-log', new Blob([payload], { type: 'application/json' }));
      else nativeFetch('/api/client-log', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: payload, keepalive: true }).catch(() => {});
    } catch {}
  }

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  window.fetch = async function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const path = (() => { try { return new URL(url, location.href).pathname; } catch { return ''; } })();
    const isApi = path.startsWith('/api/') && path !== '/api/client-log';
    if (!isApi || !init || !init.body) return nativeFetch(input, init);

    let body = init.body;
    if (body instanceof FormData) body = await swapFiles(body);
    const opts = Object.assign({}, init, { body });

    let lastErr;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        return await nativeFetch(input, opts);
      } catch (err) {
        if (err && err.name === 'AbortError') throw err;   // the page cancelled it on purpose
        lastErr = err;
        if (attempt === 0) await sleep(1200);
      }
    }
    report({ kind: 'network', endpoint: path, bytes: sizeOf(body), error: String(lastErr && (lastErr.message || lastErr)) });
    throw friendly('network', navigator.onLine
      ? "Couldn't reach PDF Suite. The connection dropped while sending your file. Nothing was lost, please try again."
      : "You're offline. Connect to the internet and try again.");
  };

  window.pdfSuiteNet = { memoryCopy, report, nativeFetch };
})();

/* PDF Suite: install-as-app support shared by every page.
 *  - registers the service worker (offline app shell)
 *  - shows an "Install app" button where the browser offers installation, and a
 *    short "Add to Home Screen" tip on iPhone/iPad Safari
 *  - shows a small banner while the device is offline
 *  - window.pdfSuiteSaveFile(blob, name): saves a file in a way that also works
 *    inside the installed iPhone app (share sheet → Save to Files)
 */
(() => {
  const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const embedded = window.top !== window;   // e.g. the converter shown inside the home page
  document.documentElement.classList.toggle('pwa-standalone', standalone);

  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(err => console.warn('Service worker not registered:', err));
    });
  }

  // ---------- styles for the bits this file adds ----------
  const style = document.createElement('style');
  style.textContent = `
    .pwa-install{display:inline-flex;align-items:center;gap:6px;border:1px solid #e6e8f0;background:#fff;color:#334155;border-radius:999px;padding:8px 14px;font:inherit;font-size:13px;font-weight:650;cursor:pointer;min-height:40px;white-space:nowrap}
    .pwa-install:hover{border-color:#c7c2ff;color:#5b4cf0}
    .pwa-tip{position:fixed;left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom));z-index:10001;background:#0f172a;color:#fff;border-radius:16px;padding:14px 44px 14px 16px;font:14px/1.45 Inter,-apple-system,sans-serif;box-shadow:0 20px 50px -20px rgba(0,0,0,.6)}
    .pwa-tip b{font-weight:700}
    .pwa-tip button{position:absolute;top:8px;right:8px;width:32px;height:32px;border:0;border-radius:999px;background:rgba(255,255,255,.12);color:#fff;font-size:16px;cursor:pointer}
    .pwa-offline{position:fixed;left:50%;top:calc(8px + env(safe-area-inset-top));transform:translateX(-50%);z-index:10002;background:#b45309;color:#fff;border-radius:999px;padding:7px 14px;font:600 12.5px Inter,-apple-system,sans-serif;box-shadow:0 10px 30px -12px rgba(0,0,0,.5);white-space:nowrap}
  `;
  document.head.append(style);

  // ---------- install button / tip ----------
  let deferredPrompt = null;
  const slots = () => document.querySelectorAll('[data-pwa-install]');
  function showInstallButtons(show) { slots().forEach(el => { el.hidden = !show; }); }

  window.addEventListener('beforeinstallprompt', e => {
    e.preventDefault(); deferredPrompt = e; showInstallButtons(true);
  });
  window.addEventListener('appinstalled', () => { deferredPrompt = null; showInstallButtons(false); });
  document.addEventListener('click', async e => {
    const btn = e.target.closest('[data-pwa-install]'); if (!btn) return;
    if (deferredPrompt) {
      deferredPrompt.prompt();
      try { await deferredPrompt.userChoice; } catch {}
      deferredPrompt = null; showInstallButtons(false);
    } else if (isIOS) {
      showIOSTip(true);
    }
  });

  function showIOSTip(force) {
    if (document.querySelector('.pwa-tip')) return;
    if (!force) { try { if (localStorage.getItem('pdfSuiteIosTipSeen')) return; } catch {} }
    const tip = document.createElement('div'); tip.className = 'pwa-tip'; tip.setAttribute('role', 'status');
    tip.innerHTML = 'Install PDF Suite: tap <b>Share</b> <span aria-hidden="true">⬆︎</span> then <b>Add to Home Screen</b>.';
    const close = document.createElement('button'); close.type = 'button'; close.setAttribute('aria-label', 'Close'); close.textContent = '✕';
    close.addEventListener('click', () => { tip.remove(); try { localStorage.setItem('pdfSuiteIosTipSeen', '1'); } catch {} });
    tip.append(close); document.body.append(tip);
  }

  document.addEventListener('DOMContentLoaded', () => {
    if (standalone || embedded) return;
    // iOS Safari never fires beforeinstallprompt, so offer the manual steps instead.
    if (isIOS) {
      showInstallButtons(true);
      if (document.body.dataset.pwaTip === 'auto') setTimeout(() => showIOSTip(false), 1500);
    }
  });

  // ---------- offline banner ----------
  let banner = null;
  function updateOnline() {
    if (embedded) return;
    if (navigator.onLine) { banner?.remove(); banner = null; return; }
    if (banner) return;
    banner = document.createElement('div'); banner.className = 'pwa-offline'; banner.setAttribute('role', 'status');
    banner.textContent = "You're offline. Opening and saving PDFs needs a connection.";
    document.body.append(banner);
  }
  window.addEventListener('online', updateOnline);
  window.addEventListener('offline', updateOnline);
  document.addEventListener('DOMContentLoaded', updateOnline);

  // ---------- saving files ----------
  // In the installed iPhone app a normal download link can open the PDF in a
  // dead-end viewer, so use the share sheet (which has "Save to Files") there.
  window.pdfSuiteSaveFile = async function (blob, filename) {
    if (isIOS && standalone && navigator.canShare) {
      const file = new File([blob], filename, { type: blob.type || 'application/pdf' });
      if (navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file], title: filename }); return 'shared'; }
        catch (err) { if (err && err.name === 'AbortError') return 'cancelled'; }
      }
    }
    const url = URL.createObjectURL(blob); const a = document.createElement('a');
    a.href = url; a.download = filename; document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    return 'downloaded';
  };
})();

"""End-to-end check on phone and desktop screens (Chromium + Playwright).

Start the app on 127.0.0.1:8080, put sample.pdf, sample2.pdf, photo.jpg and doc.docx in a folder, then:
    python tests/e2e/phone_and_desktop.py OUTPUT_DIR [FILES_DIR]
"""
import asyncio, sys, json
from playwright.async_api import async_playwright
BASE = 'http://127.0.0.1:8080'
OUT = sys.argv[1] if len(sys.argv) > 1 else '.'
FILES = sys.argv[2] if len(sys.argv) > 2 else OUT
results = []
def rec(dev, name, ok, note=''):
    results.append((dev, name, ok, note)); print(('PASS' if ok else 'FAIL'), dev, name, note, flush=True)

async def wait_download_or_status(page, click, timeout=60000):
    try:
        async with page.expect_download(timeout=timeout) as d:
            await click()
        dl = await d.value
        return True, dl.suggested_filename
    except Exception as e:
        return False, str(e)[:120]

async def run(p, dev_name, dev):
    b = await p.chromium.launch(args=['--no-sandbox'])
    ctx = await b.new_context(**dev, accept_downloads=True)
    await ctx.add_init_script("try { localStorage.setItem('pdfSuiteIosTipSeen', '1'); } catch {}")
    page = await ctx.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    phone = dev.get('is_mobile', False)

    # --- Edit PDF
    await page.goto(BASE + '/'); await page.wait_for_load_state('networkidle')
    await page.evaluate("showEditor()")
    await page.set_input_files('#fileInput', FILES + '/sample.pdf')
    await page.wait_for_selector('#toolbar:not([hidden])', timeout=20000)
    await page.wait_for_timeout(1500)
    open_ = await page.evaluate("document.getElementById('sidebar').classList.contains('open')")
    rec(dev_name, 'Edit PDF: panel collapses after upload', (not open_) if phone else True, f'open={open_}')
    await page.screenshot(path=f'{OUT}/{dev_name}-edit-after-upload.png')
    if phone:
        await page.click('#toolsToggle'); await page.wait_for_timeout(400)
        o1 = await page.evaluate("document.getElementById('sidebar').classList.contains('open')")
        await page.click('#sidebarBackdrop', position={'x': 380, 'y': 400}); await page.wait_for_timeout(400)
        o2 = await page.evaluate("document.getElementById('sidebar').classList.contains('open')")
        rec(dev_name, 'Edit PDF: Tools button reopens, backdrop closes', o1 and not o2)
    if phone: await page.click('#toolsToggle'); await page.wait_for_timeout(300)
    await page.click('#addTextBtn'); await page.fill('#editText', 'Hello from the phone'); await page.click('#modalSave'); await page.wait_for_timeout(2500)
    await page.screenshot(path=f'{OUT}/{dev_name}-edit-with-text.png')
    if phone: await page.click('#toolsToggle'); await page.wait_for_timeout(300)
    ok, note = await wait_download_or_status(page, lambda: page.click('#downloadBtn'))
    rec(dev_name, 'Edit PDF: add text + download', ok, note)

    # --- failure path: network drop on upload shows friendly message, panel reopens
    page2 = await ctx.new_page()
    await page2.route('**/api/extract', lambda r: r.abort('connectionreset'))
    await page2.goto(BASE + '/'); await page2.wait_for_load_state('networkidle')
    await page2.evaluate("showEditor()")
    await page2.set_input_files('#fileInput', FILES + '/sample.pdf')
    await page2.wait_for_timeout(4000)
    status = await page2.evaluate("document.getElementById('status').textContent")
    reopened = await page2.evaluate("document.getElementById('sidebar').classList.contains('open')")
    rec(dev_name, 'Dropped connection: clear message, panel reopens', ("Couldn't reach PDF Suite" in status) and reopened and 'Failed to fetch' not in status, status[:80])
    await page2.screenshot(path=f'{OUT}/{dev_name}-edit-network-error.png')
    await page2.close()

    # --- converter tools (single-tool mode)
    for tool, files in [('compress-pdf', [FILES + '/sample.pdf']), ('pdf-to-images', [FILES + '/sample.pdf']),
                        ('merge-pdf', [FILES + '/sample.pdf', FILES + '/sample2.pdf']), ('split-pdf', [FILES + '/sample.pdf']),
                        ('rotate-pdf', [FILES + '/sample.pdf']), ('images-to-pdf', [FILES + '/photo.jpg']),
                        ('image-to-image', [FILES + '/photo.jpg']), ('word-to-pdf', [FILES + '/doc.docx']), ('pdf-to-word', [FILES + '/sample.pdf'])]:
        await page.goto(BASE + '/'); await page.wait_for_load_state('networkidle')
        await page.evaluate(f"showConverter('{tool}')")
        fr = page.frame_locator('#converterIframe')
        await fr.locator('#files').wait_for(state='attached')
        tabs_visible = await fr.locator('.tabs-wrapper').is_visible()
        name = await fr.locator('#toolName').text_content()
        await page.wait_for_timeout(300)
        if tool == 'compress-pdf':
            await page.screenshot(path=f'{OUT}/{dev_name}-compress-before.png')
        await fr.locator('#files').set_input_files(files)
        await page.wait_for_timeout(1200)
        if tool == 'split-pdf': await fr.locator('#pageRanges').fill('1,2-3')
        if tool == 'rotate-pdf': await fr.locator('#pageRanges').fill('1') if await fr.locator('#pageRanges').count() else None
        ok, note = await wait_download_or_status(page, lambda: fr.locator('#convertBtn').click(), timeout=90000)
        rec(dev_name, f'Convert {tool}: only its own tool shown + result', ok and not tabs_visible, f'tabs={tabs_visible} title={name!r} {note}')
        if tool == 'compress-pdf':
            await page.screenshot(path=f'{OUT}/{dev_name}-compress-after.png')

    # --- protect / unlock
    await page.goto(BASE + '/'); await page.wait_for_load_state('networkidle')
    await page.evaluate("showProtect()")
    await page.set_input_files('#protectFileInput', FILES + '/sample.pdf')
    await page.fill('#protectPassword', 'secret 1')
    ok, note = await wait_download_or_status(page, lambda: page.click('#runProtectBtn'))
    rec(dev_name, 'Protect PDF', ok, note)
    # --- enhance
    await page.evaluate("showEnhance()")
    await page.set_input_files('#enhanceFileInput', FILES + '/photo.jpg')
    await page.wait_for_timeout(1500)
    ok, note = await wait_download_or_status(page, lambda: page.click('#runEnhanceBtn'))
    rec(dev_name, 'Enhance Image', ok, note)
    # --- edit existing text
    await page.goto(BASE + '/edit-existing-text'); await page.wait_for_load_state('networkidle')
    await page.set_input_files('#pdfInput', FILES + '/sample.pdf')
    await page.wait_for_timeout(3000)
    imgs = await page.evaluate("document.querySelectorAll('img').length")
    rec(dev_name, 'Edit Existing Text: page renders', imgs > 0, f'imgs={imgs}')
    # --- check page
    await page.goto(BASE + '/check'); await page.click('#runBtn'); await page.wait_for_timeout(3000)
    await page.set_input_files('#picker', FILES + '/sample.pdf'); await page.wait_for_timeout(2000)
    bad = await page.evaluate("document.querySelectorAll('.st.bad').length"); good = await page.evaluate("document.querySelectorAll('.st.ok').length")
    rec(dev_name, 'Connection check page', bad == 0 and good == 5, f'ok={good} bad={bad}')
    await page.screenshot(path=f'{OUT}/{dev_name}-check.png', full_page=True)
    rec(dev_name, 'No JavaScript errors', not errors, '; '.join(errors)[:200])
    await b.close()

async def main():
    async with async_playwright() as p:
        devs = {'android': p.devices['Pixel 7'], 'iphone': p.devices['iPhone 14'], 'desktop': {'viewport': {'width': 1366, 'height': 860}}}
        devs['iphone'] = {k: v for k, v in devs['iphone'].items() if k != 'default_browser_type'}
        devs['android'] = {k: v for k, v in devs['android'].items() if k != 'default_browser_type'}
        for n, d in devs.items():
            await run(p, n, d)
    fails = [r for r in results if not r[2]]
    print(f'\n{len(results)-len(fails)}/{len(results)} passed')
asyncio.run(main())

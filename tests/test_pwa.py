"""Installable app (PWA): manifest, icons, service worker and mobile meta tags."""
import json
import re
from pathlib import Path

import pytest
from PIL import Image

import app as app_module

ROOT = Path(__file__).resolve().parents[1]
PAGES = ['/', '/edit-existing-text', '/converter']


@pytest.fixture
def client():
    app_module.app.config['TESTING'] = True
    return app_module.app.test_client()


def test_manifest_is_served_with_install_fields(client):
    res = client.get('/manifest.webmanifest')
    assert res.status_code == 200
    assert res.mimetype == 'application/manifest+json'
    data = json.loads(res.data)
    for key in ('name', 'short_name', 'start_url', 'scope', 'display', 'theme_color', 'background_color', 'icons'):
        assert data.get(key), key
    assert data['display'] == 'standalone'
    assert data['start_url'].startswith('/') and data['scope'] == '/'
    purposes = {icon.get('purpose', 'any') for icon in data['icons']}
    assert {'any', 'maskable'} <= purposes
    assert {'192x192', '512x512'} <= {icon['sizes'] for icon in data['icons'] if icon.get('purpose') == 'any'}


def test_every_manifest_icon_exists_with_the_declared_size(client):
    data = json.loads(client.get('/manifest.webmanifest').data)
    icons = data['icons'] + [i for s in data.get('shortcuts', []) for i in s.get('icons', [])]
    for icon in icons:
        res = client.get(icon['src'])
        assert res.status_code == 200, icon['src']
        w, h = (int(n) for n in icon['sizes'].split('x'))
        with Image.open(ROOT / icon['src'].lstrip('/')) as img:
            assert img.size == (w, h), icon['src']
        res.close()


@pytest.mark.parametrize('path, size', [('/apple-touch-icon.png', (180, 180)), ('/static/icons/apple-touch-icon.png', (180, 180))])
def test_ios_touch_icon_is_opaque_180px(client, path, size):
    res = client.get(path)
    assert res.status_code == 200 and res.mimetype == 'image/png'
    res.close()
    with Image.open(ROOT / 'static/icons/apple-touch-icon.png') as img:
        assert img.size == size
        assert img.mode == 'RGB'   # iOS shows transparency as black


def test_favicon(client):
    res = client.get('/favicon.ico')
    assert res.status_code == 200
    res.close()


def test_service_worker_is_served_from_root_with_a_content_based_version(client):
    res = client.get('/sw.js')
    assert res.status_code == 200
    assert res.mimetype == 'application/javascript'
    assert res.headers['Service-Worker-Allowed'] == '/'
    assert 'no-cache' in res.headers['Cache-Control']
    body = res.get_data(as_text=True)
    assert '__CACHE_VERSION__' not in body
    version = re.search(r"CACHE_VERSION = '([0-9a-f]{12})'", body)
    assert version and version.group(1) == app_module._pwa_cache_version()


def test_service_worker_never_caches_uploads_or_api_calls():
    sw = (ROOT / 'static/sw.js').read_text()
    # Only GET requests are handled, and every /api/ route is passed straight to the network.
    assert "request.method !== 'GET'" in sw
    assert "url.pathname.startsWith('/api/')" in sw
    assert 'if (!isCacheable(request, url)) return;' in sw
    shell = re.search(r'const SHELL_URLS = \[(.*?)\];', sw, re.S).group(1)
    assert '/api/' not in shell


def test_every_precached_shell_url_exists(client):
    sw = (ROOT / 'static/sw.js').read_text()
    shell = re.findall(r"'(/[^']*)'", re.search(r'const SHELL_URLS = \[(.*?)\];', sw, re.S).group(1))
    assert set(PAGES) <= set(shell)
    for url in shell:
        res = client.get(url)
        assert res.status_code == 200, url
        res.close()


def test_cache_version_changes_when_a_shell_file_changes(monkeypatch, tmp_path):
    before = app_module._pwa_cache_version()
    for rel in app_module.PWA_SHELL_FILES:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / rel).read_bytes())
    monkeypatch.setattr(app_module, 'PROJECT_ROOT', tmp_path)
    assert app_module._pwa_cache_version() == before
    (tmp_path / 'static/theme.css').write_text('body{}')
    assert app_module._pwa_cache_version() != before


@pytest.mark.parametrize('path', PAGES)
def test_pages_have_install_and_mobile_tags(client, path):
    html = client.get(path).get_data(as_text=True)
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert '<meta name="theme-color"' in html
    assert 'rel="apple-touch-icon"' in html
    assert 'name="apple-mobile-web-app-capable" content="yes"' in html
    assert 'src="/static/pwa.js"' in html
    viewport = re.search(r'<meta name="viewport" content="([^"]+)"', html).group(1)
    assert 'width=device-width' in viewport and 'viewport-fit=cover' in viewport


def test_offline_page_is_self_contained(client):
    html = client.get('/static/offline.html').get_data(as_text=True)
    assert "You're offline" in html
    assert '<script src=' not in html and 'stylesheet' not in html


def test_edit_existing_text_has_mobile_bottom_sheet():
    html = (ROOT / 'edit_existing_text.html').read_text()
    assert 'id="sheetHandle"' in html and 'id="searchToggle"' in html
    assert '.side.collapsed' in html
    # inputs are 16px on phones so iOS does not zoom in when they are focused
    phone_css = html[html.index('@media(max-width:900px)'):]
    assert 'textarea.inp{font-size:16px' in phone_css

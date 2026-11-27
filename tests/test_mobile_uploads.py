"""Phone upload fixes: in-memory uploads, connection check, Edit PDF panel, single-tool converter."""
import io
import json
import re
from pathlib import Path

import pytest

import app as app_module

ROOT = Path(__file__).resolve().parents[1]
PAGES = ['/', '/edit-existing-text', '/converter']


@pytest.fixture
def client():
    app_module.app.config['TESTING'] = True
    return app_module.app.test_client()


@pytest.mark.parametrize('path', PAGES)
def test_every_page_loads_net_js_before_its_own_scripts(client, path):
    html = client.get(path).get_data(as_text=True)
    tag = '<script src="/static/net.js"></script>'
    assert tag in html, 'net.js must load (not deferred) on every page'
    first_inline = html.find('<script>', html.find('</head>'))
    assert html.find(tag) < first_inline


def test_net_js_copies_files_and_explains_network_errors():
    js = (ROOT / 'static' / 'net.js').read_text(encoding='utf-8')
    assert 'arrayBuffer()' in js and "addEventListener('change'" in js
    assert "/api/client-log" in js
    assert 'Failed to fetch' not in js.split('*/', 1)[1], 'the raw browser message must never be shown'


def test_service_worker_precaches_net_js(client):
    sw = client.get('/sw.js').get_data(as_text=True)
    assert "'/static/net.js'" in sw
    assert 'static/net.js' in app_module.PWA_SHELL_FILES


def test_ping_reports_bytes_received(client):
    assert client.get('/api/ping').get_json()['ok'] is True
    payload = b'%PDF-1.4\n' + b'x' * 5000
    res = client.post('/api/ping', data={'file': (io.BytesIO(payload), 'a.pdf')}, content_type='multipart/form-data')
    body = res.get_json()
    assert res.status_code == 200 and body['received_bytes'] == len(payload)
    assert body['files'] == [{'name': 'a.pdf', 'bytes': len(payload)}]


def test_client_log_accepts_reports_and_ignores_junk(client, capsys):
    res = client.post('/api/client-log', data=json.dumps({'kind': 'network', 'endpoint': '/api/extract', 'error': 'TypeError'}),
                      content_type='application/json')
    assert res.status_code == 204
    assert 'client-error' in capsys.readouterr().out
    assert client.post('/api/client-log', data=b'not json').status_code == 204


def test_check_page_is_served_uncached(client):
    res = client.get('/check')
    assert res.status_code == 200 and 'Connection check' in res.get_data(as_text=True)
    assert res.headers['Cache-Control'] == 'no-store'


def test_home_links_to_check_page(client):
    assert 'href="/check"' in client.get('/').get_data(as_text=True)


def test_edit_pdf_panel_can_close_and_reopen_on_phones():
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    assert "getElementById('sidebarClose').addEventListener('click', closeSidebar)" in html
    assert "getElementById('sidebarBackdrop').addEventListener('click', closeSidebar)" in html
    assert 'id="toolsToggle"' in html
    load = html[html.index('async function loadPDF'):]
    load = load[:load.index('\n    }\n')]
    # Collapses as soon as a file is picked, before the upload finishes, and reopens on failure.
    assert load.index('closeSidebar()') < load.index("fetch('/api/extract'")
    assert 'openSidebar()' in load


def test_converter_single_tool_mode_hides_other_tools(client):
    html = client.get('/converter?tool=compress-pdf').get_data(as_text=True)
    assert "classList.add('single-tool')" in html
    assert re.search(r'html\.single-tool \.tabs-wrapper \{ display: none', html)
    assert 'id="toolHead"' in html and "'compress-pdf': ['🗜️ Compress PDF'" in html

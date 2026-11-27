"""Shared 3-step tool page layout (static/tool-page.js / .css)."""
import re
from pathlib import Path

import pytest

import app as app_module

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / 'index.html').read_text(encoding='utf-8')
CONVERTER = (ROOT / 'converter.html').read_text(encoding='utf-8')
TOOL_VIEWS = ['protectView', 'unlockView', 'enhanceView', 'removeBgView', 'summarizeView', 'atsView', 'tailorView']


def _ids(html):
    return set(re.findall(r'\bid="([^"]+)"', html))


@pytest.mark.parametrize('view', TOOL_VIEWS)
def test_every_tool_view_opts_in_with_ids_that_exist(view):
    tag = re.search(r'<div class="app-view" id="%s"([^>]*)>' % view, INDEX)
    assert tag and 'data-tool-page' in tag.group(1), view
    ids = _ids(INDEX)
    for attr in ('data-input', 'data-zone', 'data-run', 'data-status'):
        m = re.search(attr + r'="([^"]+)"', tag.group(1))
        if m:
            for ref in m.group(1).split(','):
                assert ref.strip() in ids, f'{view} {attr} -> missing #{ref}'


@pytest.mark.parametrize('view', TOOL_VIEWS)
def test_every_tool_view_has_a_title_for_its_header_band(view):
    start = INDEX.index('id="%s"' % view)
    end = INDEX.index('<div class="app-view"', start + 10) if '<div class="app-view"' in INDEX[start + 10:] else len(INDEX)
    block = INDEX[start:end]
    assert re.search(r'<h2[^>]*>[^<]+</h2>\s*<p', block), view


def test_converter_single_tool_layout_points_at_real_elements():
    tag = re.search(r'<div class="wrap"([^>]*)>', CONVERTER).group(1)
    assert 'data-tool-page="single"' in tag
    ids = _ids(CONVERTER)
    for ref in ('toolHead', 'files', 'drop', 'convertBtn', 'status'):
        assert f'"{ref}"' in tag and ref in ids


@pytest.mark.parametrize('page', ['index.html', 'converter.html'])
def test_pages_load_tool_page_assets_after_pwa_js(page):
    html = (ROOT / page).read_text(encoding='utf-8')
    assert html.index('/static/pwa.js') < html.index('/static/tool-page.js')
    assert '/static/tool-page.css' in html


def test_results_go_through_pdfsuite_save_file():
    # The result card appears for every download made through pdfSuiteSaveFile.
    assert 'window.pdfSuiteSaveFile(blob, filename)' in INDEX
    assert 'window.pdfSuiteSaveFile(blob, filename)' in CONVERTER
    js = (ROOT / 'static' / 'tool-page.js').read_text(encoding='utf-8')
    assert "new CustomEvent('pdfsuite:saved'" in js and "'pdfsuite:saved'" in js


def test_edit_pdf_phone_dock_buttons_target_real_buttons():
    ids = _ids(INDEX)
    for target in re.findall(r'data-dock="([^"]+)"', INDEX):
        assert target in ids
    assert 'id="dockMore"' in INDEX and 'id="dockDownload"' in INDEX


def test_service_worker_caches_tool_page_assets():
    client = app_module.app.test_client()
    sw = client.get('/sw.js').get_data(as_text=True)
    assert "'/static/tool-page.js'" in sw and "'/static/tool-page.css'" in sw

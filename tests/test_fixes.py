"""Regression tests for the issues found in the November 2026 review."""
import base64
import io

import numpy as np
import pymupdf
import pytest
from PIL import Image

import app as app_module
from app import apply_edits, _apply_existing_text_edits, _extract_editable_spans


@pytest.fixture
def client():
    app_module.app.config['TESTING'] = True
    return app_module.app.test_client()


def _pdf(build):
    doc = pymupdf.open()
    build(doc)
    data = doc.tobytes()
    doc.close()
    return data


def _color_bbox(page, rgb_test):
    pix = page.get_pixmap()
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    ys, xs = np.nonzero(rgb_test(a))
    return (xs.min(), ys.min(), xs.max(), ys.max()) if len(xs) else None


# ---------- 1. Editor edits on rotated pages land where they were placed ----------
@pytest.mark.parametrize('rotation', [0, 90, 180, 270])
def test_shape_and_text_on_rotated_page_land_in_visible_position(rotation):
    def build(doc):
        page = doc.new_page(width=612, height=792)
        page.set_rotation(rotation)
    raw = _pdf(build)
    edits = [
        {'type': 'shape', 'shape': 'rect', 'page': 1, 'order': 1, 'fill': '#0000ff', 'stroke': '#0000ff',
         'rect': {'x0': 300, 'y0': 40, 'x1': 400, 'y1': 80}},
        {'type': 'add', 'page': 1, 'order': 2, 'text': 'HELLO', 'fontSize': 20, 'color': '#00aa00',
         'rect': {'x0': 300, 'y0': 100, 'x1': 400, 'y1': 130}},
    ]
    page = pymupdf.open(stream=apply_edits(raw, edits).getvalue())[0]
    blue = _color_bbox(page, lambda a: (a[:, :, 2] > 200) & (a[:, :, 0] < 50) & (a[:, :, 1] < 50))
    assert blue and abs(blue[0] - 300) <= 2 and abs(blue[1] - 40) <= 2 and abs(blue[2] - 399) <= 2
    green = _color_bbox(page, lambda a: (a[:, :, 1] > 120) & (a[:, :, 0] < 60) & (a[:, :, 2] < 60))
    # Upright text: wider than tall, near the requested spot.
    assert green and green[2] - green[0] > green[3] - green[1] and abs(green[0] - 310) <= 6


def test_add_text_keeps_non_latin_characters():
    raw = _pdf(lambda d: d.new_page())
    edits = [{'type': 'add', 'page': 1, 'text': '₹500 “quotes”', 'fontSize': 14,
              'rect': {'x0': 50, 'y0': 50, 'x1': 300, 'y1': 80}}]
    text = pymupdf.open(stream=apply_edits(raw, edits).getvalue())[0].get_text()
    assert '₹500' in text and '“quotes”' in text


# ---------- 4. Repeated text: the selected occurrence is the one replaced ----------
def test_replacing_second_occurrence_leaves_first_untouched():
    def build(doc):
        page = doc.new_page()
        page.insert_text((72, 100), 'Python', fontsize=12)
        page.insert_text((72, 200), 'Python', fontsize=12)
    raw = _pdf(build)
    doc = pymupdf.open(stream=raw)
    spans = _extract_editable_spans(doc)[0]['spans']
    second = spans[1]
    out = _apply_existing_text_edits(raw, [{
        'page': 1, 'bbox': second['bbox'], 'original_text': 'Python', 'new_text': 'Java',
        'background': '#ffffff'}])
    page = pymupdf.open(stream=out.getvalue())[0]
    words = [(w[4], round(w[3])) for w in page.get_text('words')]
    assert ('Python', 103) in [(t, y) for t, y in words] or any(t == 'Python' and y < 150 for t, y in words)
    assert any(t == 'Java' and y > 150 for t, y in words)
    assert sum(1 for t, _ in words if t == 'Python') == 1  # old second "Python" really removed


# ---------- 5. Subset fonts: new characters are not drawn as empty boxes ----------
def test_subset_font_replacement_renders_new_characters(tmp_path):
    import textfonts
    font_path = textfonts.find_system_font('DejaVu Sans') or textfonts.fallback_font_for_char('a')
    if not font_path:
        pytest.skip('no TrueType font available on this machine')

    def build(doc):
        page = doc.new_page()
        page.insert_text((72, 100), 'abc', fontsize=14, fontname='sub', fontfile=font_path)
        doc.subset_fonts()
    raw = _pdf(build)
    span = _extract_editable_spans(pymupdf.open(stream=raw))[0]['spans'][0]
    out = _apply_existing_text_edits(raw, [{'page': 1, 'bbox': span['bbox'], 'original_text': span['text'],
                                            'new_text': 'xyz QRS', 'background': '#ffffff'}])
    assert 'xyz QRS' in pymupdf.open(stream=out.getvalue())[0].get_text()


def test_existing_text_edit_works_on_rotated_page():
    def build(doc):
        page = doc.new_page()
        page.insert_text((72, 100), 'Hello rotated world', fontsize=14)
        page.set_rotation(90)
    raw = _pdf(build)
    span = _extract_editable_spans(pymupdf.open(stream=raw))[0]['spans'][0]
    out = _apply_existing_text_edits(raw, [{'page': 1, 'bbox': span['bbox'], 'original_text': span['text'],
                                            'new_text': 'Goodbye rotated world', 'background': '#ffffff'}])
    text = pymupdf.open(stream=out.getvalue())[0].get_text()
    assert 'Goodbye rotated world' in text and 'Hello' not in text


def test_background_fill_colour_is_used():
    def build(doc):
        doc.new_page().insert_text((72, 100), 'Python', fontsize=12)
    raw = _pdf(build)
    span = _extract_editable_spans(pymupdf.open(stream=raw))[0]['spans'][0]
    out = _apply_existing_text_edits(raw, [{'page': 1, 'bbox': span['bbox'], 'original_text': 'Python',
                                            'new_text': 'Ruby', 'background': '#ff0000', 'remove_original': False}])
    page = pymupdf.open(stream=out.getvalue())[0]
    assert _color_bbox(page, lambda a: (a[:, :, 0] > 200) & (a[:, :, 1] < 80) & (a[:, :, 2] < 80))


# ---------- 6. Rotate Pages ----------
def _convert(client, conversion, files, **form):
    data = {'conversion': conversion, **form, 'files': [(io.BytesIO(b), n) for n, b in files]}
    return client.post('/api/convert', data=data, content_type='multipart/form-data')


def _three_pages():
    def build(doc):
        for _ in range(3):
            doc.new_page()
        doc[2].set_rotation(90)
    return _pdf(build)


def test_rotate_duplicate_page_is_rotated_once(client):
    res = _convert(client, 'rotate-pdf', [('s.pdf', _three_pages())], page_ranges='1,1', rotation='90')
    assert res.status_code == 200
    assert [p.rotation for p in pymupdf.open(stream=res.data)] == [90, 0, 90]


@pytest.mark.parametrize('value,message', [('3-1', 'goes backwards'), ('1, x', 'Invalid page number'),
                                           ('1,,2', 'empty item'), ('9', 'outside')])
def test_rotate_bad_ranges_have_clear_errors(client, value, message):
    res = _convert(client, 'rotate-pdf', [('s.pdf', _three_pages())], page_ranges=value, rotation='90')
    assert res.status_code == 400 and message in res.get_json()['error']


def test_rotate_rejects_non_numeric_angle_cleanly(client):
    res = _convert(client, 'rotate-pdf', [('s.pdf', _three_pages())], page_ranges='all', rotation='abc')
    assert res.status_code == 400 and 'Rotation must be' in res.get_json()['error']


# ---------- 12. Images to PDF accepts WebP ----------
def test_images_to_pdf_accepts_webp(client):
    buf = io.BytesIO()
    Image.new('RGB', (100, 50), (0, 0, 255)).save(buf, format='WEBP')
    res = _convert(client, 'images-to-pdf', [('a.webp', buf.getvalue())])
    assert res.status_code == 200 and len(pymupdf.open(stream=res.data)) == 1


# ---------- 20/21. Errors and passwords ----------
def test_password_spaces_are_kept_by_protect_and_unlock(client):
    raw = _pdf(lambda d: d.new_page())
    res = client.post('/api/protect-pdf', data={'file': (io.BytesIO(raw), 's.pdf'), 'password': ' secret '},
                      content_type='multipart/form-data')
    assert res.status_code == 200
    res2 = client.post('/api/unlock-pdf', data={'file': (io.BytesIO(res.data), 'p.pdf'), 'password': ' secret '},
                       content_type='multipart/form-data')
    assert res2.status_code == 200


def test_protect_non_pdf_is_a_400(client):
    res = client.post('/api/protect-pdf', data={'file': (io.BytesIO(b'junk'), 's.pdf'), 'password': 'x'},
                      content_type='multipart/form-data')
    assert res.status_code == 400


def test_render_page_bad_input_returns_json_error(client):
    pdf = base64.b64encode(_pdf(lambda d: d.new_page())).decode()
    res = client.post('/api/render-page', json={'pdf': pdf, 'page': 'abc'})
    assert res.status_code == 400 and 'error' in res.get_json()


def test_preview_bad_image_is_a_400_json_error(client):
    pdf = base64.b64encode(_pdf(lambda d: d.new_page())).decode()
    res = client.post('/api/preview', json={'pdf': pdf, 'edits': [
        {'type': 'image', 'page': 1, 'data': 'data:image/png;base64,AAAA', 'rect': {'x0': 1, 'y0': 1, 'x1': 50, 'y1': 50}}]})
    assert res.status_code == 400 and 'error' in res.get_json()


def test_ai_endpoints_are_rate_limited(client, monkeypatch):
    monkeypatch.setenv('AI_RATE_LIMIT_PER_HOUR', '2')
    app_module._RATE_BUCKETS.clear()
    codes = [client.post('/api/summarize', json={'pdf': ''}).status_code for _ in range(3)]
    assert codes[-1] == 429
    app_module._RATE_BUCKETS.clear()

"""Replacement text longer than the original block (Edit Existing Text)."""
import io

import pymupdf
import pytest

import app as app_module
from app import _apply_existing_text_edits, _wrap_words, TextOverflowError


def _doc(build):
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    build(page)
    data = doc.tobytes()
    doc.close()
    return data


def _first_span(raw, text):
    page = pymupdf.open(stream=raw)[0]
    for block in page.get_text('dict')['blocks']:
        for line in block.get('lines', []):
            for span in line['spans']:
                if span['text'] == text:
                    return span
    raise AssertionError(text)


def _edit(raw, original, new, **extra):
    span = _first_span(raw, original)
    out, info = _apply_existing_text_edits(raw, [{
        'id': 'e1', 'page': 1, 'bbox': list(span['bbox']), 'original_text': original,
        'new_text': new, 'background': '#ffffff', **extra}], return_info=True)
    return pymupdf.open(stream=out.getvalue())[0], info


def _spans_with(page, needle):
    return [s for b in page.get_text('dict')['blocks'] for l in b.get('lines', []) for s in l['spans']
            if needle in s['text']]


def _blocked_right(page, width_text='Label'):
    page.insert_text((72, 200), width_text, fontsize=12, fontname='helv')
    page.insert_text((150, 200), 'RIGHT', fontsize=12, fontname='helv')


GAP = 150 - 1.5 - 72  # free width between "Label" and "RIGHT" (see _blocked_right)


def _text_of_width(factor, prefix='Label'):
    """A one-word string whose 12 pt Helvetica width is about factor * GAP."""
    font = pymupdf.Font('helv')
    text = prefix
    while font.text_length(text, fontsize=12) < GAP * factor:
        text += 'x'
    return text


def test_wrap_words_breaks_on_spaces_and_long_words():
    measure = lambda t: len(t) * 10.0
    assert _wrap_words('aa bb cc', 50, measure) == ['aa bb', 'cc']
    assert _wrap_words('abcdefghij', 40, measure) == ['abcd', 'efgh', 'ij']


def test_slightly_long_text_only_tightens_spacing():
    raw = _doc(_blocked_right)
    new = _text_of_width(1.001)  # at most one character (~8%) wider than the gap
    page, info = _edit(raw, 'Label', new)
    assert info['fits'] == ['spacing tightened']
    assert _spans_with(page, new)[0]['size'] > 11.5  # condensed slightly, not shrunk
    assert 'RIGHT' in page.get_text()


def test_moderately_long_text_is_shrunk_but_not_below_80_percent():
    def build(page):
        _blocked_right(page)
        page.insert_text((72, 214), 'BELOW LINE BLOCKS WRAPPING', fontsize=12, fontname='helv')
    raw = _doc(build)
    new = _text_of_width(1.2)
    page, info = _edit(raw, 'Label', new)
    assert info['fits'][0].startswith('shrunk to')
    size = _spans_with(page, new)[0]['size']
    assert 12 * 0.8 - 0.05 <= size < 12


def test_long_text_wraps_into_free_space_below_at_full_size():
    raw = _doc(_blocked_right)
    page, info = _edit(raw, 'Label', 'Label with a much longer replacement value')
    assert info['fits'][0].startswith('wrapped onto')
    text = page.get_text()
    assert 'RIGHT' in text and 'replacement' in text
    sizes = {round(s['size'], 1) for b in page.get_text('dict')['blocks'] for l in b.get('lines', [])
             for s in l['spans'] if 'RIGHT' not in s['text']}
    assert sizes == {12.0}


def test_no_room_anywhere_asks_the_user():
    def build(page):
        _blocked_right(page)
        page.insert_text((72, 214), 'BELOW LINE BLOCKS WRAPPING', fontsize=12, fontname='helv')
    raw = _doc(build)
    with pytest.raises(TextOverflowError) as err:
        _edit(raw, 'Label', 'Label with a much longer replacement value')
    assert err.value.edit_id == 'e1'


@pytest.mark.parametrize('mode,check', [
    ('shrink', lambda page, info: info['fits'][0].startswith('shrunk')),
    ('wrap', lambda page, info: info['fits'][0].startswith('wrapped')),
    ('overflow', lambda page, info: info['fits'][0].startswith('kept')),
])
def test_user_choice_is_applied_when_space_is_blocked(mode, check):
    def build(page):
        _blocked_right(page)
        page.insert_text((72, 214), 'BELOW LINE BLOCKS WRAPPING', fontsize=12, fontname='helv')
    raw = _doc(build)
    page, info = _edit(raw, 'Label', 'Label with a longer value', overflow=mode)
    assert check(page, info)
    assert 'longer' in page.get_text()


def test_endpoint_reports_overflow_code():
    def build(page):
        _blocked_right(page)
        page.insert_text((72, 214), 'BELOW LINE BLOCKS WRAPPING', fontsize=12, fontname='helv')
    raw = _doc(build)
    span = _first_span(raw, 'Label')
    import json
    client = app_module.app.test_client()
    res = client.post('/api/text-edit/preview', content_type='multipart/form-data', data={
        'file': (io.BytesIO(raw), 'a.pdf'),
        'edits': json.dumps([{'id': 'x1', 'page': 1, 'bbox': list(span['bbox']), 'original_text': 'Label',
                              'new_text': 'Label with a much longer replacement value'}])})
    body = res.get_json()
    assert res.status_code == 400 and body['code'] == 'overflow' and body['edit_id'] == 'x1'

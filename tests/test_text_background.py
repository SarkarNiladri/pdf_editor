"""Replaced text must not leave a visible box on non-white pages."""
import io

import numpy as np
import pymupdf
import pytest
from PIL import Image

from app import _apply_existing_text_edits, _extract_editable_spans

CREAM = (247 / 255, 241 / 255, 227 / 255)
MOVE_AWAY = {'offset_x': 0, 'offset_y': 250}   # draw the new text far from the old spot


def _texture_png(w=300, h=400, seed=1):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w]
    base = np.stack([200 + 40 * x / w, 180 + 50 * y / h, 150 + 30 * (x + y) / (w + h)], axis=2)
    noise = rng.integers(-12, 13, size=(h, w, 1))
    arr = np.clip(base + noise, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format='PNG')
    return buf.getvalue()


def _render(page, rect=None, scale=2):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=rect, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].astype(int)


def _make(background, text='Old heading text', rotation=0, ocr_scan=False):
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    if background == 'cream':
        page.draw_rect(page.rect, color=None, fill=CREAM, width=0)
    elif background == 'texture':
        page.insert_image(page.rect, stream=_texture_png())
    if ocr_scan:
        # A "scanned" page: the visible words are pixels in the image and an
        # invisible OCR text layer sits on top.
        img = Image.new('RGB', (300, 400), (240, 236, 225))
        from PIL import ImageDraw
        ImageDraw.Draw(img).rectangle((42, 91, 138, 99), fill=(20, 20, 20))
        buf = io.BytesIO(); img.save(buf, format='PNG')
        page.insert_image(page.rect, stream=buf.getvalue())
        page.insert_text((40, 100), text, fontsize=14, render_mode=3)
    else:
        page.insert_text((40, 100), text, fontsize=14, color=(0.1, 0.1, 0.1))
    page.set_rotation(rotation)
    data = doc.tobytes()
    doc.close()
    return data


def _background_only(raw):
    """The same page with every piece of text removed (the 'true' background)."""
    doc = pymupdf.open(stream=raw)
    page = doc[0]
    page.add_redact_annot(page.rect.transform(page.derotation_matrix).normalize(), fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    return doc


def _edit(raw, **extra):
    span = _extract_editable_spans(pymupdf.open(stream=raw))[0]['spans'][0]
    edit = {'page': 1, 'bbox': span['bbox'], 'original_text': span['text'], 'new_text': 'New',
            **MOVE_AWAY, **extra}
    out, info = _apply_existing_text_edits(raw, [edit], return_info=True)
    return pymupdf.open(stream=out.getvalue()), span, info


def _old_area(span):
    x0, y0, x1, y1 = span['bbox']
    return pymupdf.Rect(x0 - 3, y0 - 3, x1 + 3, y1 + 3)


@pytest.mark.parametrize('background', ['white', 'cream', 'texture'])
@pytest.mark.parametrize('remove_original', [True, False])
def test_old_text_area_matches_the_real_page_background(background, remove_original):
    raw = _make(background)
    out, span, info = _edit(raw, remove_original=remove_original)
    expected = _background_only(raw)
    area = _old_area(span)
    got, want = _render(out[0], area), _render(expected[0], area)
    # No box: every pixel around the old text looks like the untouched background.
    assert np.abs(got - want).max() <= 24, info
    assert np.abs(got - want).mean() <= 2.0, info
    if remove_original:
        assert info['covers'] == {'none': 1}   # glyphs deleted, nothing painted over them
        assert 'Old heading' not in out[0].get_text()


def test_cream_page_with_legacy_white_default_has_no_white_box():
    raw = _make('cream')
    out, span, _ = _edit(raw, background='#ffffff', remove_original=False)
    pixels = _render(out[0], _old_area(span))
    # Before the fix this area held a pure-white rectangle on the cream page.
    assert not (pixels.min(axis=2) >= 252).any()


def test_textured_cover_uses_a_picture_of_the_page():
    raw = _make('texture')
    _, _, info = _edit(raw, remove_original=False)
    assert info['covers'] == {'image': 1}


@pytest.mark.parametrize('rotation', [90, 180, 270])
def test_textured_cover_lines_up_on_rotated_pages(rotation):
    raw = _make('texture', rotation=rotation)
    out, span, _ = _edit(raw, remove_original=False, offset_x=0, offset_y=0, new_text='Old heading text')
    expected = _background_only(raw)
    # Compare a strip just above/below the glyphs: it is cover, not new ink.
    vis = pymupdf.Rect(span['bbox'])
    for strip in (pymupdf.Rect(vis.x0, vis.y0 - 2.5, vis.x1, vis.y0 - 0.5),
                  pymupdf.Rect(vis.x0, vis.y1 + 0.5, vis.x1, vis.y1 + 2.5)):
        unrot = strip.transform(out[0].derotation_matrix).normalize()
        got = _render(out[0], unrot)
        want = _render(expected[0], unrot)
        assert np.abs(got - want).mean() <= 3.0


def test_scanned_text_behind_ocr_layer_is_hidden_with_paper_colour():
    raw = _make('white', ocr_scan=True)
    out, span, info = _edit(raw, remove_original=True)
    pixels = _render(out[0], pymupdf.Rect(span['bbox']))
    assert pixels.max(axis=2).min() > 200        # the dark scanned words are covered
    assert info['covers'] == {'solid': 1}
    # ...with the paper colour, not white.
    assert abs(pixels[..., 2].mean() - 225) < 6


def test_explicit_colour_is_still_respected():
    raw = _make('cream')
    out, span, info = _edit(raw, background='#ff0000', remove_original=False)
    pixels = _render(out[0], pymupdf.Rect(span['bbox']))
    assert ((pixels[..., 0] > 200) & (pixels[..., 1] < 60)).mean() > 0.5
    assert info['covers'] == {}


def test_invalid_background_is_rejected():
    raw = _make('white')
    with pytest.raises(ValueError):
        _edit(raw, background='blue')

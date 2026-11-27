"""Make replaced text blend into the real page background.

When existing text is replaced, the old glyphs are deleted from the content
stream where that is safe. Drawing a solid "whiteout" box on top of that is
what used to leave a visible off-white rectangle on cream, coloured, gradient
or scanned pages. This module decides, per edit, what (if anything) has to be
drawn over the old text so that the result is invisible:

* ``none``  - the old glyphs were really deleted and nothing of them is still
              visible, so no cover is drawn at all.
* ``solid`` - the background under the text is a single flat colour; it is
              covered with exactly that colour (sampled from the page).
* ``image`` - the background is textured/gradient/an image; it is covered with
              a picture of the page rendered *without* the old text.
* ``solid`` (sampled fallback) - the old text cannot be removed even on a
              scratch copy (e.g. it is part of a scanned image), so it is
              covered with the median colour of the surrounding pixels.

All rectangles here are in unrotated page space (the space of the content
stream). Rotated pages are rendered and patched with their /Rotate entry
temporarily set to 0, so pixels and patches always line up.
"""
from contextlib import contextmanager

import numpy as np
import pymupdf as fitz

PATCH_SCALE = 4          # render resolution for background patches (288 dpi)
INK_DELTA = 48           # per-pixel difference that counts as "ink changed"
INK_FRACTION = 0.004     # share of changed pixels that means "ink is gone"
FLAT_RANGE = 6           # max per-channel spread for a background to count as flat
SAMPLE_MARGIN = 3.0      # points around the text used to sample the background


@contextmanager
def _unrotated(page):
    rotation = page.rotation
    if rotation:
        page.set_rotation(0)
    try:
        yield page
    finally:
        if rotation:
            page.set_rotation(rotation)


def _render(page, rect, scale=PATCH_SCALE):
    with _unrotated(page):
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=fitz.Rect(rect) & page.rect,
                              alpha=False, colorspace=fitz.csRGB)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()


def ink_changed(before, after):
    """True when a visible part of the region changed between two renders."""
    if before.shape != after.shape or before.size == 0:
        return True
    diff = np.abs(before.astype(np.int16) - after.astype(np.int16)).max(axis=2)
    return float((diff > INK_DELTA).mean()) >= INK_FRACTION


def flat_colour(arr):
    """Return the colour (0..1 tuple) if the region is one flat colour, else None."""
    if arr.size == 0:
        return None
    flat = arr.reshape(-1, 3)
    # Ignore the outermost anti-aliasing noise: use the 1st..99th percentile spread.
    lo = np.percentile(flat, 1, axis=0)
    hi = np.percentile(flat, 99, axis=0)
    if float((hi - lo).max()) > FLAT_RANGE:
        return None
    return tuple(float(v) / 255 for v in np.median(flat, axis=0))


def sampled_colour(page, rect):
    """Median colour of a thin ring just outside ``rect`` (the surrounding paper)."""
    outer = fitz.Rect(rect.x0 - SAMPLE_MARGIN, rect.y0 - SAMPLE_MARGIN,
                      rect.x1 + SAMPLE_MARGIN, rect.y1 + SAMPLE_MARGIN)
    arr = _render(page, outer, scale=2)
    h, w = arr.shape[:2]
    ring = max(1, int(round(SAMPLE_MARGIN)))  # ~1.5 pt at scale 2
    mask = np.zeros((h, w), dtype=bool)
    mask[:ring, :] = mask[-ring:, :] = True
    mask[:, :ring] = mask[:, -ring:] = True
    pixels = arr[mask] if mask.any() else arr.reshape(-1, 3)
    # Prefer the light, paper-like majority: drop the darkest quarter (neighbouring ink).
    lum = pixels.astype(np.int32) @ np.array([299, 587, 114]) // 1000
    keep = pixels[lum >= np.percentile(lum, 25)] if len(pixels) > 8 else pixels
    return tuple(float(v) / 255 for v in np.median(keep, axis=0))


def _background_without_text(raw, page_index, rect):
    """Render ``rect`` of the original page with all text inside it removed."""
    scratch = fitz.open(stream=raw, filetype='pdf')
    try:
        page = scratch[page_index]
        page.add_redact_annot(fitz.Rect(rect), fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        return _render(page, rect)
    finally:
        scratch.close()


def plan_cover(raw, original_page, page_index, current_page, rect, removed):
    """Decide how to hide the old text inside ``rect`` (unrotated space).

    ``original_page`` is the untouched page, ``current_page`` the page after the
    glyph removal step, ``removed`` whether this edit's glyphs were deleted.
    Returns ``('none', None)``, ``('solid', rgb01)`` or ``('image', png_bytes)``.
    """
    before = _render(original_page, rect)
    if removed and ink_changed(before, _render(current_page, rect)):
        return 'none', None

    clean = _background_without_text(raw, page_index, rect)
    if ink_changed(before, clean):
        colour = flat_colour(clean)
        if colour is not None:
            return 'solid', colour
        pix = fitz.Pixmap(fitz.csRGB, clean.shape[1], clean.shape[0], clean.tobytes(), False)
        return 'image', pix.tobytes('png')

    # The text is not in the text layer (e.g. a scan behind an OCR layer):
    # the best we can do is the colour of the surrounding paper.
    return 'solid', sampled_colour(original_page, rect)


def apply_cover(page, rect, kind, value):
    """Draw the cover chosen by :func:`plan_cover` onto ``page``."""
    if kind == 'none':
        return
    if kind == 'solid':
        page.draw_rect(fitz.Rect(rect), color=None, fill=value, overlay=True, width=0)
    elif kind == 'image':
        with _unrotated(page):
            page.insert_image(fitz.Rect(rect), stream=value, overlay=True, keep_proportion=False)

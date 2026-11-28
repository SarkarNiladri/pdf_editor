import os
import io
import base64
import hashlib
import binascii
import math
import tempfile
import shutil
import re
import json
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from functools import wraps
from pathlib import Path
from PIL import ImageEnhance, Image, ImageOps

from flask import Flask, request, jsonify, send_file
import pymupdf as fitz
from dotenv import load_dotenv

import textfonts
import page_background

# openai / pdfplumber / dateparser are only needed by the AI + ATS tools, so they
# are imported lazily. The rest of the suite starts even if they are missing.

load_dotenv()
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB (matches the UI hint)

PROJECT_ROOT = Path(__file__).resolve().parent

from converter import (
    find_libreoffice, safe_name,
    UPLOAD_DIR as CONVERTER_UPLOAD_DIR,
    _validate_file, _cleanup_old_outputs, _run_conversion,
)


# ---------- Tiny in-memory rate limiter (protects the paid AI endpoints) ----------
_RATE_BUCKETS = defaultdict(deque)
_RATE_LOCK = threading.Lock()


def _client_ip():
    return (request.headers.get('Fly-Client-IP')
            or (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
            or request.remote_addr or 'unknown')


def rate_limited(limit_env, default, window=3600):
    """Allow ``limit_env`` requests per IP per ``window`` seconds (0 = unlimited)."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                limit = int(os.getenv(limit_env, default))
            except ValueError:
                limit = default
            if limit > 0:
                now = time.time()
                key = (func.__name__, _client_ip())
                with _RATE_LOCK:
                    bucket = _RATE_BUCKETS[key]
                    while bucket and bucket[0] < now - window:
                        bucket.popleft()
                    if len(bucket) >= limit:
                        return jsonify({'error': 'Too many requests. Please try again later.'}), 429
                    bucket.append(now)
            return func(*args, **kwargs)
        return wrapper
    return decorator


@app.errorhandler(413)
def too_large(_):
    return jsonify({'error': 'That file is too large (50 MB maximum).'}), 413

@app.route('/')
def index():
    return send_file(PROJECT_ROOT / 'index.html', mimetype='text/html')


# ---------- Installable app (PWA) ----------
STATIC_DIR = PROJECT_ROOT / 'static'
# Files the service worker keeps for offline use. Their combined hash names the
# cache, so a deploy that changes any of them replaces the cached copies.
PWA_SHELL_FILES = ('index.html', 'edit_existing_text.html', 'converter.html',
                   'static/theme.css', 'static/pwa.js', 'static/net.js', 'static/tool-page.js', 'static/tool-page.css', 'static/image-tools.js', 'static/remove-bg.js', 'static/offline.html',
                   'static/manifest.webmanifest', 'static/sw.js')


def _pwa_cache_version():
    digest = hashlib.sha256()
    for rel in PWA_SHELL_FILES:
        path = PROJECT_ROOT / rel
        if path.is_file():
            digest.update(rel.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


@app.get('/sw.js')
def service_worker():
    # Served from the site root so it can control every page (scope '/').
    script = (STATIC_DIR / 'sw.js').read_text(encoding='utf-8')
    script = script.replace('__CACHE_VERSION__', _pwa_cache_version())
    resp = app.response_class(script, mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


@app.get('/manifest.webmanifest')
def web_manifest():
    resp = send_file(STATIC_DIR / 'manifest.webmanifest', mimetype='application/manifest+json')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.get('/favicon.ico')
def favicon():
    return send_file(STATIC_DIR / 'icons' / 'favicon.ico', mimetype='image/x-icon', max_age=86400)


@app.get('/apple-touch-icon.png')
@app.get('/apple-touch-icon-precomposed.png')
def apple_touch_icon():
    # iOS looks here when a page has no <link rel="apple-touch-icon">.
    return send_file(STATIC_DIR / 'icons' / 'apple-touch-icon.png', mimetype='image/png', max_age=86400)


# ---------- PDF text extraction ----------
def span_color(span):
    return '#{:06x}'.format(int(span.get('color', 0)) & 0xFFFFFF)

def extract_pages(data):
    doc = fitz.open(stream=data, filetype='pdf')
    pages = []
    try:
        if doc.needs_pass:
            raise ValueError('document is encrypted')
        for pno, page in enumerate(doc):
            # page.rect is the *visible* (already rotated) page size.
            pages.append({'page': pno+1, 'width': page.rect.width, 'height': page.rect.height,
                          'rotation': page.rotation})
        return pages
    finally:
        doc.close()


@app.post('/api/extract')
def extract():
    file = request.files.get('file')
    if not file: return jsonify({'error': 'No PDF uploaded'}), 400
    data = file.read()
    if not data.startswith(b'%PDF'): return jsonify({'error': 'The uploaded file is not a valid PDF.'}), 400
    try:
        pages = extract_pages(data)
    except Exception as e:
        if 'encrypted' in str(e).lower():
            return jsonify({'error': 'This PDF is password protected. Unlock it first with the Unlock PDF tool.'}), 400
        return jsonify({'error': f'Invalid PDF: {e}'}), 400
    return jsonify({'pdf': base64.b64encode(data).decode('ascii'), 'pageCount': len(pages), 'pages': pages})


def _json_payload():
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        raise ValueError('Invalid request body.')
    return payload


# ---------- Render page ----------
@app.post('/api/render-page')
def render_page():
    try:
        payload = _json_payload()
        pdf_b64 = payload.get('pdf')
        if not pdf_b64: return jsonify({'error': 'Missing PDF data'}), 400
        try: pdf_bytes = base64.b64decode(pdf_b64, validate=True)
        except (binascii.Error, ValueError): return jsonify({'error': 'Invalid base64 PDF data'}), 400
        try:
            page_num = int(payload.get('page', 1)) - 1
            scale = float(payload.get('scale', 1.5))
        except (TypeError, ValueError):
            return jsonify({'error': 'Page and scale must be numbers.'}), 400
        if not (0 < scale <= 5): scale = 1.5

        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
        try:
            if page_num < 0 or page_num >= len(doc): return jsonify({'error': 'Page number out of range'}), 400
            pix = doc[page_num].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            return send_file(io.BytesIO(pix.tobytes('png')), mimetype='image/png')
        finally:
            doc.close()
    except Exception as e:
        return jsonify({'error': f'Could not render page: {e}'}), 400


def hex_to_rgb01(value):
    value = (value or '#000000').lstrip('#')
    if len(value) != 6: return (0, 0, 0)
    try:
        return tuple(int(value[i:i+2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


# ---------- Editing (additive editor) ----------
# The browser works in the *visible* page space (what you see, rotation applied).
# PyMuPDF draws in the *unrotated* page space, so every coordinate coming from the
# UI is mapped through page.derotation_matrix first. Text is drawn with
# rotate=page.rotation so it stays upright on rotated pages.
def _base_font_name(font_family):
    family = (font_family or '').lower()
    if 'times' in family: return 'tiro'
    if 'courier' in family: return 'cour'
    return 'helv'


def insert_text(page, rect, text, font_size, color, font_family):
    if not text or not text.strip(): return True
    base_name = _base_font_name(font_family)
    base_font = fitz.Font(base_name)
    to_page = page.derotation_matrix
    # Direction of "visible right" expressed in unrotated space.
    zero = fitz.Point(0, 0) * to_page
    step = fitz.Point(1, 0) * to_page - zero
    line_height = font_size * 1.2
    inserted = set()
    for index, line in enumerate(text.split('\n')):
        visible = fitz.Point(rect.x0 + 8, rect.y0 + font_size * 0.8 + index * line_height)
        origin = visible * to_page
        runs = textfonts.split_runs(line, textfonts.builtin_covers)
        textfonts.draw_runs(page, origin, (step.x, step.y), runs, font_size, color,
                            page.rotation, base_name, base_font, inserted)
    return True


def decode_image_data(value):
    if not value or not isinstance(value, str): raise ValueError('Missing image data')
    if value.startswith('data:'):
        try: header, encoded = value.split(',', 1)
        except ValueError: raise ValueError('Invalid image data URL')
        if ';base64' not in header.lower(): raise ValueError('Image data URL must be base64 encoded')
        raw = base64.b64decode(encoded, validate=True)
    else: raw = base64.b64decode(value, validate=True)

    if not raw: raise ValueError('Empty image data')
    if raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff'): return raw

    try:
        with Image.open(io.BytesIO(raw)) as im:
            buf = io.BytesIO()
            im.convert('RGBA').save(buf, format='PNG')
            return buf.getvalue()
    except Exception as exc:
        raise ValueError(f'Unsupported or corrupt image format: {exc}')


def _to_page_rect(rect, page):
    """Map a visible-space rectangle to unrotated page space."""
    return fitz.Rect(rect).transform(page.derotation_matrix).normalize()


def _draw_shape(page, edit, rect):
    fill = hex_to_rgb01(edit.get('fill') or '#6366f1')
    stroke = hex_to_rgb01(edit.get('stroke') or edit.get('fill') or '#6366f1')
    try: width = max(0.5, float(edit.get('strokeWidth') or 1))
    except (TypeError, ValueError): width = 1
    try: rotation = float(edit.get('rotation') or 0)
    except (TypeError, ValueError): rotation = 0
    to_page = page.derotation_matrix
    cx, cy = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
    angle_rot = math.radians(rotation)

    def visible_point(x, y):
        dx, dy = x - cx, y - cy
        return fitz.Point(cx + dx * math.cos(angle_rot) - dy * math.sin(angle_rot),
                          cy + dx * math.sin(angle_rot) + dy * math.cos(angle_rot))

    def to_unrotated(point):
        return point * to_page

    shape_kind = edit.get('shape', 'rect')
    if shape_kind in ('line', 'arrow', 'double-arrow'):
        p1 = visible_point(rect.x0, rect.y0)
        p2 = visible_point(rect.x1, rect.y1)
        page.draw_line(to_unrotated(p1), to_unrotated(p2), color=stroke, width=width, overlay=True)
        angle = math.atan2(p2.y - p1.y, p2.x - p1.x)
        head_len = max(7.0, width * 4.0)
        head_ang = math.radians(28)

        def arrowhead(tip, direction):
            for a in (direction + math.pi - head_ang, direction + math.pi + head_ang):
                tail = fitz.Point(tip.x + head_len * math.cos(a), tip.y + head_len * math.sin(a))
                page.draw_line(to_unrotated(tip), to_unrotated(tail), color=stroke, width=width, overlay=True)
        if shape_kind in ('arrow', 'double-arrow'): arrowhead(p2, angle)
        if shape_kind == 'double-arrow': arrowhead(p1, angle + math.pi)
        return

    is_outline = shape_kind.endswith('-outline')
    fill_color = None if is_outline else fill
    is_ellipse = shape_kind.startswith('ellipse')
    if rotation % 360:
        if is_ellipse:
            pts = [visible_point(cx + rect.width / 2 * math.cos(2 * math.pi * i / 72),
                                 cy + rect.height / 2 * math.sin(2 * math.pi * i / 72)) for i in range(72)]
        else:
            pts = [visible_point(rect.x0, rect.y0), visible_point(rect.x1, rect.y0),
                   visible_point(rect.x1, rect.y1), visible_point(rect.x0, rect.y1)]
        page.draw_polyline([to_unrotated(p) for p in pts], color=stroke, fill=fill_color,
                           width=width, closePath=True, overlay=True)
        return
    page_rect = _to_page_rect(rect, page)
    if is_ellipse:
        page.draw_oval(page_rect, color=stroke, fill=fill_color, width=width, overlay=True)
    else:
        page.draw_rect(page_rect, color=stroke, fill=fill_color, width=width, overlay=True)


def apply_edits(raw, edits):
    doc = fitz.open(stream=raw, filetype='pdf')
    try:
        if not isinstance(edits, list): raise ValueError('Edits must be a list.')
        # Sort by order so they stack correctly
        ordered_edits = sorted((e for e in edits if isinstance(e, dict)), key=lambda e: int(e.get('order', 0) or 0))
        for edit in ordered_edits:
            page_value = edit.get('page')
            if page_value is None: continue
            try: page_no = int(page_value) - 1
            except (TypeError, ValueError): continue
            if not 0 <= page_no < len(doc): continue

            page = doc[page_no]
            edit_type = edit.get('type')
            rect_data = edit.get('rect') or {}
            rect = fitz.Rect(float(rect_data.get('x0', 0)), float(rect_data.get('y0', 0)),
                             float(rect_data.get('x1', 0)), float(rect_data.get('y1', 0)))

            if edit_type == 'image':
                if edit.get('deleted'): continue
                if rect.width > 0 and rect.height > 0:
                    raw_img = decode_image_data(edit.get('data'))
                    page.insert_image(_to_page_rect(rect, page), stream=raw_img, keep_proportion=False,
                                      rotate=page.rotation, overlay=True)
            elif edit_type == 'shape':
                if rect.width <= 0 or rect.height <= 0: continue
                _draw_shape(page, edit, rect)
            elif edit_type == 'add':
                if edit.get('deleted') or not str(edit.get('text', '')).strip(): continue
                insert_text(page, rect, str(edit.get('text', '')),
                            float(edit.get('fontSize') or 12),
                            hex_to_rgb01(edit.get('color') or '#000000'),
                            edit.get('font', 'Arial'))

        out = io.BytesIO()
        doc.save(out, garbage=4, deflate=True, clean=True)
        out.seek(0)
        return out
    finally:
        doc.close()


def decode_pdf(payload):
    pdf_b64 = payload.get('pdf')
    if not pdf_b64: raise ValueError('Missing PDF data')
    try:
        return base64.b64decode(pdf_b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('Invalid PDF data.')


@app.post('/api/preview')
def preview():
    try:
        payload = _json_payload()
        out = apply_edits(decode_pdf(payload), payload.get('edits', []))
        return send_file(out, mimetype='application/pdf', as_attachment=False)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500


@app.post('/api/edit')
def edit():
    try:
        payload = _json_payload()
        out = apply_edits(decode_pdf(payload), payload.get('edits', []))
        return send_file(out, mimetype='application/pdf', as_attachment=True, download_name='edited.pdf')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500


# ---------- Separate existing PDF text replacement tool ----------
def _open_uploaded_pdf(file_storage):
    if not file_storage:
        raise ValueError('Please upload a PDF file.')
    raw = file_storage.read()
    if not raw.startswith(b'%PDF'):
        raise ValueError('The uploaded file is not a valid PDF.')
    doc = fitz.open(stream=raw, filetype='pdf')
    if doc.needs_pass:
        doc.close()
        raise ValueError('This PDF is password protected. Unlock it before editing existing text.')
    return raw, doc


def _font_name_key(value):
    value = re.sub(r'^[A-Z]{6,8}\+', '', str(value or ''), flags=re.I)
    return re.sub(r'[^a-z0-9]', '', value.lower())


def _span_font_buffer(doc, page, span):
    """Find the embedded face matching both family and bold/italic style."""
    wanted = _font_name_key(span.get('font'))
    flags = int(span.get('flags', 0) or 0)
    want_bold = bool(flags & 16) or 'bold' in wanted
    want_italic = bool(flags & 2) or 'italic' in wanted or 'oblique' in wanted
    candidates = []
    try:
        for font in page.get_fonts(full=True):
            xref = int(font[0])
            basefont = str(font[3] if len(font) > 3 else '')
            resource_name = str(font[4] if len(font) > 4 else '')
            base_key = _font_name_key(basefont)
            resource_key = _font_name_key(resource_name)
            if not wanted:
                continue
            # PDF span names often omit the style suffix (e.g. CharisSIL),
            # while the embedded resource exposes Charis SIL Regular/Bold.
            family_match = (base_key == wanted or resource_key == wanted or
                            base_key.startswith(wanted) or resource_key.startswith(wanted))
            if not family_match:
                continue
            embedded_bold = 'bold' in base_key
            embedded_italic = 'italic' in base_key or 'oblique' in base_key
            style_penalty = int(embedded_bold != want_bold) + int(embedded_italic != want_italic)
            extracted = doc.extract_font(xref)
            if extracted and len(extracted) >= 4 and extracted[3]:
                candidates.append((style_penalty, xref, extracted[3]))
    except Exception:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    # Never silently substitute a bold face for regular text (or vice versa).
    if candidates[0][0] != 0:
        return None
    return candidates[0][2]


def _fallback_pdf_font(span):
    flags = int(span.get('flags', 0) or 0)
    font_name = str(span.get('font', '')).lower()
    bold = bool(flags & 16) or 'bold' in font_name
    italic = bool(flags & 2) or 'italic' in font_name or 'oblique' in font_name
    if flags & 8 or 'mono' in font_name or 'courier' in font_name:
        return 'cobi' if bold and italic else 'cobo' if bold else 'coit' if italic else 'cour'
    if 'sans' not in font_name and (flags & 4 or 'serif' in font_name or 'times' in font_name or 'georgia' in font_name):
        return 'tibi' if bold and italic else 'tibo' if bold else 'tiit' if italic else 'tiro'
    return 'hebi' if bold and italic else 'hebo' if bold else 'heit' if italic else 'helv'


def _bbox_union(rectangles):
    """Return the union of valid rectangles, or an empty Rect."""
    rects = [fitz.Rect(r) for r in rectangles if r and len(r) == 4]
    rects = [r for r in rects if not r.is_empty and not r.is_infinite and r.width > 0 and r.height > 0]
    if not rects:
        return fitz.Rect()
    result = fitz.Rect(rects[0])
    for rect in rects[1:]:
        result |= rect
    return result


def _rawdict_text_ink_bbox(page, span):
    """Level 2: reconstruct glyph bounds from rawdict character records.

    This is independent of get_texttrace() and works for PDFs where MuPDF can
    decode the span text but cannot provide a matching text-trace character run.
    """
    target_text = str(span.get('text', ''))
    target_bbox = fitz.Rect(span.get('bbox', (0, 0, 0, 0)))
    target_font = _font_name_key(span.get('font', ''))
    candidates = []
    raw = page.get_text('rawdict')
    for block in raw.get('blocks', []):
        if block.get('type', 0) != 0:
            continue
        for line in block.get('lines', []):
            for raw_span in line.get('spans', []):
                chars = raw_span.get('chars') or []
                if not chars:
                    continue
                decoded = ''.join(str(ch.get('c', '')) for ch in chars)
                if decoded != target_text:
                    continue
                char_rects = [ch.get('bbox') for ch in chars if ch.get('bbox')]
                ink = _bbox_union(char_rects)
                if ink.is_empty or ink.is_infinite:
                    continue
                raw_bbox = fitz.Rect(raw_span.get('bbox', ink))
                # Geometry disambiguates repeated identical text on the page.
                distance = sum(abs(a - b) for a, b in zip(raw_bbox, target_bbox))
                raw_font = _font_name_key(raw_span.get('font', ''))
                font_penalty = 0 if not target_font or not raw_font or target_font == raw_font else 1
                candidates.append((font_penalty, distance, ink))
    if not candidates:
        raise ValueError('Raw character positions are unavailable for this text span.')
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _safe_span_level_bbox(page, span):
    """Level 3: use a span bbox only when it cannot touch other text spans.

    This intentionally rejects ambiguous cases rather than risking deletion of
    adjacent lines. It is a last resort, not a blanket fallback.
    """
    target = fitz.Rect(span.get('bbox', (0, 0, 0, 0)))
    if target.is_empty or target.is_infinite or target.width <= 0 or target.height <= 0:
        raise ValueError('The selected text has no usable span bounds.')

    target_text = str(span.get('text', ''))
    target_font = _font_name_key(span.get('font', ''))
    overlaps = []
    for block in page.get_text('dict').get('blocks', []):
        if block.get('type', 0) != 0:
            continue
        for line in block.get('lines', []):
            for other in line.get('spans', []):
                other_text = str(other.get('text', ''))
                other_rect = fitz.Rect(other.get('bbox', (0, 0, 0, 0)))
                if (other_text == target_text and
                    _font_name_key(other.get('font', '')) == target_font and
                    max(abs(a-b) for a,b in zip(other_rect, target)) <= 0.25):
                    continue
                intersection = target & other_rect
                if not intersection.is_empty and intersection.width > 0.25 and intersection.height > 0.25:
                    overlaps.append(other_text[:40])
    if overlaps:
        raise ValueError(
            'This PDF does not expose exact character positions, and the text area '
            'overlaps nearby content. For safety, select another text span or use a '
            'PDF with supported character positioning.'
        )
    # Slight inset avoids extending into the line-leading area.
    inset_x = min(0.15, target.width * 0.02)
    inset_y = min(0.10, target.height * 0.02)
    return fitz.Rect(target.x0 + inset_x, target.y0 + inset_y,
                     target.x1 - inset_x, target.y1 - inset_y)


def _precise_text_ink_bbox(page, span):
    """Three-level glyph-bound detection with conservative failure behaviour.

    Level 1: get_texttrace() rendered glyph bounds.
    Level 2: rawdict per-character bounds.
    Level 3: span bounds only if collision checks prove the region isolated.
    """
    target_text = str(span.get('text', ''))
    target_origin = span.get('origin', (0, 0))
    target_font = _font_name_key(span.get('font', ''))
    candidates = []

    # Level 1: text trace.
    try:
        traces = page.get_texttrace()
    except Exception:
        traces = []
    for trace in traces:
        chars = trace.get('chars') or ()
        if not chars:
            continue
        try:
            trace_text = ''.join(chr(int(char[0])) for char in chars if int(char[0]) >= 0)
        except Exception:
            continue
        if trace_text != target_text:
            continue
        trace_font = _font_name_key(trace.get('font', ''))
        first_origin = chars[0][2] if len(chars[0]) > 2 else target_origin
        try:
            origin_distance = ((float(first_origin[0]) - float(target_origin[0])) ** 2 +
                               (float(first_origin[1]) - float(target_origin[1])) ** 2) ** 0.5
        except Exception:
            origin_distance = 1e9
        font_penalty = 0 if not target_font or not trace_font or target_font == trace_font else 1
        candidates.append((font_penalty, origin_distance, trace))

    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]))
        chosen_trace = candidates[0][2]
        # A trace-level bbox can include generous font ascent/descent and can
        # touch the line below. Use the actual per-glyph boxes instead.
        glyph_boxes = []
        for char in chosen_trace.get('chars') or ():
            try:
                char_bbox = char[3]  # (unicode, glyph id, origin, bbox)
                rect = fitz.Rect(char_bbox)
                if not rect.is_empty and not rect.is_infinite and rect.width > 0 and rect.height > 0:
                    glyph_boxes.append(rect)
            except Exception:
                continue
        glyph_bbox = _bbox_union(glyph_boxes)
        if not glyph_bbox.is_empty and not glyph_bbox.is_infinite and glyph_bbox.width > 0 and glyph_bbox.height > 0:
            # No vertical padding: adjacent lines can be separated by only a
            # few points. A tiny horizontal inset avoids erasing neighbours.
            inset_x = min(0.03, glyph_bbox.width * 0.005)
            return fitz.Rect(glyph_bbox.x0 + inset_x, glyph_bbox.y0,
                             glyph_bbox.x1 - inset_x, glyph_bbox.y1)

    # Level 2: rawdict character bounds.
    try:
        raw_bbox = _rawdict_text_ink_bbox(page, span)
        if not raw_bbox.is_empty and not raw_bbox.is_infinite and raw_bbox.width > 0 and raw_bbox.height > 0:
            # rawdict boxes are already character-level; do not add vertical
            # margins that could reach the next baseline.
            inset_x = min(0.03, raw_bbox.width * 0.005)
            return fitz.Rect(raw_bbox.x0 + inset_x, raw_bbox.y0,
                             raw_bbox.x1 - inset_x, raw_bbox.y1)
    except Exception:
        pass

    # Level 3: conservative span-level fallback, only if isolated.
    try:
        return _safe_span_level_bbox(page, span)
    except Exception as exc:
        raise ValueError(
            'Could not determine safe character bounds for this text. '
            'Character-level detection and the raw-character fallback were '
            'unavailable; span-level replacement was blocked because it could '
            'damage neighbouring content. Try another text span or PDF.'
        ) from exc



def _vis_rect(page, rect):
    """Unrotated page space -> visible (rotated) space."""
    return fitz.Rect(rect).transform(page.rotation_matrix).normalize()


def _vis_vector(page, x, y):
    zero = fitz.Point(0, 0) * page.rotation_matrix
    moved = fitz.Point(x, y) * page.rotation_matrix
    return moved - zero


def _unrot_vector(page, x, y):
    zero = fitz.Point(0, 0) * page.derotation_matrix
    moved = fitz.Point(x, y) * page.derotation_matrix
    return moved - zero


def _extract_editable_spans(doc):
    """Spans are reported in *visible* page space so the UI can overlay them directly."""
    pages = []
    for page_index, page in enumerate(doc):
        page_data = {'page': page_index + 1, 'width': float(page.rect.width), 'height': float(page.rect.height),
                     'rotation': page.rotation, 'spans': []}
        text_dict = page.get_text('dict')
        for block_index, block in enumerate(text_dict.get('blocks', [])):
            if block.get('type', 0) != 0:
                continue
            for line_index, line in enumerate(block.get('lines', [])):
                direction = line.get('dir', (1, 0))
                vis_dir = _vis_vector(page, float(direction[0]), float(direction[1]))
                for span_index, span in enumerate(line.get('spans', [])):
                    text = span.get('text', '')
                    if not text or not text.strip():
                        continue
                    bbox_unrot = span.get('bbox', (0, 0, 0, 0))
                    bbox = [round(float(v), 3) for v in _vis_rect(page, bbox_unrot)]
                    origin_unrot = span.get('origin', (bbox_unrot[0], bbox_unrot[3]))
                    origin_vis = fitz.Point(origin_unrot) * page.rotation_matrix
                    page_data['spans'].append({
                        'id': f'p{page_index + 1}-b{block_index}-l{line_index}-s{span_index}',
                        'page': page_index + 1,
                        'text': text,
                        'font': span.get('font', 'Unknown'),
                        'size': round(float(span.get('size', 11)), 2),
                        'color': '#{:06x}'.format(int(span.get('color', 0)) & 0xFFFFFF),
                        'flags': int(span.get('flags', 0) or 0),
                        'bbox': bbox,
                        'origin': [round(origin_vis.x, 3), round(origin_vis.y, 3)],
                        'direction': [round(vis_dir.x, 5), round(vis_dir.y, 5)],
                    })
        pages.append(page_data)
    return pages


def _span_glyph_rects(page, span):
    """Per-glyph rectangles (unrotated space) of exactly the selected span.

    The text trace / rawdict can hold several spans or lines in one run, and the
    same words can appear many times on a page, so every occurrence of the span
    text is a candidate and the one whose first glyph sits closest to the span's
    own origin wins.
    """
    target_text = str(span.get('text', ''))
    target_font = _font_name_key(span.get('font', ''))
    origin = fitz.Point(span.get('origin', (0, 0)))
    candidates = []  # (distance, rects)

    def consider(chars_info, pos):
        subset = chars_info[pos:pos + len(target_text)]
        rects = []
        for rect, _ in subset:
            try:
                r = fitz.Rect(rect)
                if r.is_valid and not r.is_empty and r.width > 0 and r.height > 0:
                    rects.append(r)
            except Exception:
                continue
        if rects and subset and subset[0][1] is not None:
            first = fitz.Point(subset[0][1])
            candidates.append((abs(first.x - origin.x) + abs(first.y - origin.y), rects))

    try:
        for trace in page.get_texttrace():
            trace_chars = trace.get('chars') or ()
            trace_text = ''.join(chr(int(ch[0])) if int(ch[0]) >= 0 else '\0' for ch in trace_chars)
            trace_font = _font_name_key(trace.get('font', ''))
            if target_font and trace_font and target_font != trace_font:
                continue
            start = 0
            while True:
                pos = trace_text.find(target_text, start)
                if pos < 0:
                    break
                consider([(ch[3], ch[2]) for ch in trace_chars], pos)
                start = pos + 1
    except Exception:
        pass
    if candidates:
        return min(candidates, key=lambda c: c[0])[1]

    try:
        for block in page.get_text('rawdict').get('blocks', []):
            if block.get('type', 0) != 0:
                continue
            for line in block.get('lines', []):
                for raw_span in line.get('spans', []):
                    chars = raw_span.get('chars') or []
                    raw_text = ''.join(str(ch.get('c', '')) for ch in chars)
                    raw_font = _font_name_key(raw_span.get('font', ''))
                    if target_font and raw_font and target_font != raw_font:
                        continue
                    start = 0
                    while True:
                        pos = raw_text.find(target_text, start)
                        if pos < 0:
                            break
                        consider([(ch.get('bbox'), ch.get('origin')) for ch in chars], pos)
                        start = pos + 1
    except Exception:
        pass
    if not candidates:
        raise ValueError('Exact rendered character positions are unavailable; this selection cannot be safely replaced.')
    return min(candidates, key=lambda c: c[0])[1]


def _pixel_whiteout_overlay(page, span, fill_hex='#ffffff', glyph_rects=None):
    """Return the tight glyph-union rectangle (visible space) to cover.

    With the default white fill the background around the glyphs must be plain
    near-white. A user-chosen non-white fill is trusted (they picked the colour
    to match the background), so the check is skipped.
    """
    glyph_rects = glyph_rects if glyph_rects is not None else _span_glyph_rects(page, span)
    glyph_rects = [_vis_rect(page, r) for r in glyph_rects]
    clip = _bbox_union(glyph_rects)
    if clip.is_empty:
        raise ValueError('No usable glyph geometry was found for this selection.')
    tight = fitz.Rect(clip)
    clip.x0 = max(page.rect.x0, clip.x0 - 2.5)
    clip.y0 = max(page.rect.y0, clip.y0 - 2.5)
    clip.x1 = min(page.rect.x1, clip.x1 + 2.5)
    clip.y1 = min(page.rect.y1, clip.y1 + 2.5)

    if fill_hex.lower() == '#ffffff':
        scale = 4
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
        import numpy as np
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        gray = (arr[:, :, 0].astype('uint32') * 299 + arr[:, :, 1].astype('uint32') * 587 +
                arr[:, :, 2].astype('uint32') * 114) // 1000
        # Sample the four outer corners (the whole perimeter may contain neighbouring text).
        corners = [int(gray[y, x]) for y, x in (
            (2, 2), (2, max(2, pix.width - 3)),
            (max(2, pix.height - 3), 2),
            (max(2, pix.height - 3), max(2, pix.width - 3)))]
        if sum(corners) / len(corners) < 238 or min(corners) < 220:
            raise ValueError('This selection appears on a coloured or complex background. '
                             'Pick a background fill that matches it (or use a plain white area).')
        if not (gray < 250).any():
            raise ValueError('No visible glyph pixels were found for this text selection.')

    tight.x0 = max(page.rect.x0, tight.x0 - 0.12)
    tight.y0 = max(page.rect.y0, tight.y0 - 0.12)
    tight.x1 = min(page.rect.x1, tight.x1 + 0.12)
    tight.y1 = min(page.rect.y1, tight.y1 + 0.12)
    return tight


def _unrotated_page_rect(page):
    return fitz.Rect(page.rect).transform(page.derotation_matrix).normalize()


def _available_horizontal_width(page, selected_span, ink_bbox, origin, direction=(1.0, 0.0)):
    """Measure safe room along the text direction before the next span/page edge.

    Works in unrotated page space (where text of a rotated page is still
    horizontal). Only applies to horizontal text.
    """
    try:
        dx, dy = float(direction[0]), float(direction[1])
    except Exception:
        return max(1.0, float(ink_bbox.width))
    if abs(dy) > 0.08 or dx < 0.92:
        return max(1.0, float(ink_bbox.width))

    page_right = float(_unrotated_page_rect(page).x1) - 2.0
    nearest_blocker = page_right
    selected_text = str(selected_span.get('text', ''))
    selected_bbox = [float(v) for v in selected_span.get('bbox', ())]
    selected_origin = selected_span.get('origin', (origin.x, origin.y))
    selected_size = max(4.0, float(selected_span.get('size', 11) or 11))
    baseline_tolerance = max(1.5, selected_size * 0.30)

    for block in page.get_text('dict').get('blocks', []):
        if block.get('type', 0) != 0:
            continue
        for line in block.get('lines', []):
            line_dir = line.get('dir', (1.0, 0.0))
            if abs(float(line_dir[1])) > 0.08 or float(line_dir[0]) < 0.92:
                continue
            for candidate in line.get('spans', []):
                text = str(candidate.get('text', ''))
                cb = candidate.get('bbox', ())
                co = candidate.get('origin', (0, 0))
                if len(cb) != 4:
                    continue
                cb = [float(v) for v in cb]
                if text == selected_text and len(selected_bbox) == 4 and max(abs(a-b) for a, b in zip(cb, selected_bbox)) <= 1.25:
                    continue
                try:
                    same_baseline = abs(float(co[1]) - float(selected_origin[1])) <= baseline_tolerance
                except Exception:
                    same_baseline = abs((cb[1] + cb[3]) / 2 - float(origin.y)) <= baseline_tolerance
                if not same_baseline:
                    continue
                if cb[0] > float(origin.x) + 1.0:
                    nearest_blocker = min(nearest_blocker, cb[0] - 1.5)

    return max(1.0, nearest_blocker - float(origin.x))


def _find_original_span(page, edit):
    target_bbox = edit.get('bbox') or []
    target_text = edit.get('original_text')
    if len(target_bbox) != 4 or not isinstance(target_text, str):
        raise ValueError('Invalid text selection. Please select the text again.')
    try:
        # The UI sends visible-space coordinates; the text layer uses unrotated space.
        target_bbox = [float(v) for v in _to_page_rect(fitz.Rect([float(v) for v in target_bbox]), page)]
    except (TypeError, ValueError):
        raise ValueError('Invalid text selection coordinates.')
    best = None
    for block in page.get_text('dict').get('blocks', []):
        if block.get('type', 0) != 0:
            continue
        for line in block.get('lines', []):
            for span in line.get('spans', []):
                bbox = [float(v) for v in span.get('bbox', ())]
                if len(bbox) != 4 or span.get('text', '') != target_text:
                    continue
                distance = max(abs(a - b) for a, b in zip(bbox, target_bbox))
                if distance <= 1.25 and (best is None or distance < best[0]):
                    best = (distance, span, line)
    if best:
        return best[1], best[2]
    raise ValueError('The selected text could not be found in the original PDF. Please re-upload the document and try again.')


def _nonspace_len(text):
    return len(re.sub(r'\s+', '', text or ''))


def _remove_original_glyphs(raw, page_index, glyph_groups):
    """Really delete the selected glyphs from the page (not just cover them).

    A thin strip through the middle of every glyph is redacted so neighbouring
    lines are never touched. The result is checked on a scratch copy first: only
    the expected number of characters may disappear, otherwise nothing is
    removed and the caller falls back to cover-only.
    Returns the list of redaction rects to apply, or None if unsafe.
    """
    rects = []
    for group in glyph_groups:
        for r in group:
            h = r.height
            strip = fitz.Rect(r.x0 + r.width * 0.15, r.y0 + h * 0.35, r.x1 - r.width * 0.15, r.y1 - h * 0.35)
            if strip.width > 0 and strip.height > 0:
                rects.append(strip)
    if not rects:
        return None
    scratch = fitz.open(stream=raw, filetype='pdf')
    try:
        page = scratch[page_index]
        before = _nonspace_len(page.get_text())
        for rect in rects:
            page.add_redact_annot(rect, fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        after = _nonspace_len(page.get_text())
        return rects, before - after
    except Exception:
        return None
    finally:
        scratch.close()



# ---------- Fitting replacement text that is longer than the original ----------
FONT_FLOOR = 0.80        # automatic shrinking never goes below 80% of the original size
CONDENSE_MIN = 0.92      # letters/spaces may be tightened by up to 8% first
MAX_WRAP_LINES = 8
OVERFLOW_MODES = ('auto', 'shrink', 'wrap', 'overflow')


def _overflow_mode(edit):
    mode = edit.get('overflow')
    if mode is None:
        return 'auto' if edit.get('fit_to_width', True) else 'overflow'
    if mode not in OVERFLOW_MODES:
        raise ValueError('Invalid overflow option.')
    return mode


class TextOverflowError(ValueError):
    """The replacement doesn't fit anywhere safe; the user has to choose how to handle it."""
    def __init__(self, message, edit_id=None):
        super().__init__(message)
        self.edit_id = edit_id


def _layout_obstacles(page, span):
    """Text spans and images on the page (unrotated space), except the selected span."""
    selected = fitz.Rect(span.get('bbox', (0, 0, 0, 0)))
    obstacles = []
    for block in page.get_text('dict').get('blocks', []):
        if block.get('type', 0) == 1:
            obstacles.append(fitz.Rect(block['bbox']))
            continue
        for line in block.get('lines', []):
            for other in line.get('spans', []):
                if not str(other.get('text', '')).strip():
                    continue
                rect = fitz.Rect(other.get('bbox'))
                if other.get('text') == span.get('text') and max(abs(a - b) for a, b in zip(rect, selected)) <= 0.5:
                    continue
                obstacles.append(rect)
    return obstacles


def _wrap_words(text, width, measure):
    """Greedy word wrap; a single word wider than ``width`` is broken by characters."""
    lines, current = [], ''
    for word in text.split(' '):
        candidate = word if not current else current + ' ' + word
        if measure(candidate) <= width or not current and measure(word) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ''
        while measure(word) > width and len(word) > 1:
            cut = len(word)
            while cut > 1 and measure(word[:cut]) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        lines.append(current)
    return lines


def _plan_text_layout(text, origin, available_width, size, angle, measure, mode, obstacles, page_bottom, edit_id=None):
    """Decide how to place ``text``. ``measure(text, size)`` returns its width.

    auto:     fits -> as is; else tighten spacing (<=8%); else shrink to >=80%;
              else wrap onto more lines into free space below; else TextOverflowError.
    shrink:   tighten + shrink as far as needed (down to 4 pt).
    wrap:     wrap onto more lines even if they run into content below.
    overflow: keep the original size on one line, even past the free space.
    """
    def single(font_size, hscale, method):
        return {'lines': [(text, fitz.Point(origin))], 'size': font_size, 'hscale': hscale, 'method': method}

    full = measure(text, size)
    if full <= available_width or full <= 0:
        return single(size, 1.0, 'fits')
    if mode == 'overflow':
        return single(size, 1.0, 'kept as is (overflows)')
    if angle != 0:
        # Vertical/rotated runs: only shrinking is supported.
        new_size = max(4.0, size * available_width / full)
        if mode == 'auto' and new_size < size * FONT_FLOOR:
            raise TextOverflowError('The new text is too long for this rotated text block. Shorten it, or choose Shrink or Keep as is.', edit_id)
        return single(new_size, 1.0, f'shrunk to {round(100 * new_size / size)}%')

    hscale = available_width / full
    if hscale >= CONDENSE_MIN:
        return single(size, hscale, 'spacing tightened')
    if mode in ('auto', 'shrink'):
        floor = size * FONT_FLOOR if mode == 'auto' else 4.0
        new_size = size * available_width / (full * CONDENSE_MIN)
        if new_size >= floor:
            return single(new_size, CONDENSE_MIN, f'shrunk to {round(100 * new_size / size)}%')
        if mode == 'shrink':
            raise TextOverflowError('Even at the smallest readable size the new text does not fit. Shorten it, or choose Wrap or Keep as is.', edit_id)

    for font_size in ((size, size * FONT_FLOOR) if mode == 'auto' else (size,)):
        lines = _wrap_words(text, available_width, lambda t, s=font_size: measure(t, s))
        if mode == 'auto' and len(lines) > MAX_WRAP_LINES:
            continue
        pitch = font_size * 1.2
        placed, ok = [], True
        for index, line in enumerate(lines):
            point = fitz.Point(origin.x, origin.y + index * pitch)
            box = fitz.Rect(point.x, point.y - font_size * 0.9, point.x + measure(line, font_size), point.y + font_size * 0.25)
            if index and box.y1 > page_bottom:
                ok = False
                break
            if index and mode == 'auto':
                for obstacle in obstacles:
                    overlap = box & obstacle
                    if not overlap.is_empty and overlap.width > 0.5 and overlap.height > 0.5:
                        ok = False
                        break
                if not ok:
                    break
            placed.append((line, point))
        if ok:
            note = f'wrapped onto {len(lines)} lines' + ('' if font_size == size else f' at {round(100 * font_size / size)}%')
            return {'lines': placed, 'size': font_size, 'hscale': 1.0, 'method': note}
    raise TextOverflowError('The new text does not fit: there is no free space to the right or below. '
                            'Choose Shrink, Wrap or Keep as is.', edit_id)


def _apply_existing_text_edits(raw, edits, return_info=False):
    if not isinstance(edits, list) or not edits:
        raise ValueError('Please make at least one text replacement.')
    if len(edits) > 100:
        raise ValueError('You can replace up to 100 text spans in one operation.')

    doc = fitz.open(stream=raw, filetype='pdf')
    info = {'removed': 0, 'covered_only': 0, 'fits': [], 'fit_by_id': {}, 'covers': {}}
    try:
        if doc.needs_pass:
            raise ValueError('This PDF is password protected. Unlock it before editing existing text.')

        prepared = []
        for edit in edits:
            if not isinstance(edit, dict):
                raise ValueError('Invalid edit data.')
            try:
                page_index = int(edit.get('page', 0)) - 1
            except (TypeError, ValueError):
                raise ValueError('Invalid page number.')
            if page_index < 0 or page_index >= len(doc):
                raise ValueError('A selected text item refers to an invalid page.')
            new_text = edit.get('new_text', '')
            if not isinstance(new_text, str) or not new_text.strip():
                raise ValueError('Replacement text cannot be empty.')
            if len(new_text) > 1000:
                raise ValueError('Replacement text must be 1,000 characters or fewer.')
            if '\n' in new_text or '\r' in new_text:
                raise ValueError('Please replace one text line at a time. Line breaks are not supported yet.')

            page = doc[page_index]
            span, line = _find_original_span(page, edit)
            original_value = str(span.get('text', ''))
            if original_value[:1].isspace() and not new_text[:1].isspace():
                new_text = ' ' + new_text
            if original_value[-1:].isspace() and not new_text[-1:].isspace():
                new_text = new_text + ' '
            bbox = _precise_text_ink_bbox(page, span)
            if bbox.is_empty or bbox.is_infinite or bbox.width <= 0 or bbox.height <= 0:
                raise ValueError('The selected text has an unsupported bounding box.')
            fill_value = edit.get('background') or 'auto'
            if not isinstance(fill_value, str):
                raise ValueError('Invalid background color.')
            # "auto" (and the old default white) means: match the real page background.
            if fill_value.lower() in ('auto', '#ffffff'):
                fill_value, fill = 'auto', None
            elif re.fullmatch(r'#[0-9a-fA-F]{6}', fill_value):
                fill = tuple(int(fill_value[i:i + 2], 16) / 255 for i in (1, 3, 5))
            else:
                raise ValueError('Invalid background color.')
            glyph_rects = _span_glyph_rects(page, span)
            overlay_rect = _pixel_whiteout_overlay(page, span, fill_value, glyph_rects)

            direction = tuple(line.get('dir', (1.0, 0.0)))
            try:
                off_x = max(-300.0, min(300.0, float(edit.get('offset_x', 0) or 0)))
                off_y = max(-300.0, min(300.0, float(edit.get('offset_y', 0) or 0)))
            except (TypeError, ValueError):
                raise ValueError('Invalid text offset.')
            shift = _unrot_vector(page, off_x, off_y)  # UI offsets are in visible space
            origin = fitz.Point(span.get('origin', (bbox.x0, bbox.y1)))
            prepared.append({
                'page_index': page_index,
                'bbox': bbox,
                'glyph_rects': glyph_rects,
                'overlay_rect': overlay_rect,
                'fill': fill,
                'remove_original': bool(edit.get('remove_original', True)),
                'target_len': _nonspace_len(original_value),
                'origin': origin + shift,
                'font_size': max(4.0, min(200.0, float(span.get('size', 11) or 11))),
                'font_color': tuple(((int(span.get('color', 0)) >> sh) & 255) / 255 for sh in (16, 8, 0)),
                'flags': int(span.get('flags', 0) or 0),
                'font_name': span.get('font', ''),
                'font_buffer': _span_font_buffer(doc, page, span),
                'new_text': new_text,
                'fit_to_width': bool(edit.get('fit_to_width', True)),
                'overflow': _overflow_mode(edit),
                'edit_id': edit.get('id'),
                'obstacles': _layout_obstacles(page, span),
                'page_bottom': float(_unrotated_page_rect(page).y1) - 2.0,
                'direction': direction,
                'available_width': max(1.0, _available_horizontal_width(page, span, bbox, origin, direction)
                                       - (shift.x if abs(direction[0]) >= abs(direction[1]) else 0)),
            })

        # 1) Delete the original glyphs where that can be done safely.
        removed_pages = {}
        for page_index in sorted({p['page_index'] for p in prepared}):
            items = [p for p in prepared if p['page_index'] == page_index and p['remove_original']]
            if not items:
                continue
            result = _remove_original_glyphs(raw, page_index, [p['glyph_rects'] for p in items])
            expected = sum(p['target_len'] for p in items)
            if result and result[1] == expected:
                page = doc[page_index]
                for rect in result[0]:
                    page.add_redact_annot(rect, fill=False)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
                removed_pages[page_index] = True
                info['removed'] += len(items)
        info['covered_only'] = len(prepared) - info['removed']

        # 2) Hide whatever is left of the old text. With an automatic background
        #    nothing is drawn when the glyphs are really gone; otherwise the cover
        #    is the page's own colour or a picture of the page without the text.
        original = None
        try:
            for item in prepared:
                page = doc[item['page_index']]
                rect = _to_page_rect(item['overlay_rect'], page)
                if item['fill'] is not None:
                    page.draw_rect(rect, color=None, fill=item['fill'], overlay=True, width=0)
                    continue
                if original is None:
                    original = fitz.open(stream=raw, filetype='pdf')
                removed = item['remove_original'] and removed_pages.get(item['page_index'], False)
                kind, value = page_background.plan_cover(raw, original[item['page_index']], item['page_index'],
                                                         page, rect, removed)
                page_background.apply_cover(page, rect, kind, value)
                info['covers'][kind] = info['covers'].get(kind, 0) + 1
        finally:
            if original is not None:
                original.close()

        # 3) Draw the replacement text.
        inserted = set()
        for edit_index, item in enumerate(prepared):
            page = doc[item['page_index']]
            font_size = item['font_size']
            new_text = item['new_text']
            builtin_name = _fallback_pdf_font({'font': item['font_name'], 'flags': item['flags']})
            base_name, base_font, covers = builtin_name, fitz.Font(builtin_name), textfonts.builtin_covers
            buffer = item['font_buffer']
            if buffer:
                # Subset fonts only contain the glyphs the original text used: use the
                # embedded face only if it can draw every character of the new text.
                try:
                    embedded = fitz.Font(fontbuffer=buffer)
                    if all(ch.isspace() or embedded.has_glyph(ord(ch)) for ch in new_text):
                        resource = f'replacefont_{edit_index}_{item["page_index"]}'
                        page.insert_font(fontname=resource, fontbuffer=buffer)
                        base_name, base_font = resource, embedded
                        covers = lambda ch, f=embedded: ch.isspace() or bool(f.has_glyph(ord(ch)))
                except Exception:
                    pass
            if base_font is not None and base_name == builtin_name:
                # The embedded (subset) font can't draw the new text: prefer the same
                # family installed on the server before falling back to Helvetica/Times.
                flags = item['flags']
                system_path = textfonts.find_system_font(
                    item['font_name'], bold=bool(flags & 16) or 'bold' in item['font_name'].lower(),
                    italic=bool(flags & 2) or 'italic' in item['font_name'].lower())
                if system_path:
                    try:
                        system_font = fitz.Font(fontfile=system_path)
                        resource = f'sysfont_{edit_index}_{item["page_index"]}'
                        page.insert_font(fontname=resource, fontfile=system_path)
                        base_name, base_font = resource, system_font
                        covers = lambda ch, f=system_font: ch.isspace() or bool(f.has_glyph(ord(ch)))
                    except Exception:
                        pass
            direction = item['direction']
            angle = 0
            if abs(direction[1]) > 0.5:
                angle = 90 if direction[1] > 0 else 270
            elif direction[0] < -0.5:
                angle = 180

            def measure(text, size, covers=covers, base_font=base_font):
                return textfonts.runs_length(textfonts.split_runs(text, covers), base_font, size)

            available_width = max(1.0, float(item.get('available_width', item['bbox'].width)))
            plan = _plan_text_layout(new_text, item['origin'], available_width, font_size, angle, measure,
                                     item['overflow'], item['obstacles'], item['page_bottom'], item['edit_id'])
            info['fits'].append(plan['method'])
            if item['edit_id']:
                info['fit_by_id'][str(item['edit_id'])] = plan['method']
            for line_text, line_origin in plan['lines']:
                textfonts.draw_runs(page, line_origin, direction, textfonts.split_runs(line_text, covers),
                                    plan['size'], item['font_color'], angle, base_name, base_font, inserted,
                                    hscale=plan['hscale'])

        output = io.BytesIO()
        doc.save(output, garbage=4, deflate=True, clean=True)
        output.seek(0)
        return (output, info) if return_info else output
    finally:
        doc.close()


@app.get('/edit-existing-text')
def edit_existing_text_ui():
    return send_file(PROJECT_ROOT / 'edit_existing_text.html', mimetype='text/html')


# ---------- Connection check (phones) ----------
@app.get('/check')
def connection_check_ui():
    resp = send_file(PROJECT_ROOT / 'check.html', mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/ping', methods=['GET', 'POST'])
def api_ping():
    """Echo back how many bytes arrived, so /check can tell which step fails."""
    received = 0
    files = []
    if request.method == 'POST':
        for f in request.files.getlist('file'):
            data = f.read()
            received += len(data)
            files.append({'name': f.filename, 'bytes': len(data)})
        if not files:
            received = len(request.get_data(cache=False) or b'')
    resp = jsonify({'ok': True, 'method': request.method, 'received_bytes': received, 'files': files,
                    'server_time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.post('/api/client-log')
@rate_limited('CLIENT_LOG_LIMIT_PER_HOUR', 60)
def client_log():
    """Browsers report failed uploads here (no file contents) so they show in `fly logs`."""
    try:
        info = json.loads(request.get_data(cache=False)[:4000] or b'{}')
    except ValueError:
        info = {'raw': 'unparseable'}
    if not isinstance(info, dict):
        info = {'raw': str(info)[:200]}
    safe = {k: str(v)[:300] for k, v in list(info.items())[:20]}
    print('client-error', json.dumps(safe, ensure_ascii=True), flush=True)
    return ('', 204)


@app.post('/api/text-edit/extract')
def text_edit_extract():
    doc = None
    try:
        _, doc = _open_uploaded_pdf(request.files.get('file'))
        pages = _extract_editable_spans(doc)
        total_spans = sum(len(page['spans']) for page in pages)
        return jsonify({'pageCount': len(pages), 'pages': pages, 'spanCount': total_spans})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400
    finally:
        if doc is not None:
            doc.close()


@app.post('/api/text-edit/render-page')
def text_edit_render_page():
    doc = None
    try:
        _, doc = _open_uploaded_pdf(request.files.get('file'))
        page_number = int(request.form.get('page', '1')) - 1
        scale = min(3.0, max(0.5, float(request.form.get('scale', '1.5'))))
        if page_number < 0 or page_number >= len(doc):
            return jsonify({'error': 'Page number is outside this document.'}), 400
        pix = doc[page_number].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return send_file(io.BytesIO(pix.tobytes('png')), mimetype='image/png')
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400
    finally:
        if doc is not None:
            doc.close()


def _text_edit_request_output(download=False):
    file = request.files.get('file')
    raw, _doc = _open_uploaded_pdf(file)
    _doc.close()
    try:
        edits = json.loads(request.form.get('edits', '[]'))
    except json.JSONDecodeError:
        raise ValueError('Invalid text replacement data.')
    output, info = _apply_existing_text_edits(raw, edits, return_info=True)
    response = send_file(output, mimetype='application/pdf', as_attachment=download,
                         download_name='text-edited.pdf' if download else None)
    # How many replacements truly deleted the old text vs. only covered it.
    response.headers['X-Text-Removed'] = str(info['removed'])
    response.headers['X-Text-Covered-Only'] = str(info['covered_only'])
    response.headers['X-Text-Fit'] = '; '.join(info['fits'])
    response.headers['X-Text-Fit-By-Id'] = json.dumps(info['fit_by_id'], ensure_ascii=True)
    response.headers['Access-Control-Expose-Headers'] = 'X-Text-Removed, X-Text-Covered-Only, X-Text-Fit, X-Text-Fit-By-Id'
    return response


@app.post('/api/text-edit/preview')
def text_edit_preview():
    try:
        return _text_edit_request_output(download=False)
    except TextOverflowError as exc:
        return jsonify({'error': str(exc), 'code': 'overflow', 'edit_id': exc.edit_id}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.post('/api/text-edit/download')
def text_edit_download():
    try:
        return _text_edit_request_output(download=True)
    except TextOverflowError as exc:
        return jsonify({'error': str(exc), 'code': 'overflow', 'edit_id': exc.edit_id}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400

# ---------- Password Protect & Unlock ----------
def _open_pdf_bytes(data):
    """Open uploaded bytes as a PDF, with friendly errors for bad input."""
    if not data.startswith(b'%PDF'):
        raise ValueError('The uploaded file is not a valid PDF.')
    try:
        return fitz.open(stream=data, filetype='pdf')
    except Exception:
        raise ValueError('This PDF could not be read. It may be damaged.')


@app.route('/api/protect-pdf', methods=['POST'])
def protect_pdf():
    if 'file' not in request.files or 'password' not in request.form:
        return jsonify({'error': 'Missing file or password'}), 400

    password = request.form['password']  # exact string, same rule as Unlock (spaces count)
    if not password.strip():
        return jsonify({'error': 'Password cannot be empty'}), 400

    doc = None
    try:
        doc = _open_pdf_bytes(request.files['file'].read())
        if doc.needs_pass:
            return jsonify({'error': 'This PDF is already password protected. Unlock it first.'}), 400
        out = io.BytesIO()
        doc.save(out, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password)
        out.seek(0)
        return send_file(out, mimetype='application/pdf', as_attachment=True, download_name='protected.pdf')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Could not protect this PDF: {e}'}), 500
    finally:
        if doc is not None: doc.close()


@app.route('/api/unlock-pdf', methods=['POST'])
def unlock_pdf():
    if 'file' not in request.files or 'password' not in request.form:
        return jsonify({'error': 'Missing file or password'}), 400

    password = request.form['password']  # keep exact string, passwords can have spaces
    doc = None
    try:
        doc = _open_pdf_bytes(request.files['file'].read())
        if doc.needs_pass and not doc.authenticate(password):
            return jsonify({'error': 'Incorrect password. Please check for typos or hidden spaces.'}), 403
        out = io.BytesIO()
        doc.save(out, encryption=fitz.PDF_ENCRYPT_NONE)
        out.seek(0)
        return send_file(out, mimetype='application/pdf', as_attachment=True, download_name='unlocked.pdf')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        return jsonify({'error': 'Invalid PDF file or unexpected error.'}), 400
    finally:
        if doc is not None: doc.close()


# ---------- Summarization ----------
def extract_all_text(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    try:
        return ''.join(page.get_text() for page in doc)
    finally:
        doc.close()


def _openai_client(api_key):
    import openai
    return openai.OpenAI(api_key=api_key)


@app.post('/api/summarize')
@rate_limited('AI_RATE_LIMIT_PER_HOUR', 20)
def summarize_pdf():
    data = request.get_json(silent=True) or {}
    pdf_b64 = data.get('pdf')
    if not pdf_b64: return jsonify({'error': 'No PDF data provided'}), 400

    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key: return jsonify({'error': 'OpenAI API key is not configured.'}), 500

    try: pdf_bytes = base64.b64decode(pdf_b64, validate=True)
    except (binascii.Error, ValueError): return jsonify({'error': 'Invalid base64 PDF'}), 400

    try:
        full_text = extract_all_text(pdf_bytes)
    except Exception:
        return jsonify({'error': 'This PDF could not be read.'}), 400
    if not full_text.strip(): return jsonify({'error': 'No text found in PDF.'}), 400

    max_chars = 12000
    if len(full_text) > max_chars: full_text = full_text[:max_chars] + "\n\n[Document truncated...]"

    try:
        client = _openai_client(api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a document summarization assistant. Summarize the provided document in 5 to 7 concise sentences."},
                {"role": "user", "content": f"Document text:\n\n{full_text}"}
            ],
            max_tokens=300, temperature=0.5
        )
        return jsonify({'summary': response.choices[0].message.content.strip()})
    except Exception as e:
        return jsonify({'error': f'OpenAI API error: {str(e)}'}), 500


# ---------- ATS ----------
ACTION_VERBS = {"achieved", "managed", "led", "developed", "built", "created", "designed", "implemented", "improved", "increased", "decreased", "reduced", "solved", "analyzed", "coordinated", "delivered", "launched", "optimized", "streamlined", "facilitated", "mentored", "spearheaded", "drove", "executed", "generated", "negotiated", "presented", "supervised", "trained", "transformed", "introduced"}
TECH_SKILLS = {"python", "java", "c++", "c#", "javascript", "typescript", "react", "angular", "vue", "node.js", "express", "django", "flask", "spring", "sql", "mysql", "postgresql", "mongodb", "docker", "kubernetes", "aws", "azure", "gcp", "linux", "git", "jenkins", "ci/cd", "agile", "scrum", "kanban", "jira", "confluence", "data analysis", "machine learning", "deep learning", "nlp", "computer vision", "tableau", "power bi", "excel", "vba", "html", "css", "bootstrap", "sass", "webpack", "rest api", "graphql", "microservices", "unity", "unreal", "android", "ios", "swift", "kotlin", "rust", "go", "perl", "ruby", "php", "wordpress", "shopify", "salesforce", "workday"}


def extract_text_from_file(file_storage):
    filename = file_storage.filename.lower()
    if filename.endswith('.pdf'):
        import pdfplumber
        with pdfplumber.open(file_storage) as pdf:
            text = ""
            for page in pdf.pages: text += (page.extract_text() or "") + "\n"
        return text
    elif filename.endswith('.docx'):
        from docx import Document as DocxDocument
        doc = DocxDocument(file_storage)
        return "\n".join([para.text for para in doc.paragraphs])
    raise ValueError("Unsupported file format. Please upload a PDF or DOCX.")


def ats_check(file_storage):
    import dateparser
    try: text = extract_text_from_file(file_storage)
    except Exception as e: return {"error": f"Could not read file: {str(e)}"}
    if not text or len(text.strip()) < 50: return {"error": "Document appears empty."}

    text_lower = text.lower()
    words = text_lower.split()
    word_count = len(words)

    parseable_score = 10
    if word_count < 100: parseable_score = 0
    elif word_count < 200: parseable_score = 5
    alpha_chars = sum(c.isalpha() for c in text)
    if len(text) > 0 and alpha_chars / len(text) < 0.4: parseable_score = max(0, parseable_score - 5)

    section_keywords = {"summary": ["summary", "profile", "about me", "objective"], "education": ["education", "academic", "university", "college", "school", "degree"], "experience": ["experience", "employment", "work history", "professional experience"], "skills": ["skills", "technical skills", "core competencies", "expertise"], "projects": ["projects", "personal projects"], "certifications": ["certifications", "certificates"], "languages": ["languages"]}
    sections_found = sum(1 for section, keywords in section_keywords.items() if any(kw in text_lower for kw in keywords))
    structure_score = min(20, sections_found * 3)

    formatting_score = 0
    bullet_lines = re.findall(r'^\s*[-*•\d]+\.?\s+', text, re.MULTILINE)
    if len(bullet_lines) >= 5: formatting_score += 7
    elif len(bullet_lines) >= 2: formatting_score += 4
    if "\n\n\n" not in text: formatting_score += 4
    elif "\n\n\n\n" not in text: formatting_score += 2
    formatting_score = min(15, formatting_score)

    contact_score = 0
    if re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', text): contact_score += 4
    if re.search(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', text): contact_score += 3
    if re.search(r'(linkedin\.com/in/|github\.com/)', text_lower): contact_score += 3
    contact_score = min(10, contact_score)

    date_score = 0
    raw_dates = re.findall(r'\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}\b|\b\d{1,2}/\d{1,2}/\d{4}\b|\b\d{4}\b', text_lower, re.IGNORECASE)
    parsed = (dateparser.parse(d, languages=['en']) for d in raw_dates)
    parsed_years = [p.year for p in parsed if p]
    if len(parsed_years) >= 2:
        years = sorted(parsed_years, reverse=True)
        if all(years[i] >= years[i+1] for i in range(len(years)-1)): date_score += 8
        if any(year >= datetime.now().year - 2 for year in years): date_score += 4
        if max(years) - min(years) < 20: date_score += 3
    elif len(parsed_years) == 1: date_score = 5
    date_score = min(15, date_score)

    quality_score = 0
    if word_count > 300: quality_score += 5
    elif word_count > 150: quality_score += 3
    verb_count = sum(1 for w in words if w in ACTION_VERBS)
    if verb_count >= 10: quality_score += 5
    elif verb_count >= 5: quality_score += 3
    numbers = re.findall(r'\b\d+%|\$\d+|\d+\s*(?:percent|%)|\b\d{1,3}(?:,\d{3})*\b', text)
    if len(numbers) >= 3: quality_score += 5
    elif len(numbers) >= 1: quality_score += 2
    quality_score = min(15, quality_score)

    skill_count = sum(1 for skill in TECH_SKILLS if skill in text_lower)
    skill_score = 15 if skill_count >= 10 else 12 if skill_count >= 6 else 8 if skill_count >= 3 else 4

    total_score = (parseable_score/10)*10 + (structure_score/20)*20 + (formatting_score/15)*15 + (contact_score/10)*10 + (date_score/15)*15 + (quality_score/15)*15 + (skill_score/15)*15

    return {
        "total": round(total_score, 1),
        "breakdown": {
            "parseability": {"score": parseable_score, "max": 10, "weight": 0.10},
            "structure": {"score": structure_score, "max": 20, "weight": 0.20},
            "formatting": {"score": formatting_score, "max": 15, "weight": 0.15},
            "contact": {"score": contact_score, "max": 10, "weight": 0.10},
            "date_consistency": {"score": date_score, "max": 15, "weight": 0.15},
            "content_quality": {"score": quality_score, "max": 15, "weight": 0.15},
            "skill_coverage": {"score": skill_score, "max": 15, "weight": 0.15},
        },
        "word_count": word_count, "sections_found": sections_found, "skills_detected": skill_count,
    }


@app.post('/api/ats-check')
def ats_check_endpoint():
    if 'file' not in request.files: return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({'error': 'No file selected'}), 400
    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ['pdf', 'docx']: return jsonify({'error': 'Unsupported file format. Please upload a PDF or DOCX.'}), 400
    try:
        result = ats_check(file)
        if 'error' in result: return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': f'Analysis failed: {str(e)}'}), 500


# ---------- AI Resume Tailor ----------
@app.route('/api/tailor-resume', methods=['POST'])
@rate_limited('AI_RATE_LIMIT_PER_HOUR', 20)
def tailor_resume():
    jd_text = request.form.get('jd', '')
    jd_file = request.files.get('jd_file')

    if jd_file and jd_file.filename != '':
        try: jd_text += "\n" + extract_text_from_file(jd_file)
        except Exception as e: return jsonify({'error': f'Could not read JD file: {str(e)}'}), 400

    if 'resume' not in request.files or not jd_text.strip(): return jsonify({'error': 'Missing resume file or Job Description'}), 400

    file = request.files['resume']
    if file.filename == '': return jsonify({'error': 'No resume file selected'}), 400

    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key: return jsonify({'error': 'OpenAI API key not configured'}), 500

    try:
        resume_text = extract_text_from_file(file)
        client = _openai_client(api_key)
        prompt = f"""
        You are an expert ATS resume optimizer. I will provide a resume and a JD.
        1. Rewrite the resume to heavily emphasize matching skills.
        2. Calculate a match score from 0 to 100.
        3. Identify top missing keywords.
        4. Output the result in clean HTML (use <h1>, <h2>, <ul>, <li>, <p>, <strong>. No <html>/<body> tags).
        You MUST return a valid JSON object:
        {{ "tailored_html": "...", "match_score": 85, "missing_keywords": ["kw1"], "improvements": ["chg1"] }}
        Job Description:
        {jd_text}
        Current Resume:
        {resume_text}
        """
        response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], response_format={"type": "json_object"}, temperature=0.7)
        ai_data = json.loads(response.choices[0].message.content)

        tailored_html = ai_data.get('tailored_html', '')
        if tailored_html.startswith("```html"): tailored_html = tailored_html[7:]
        if tailored_html.endswith("```"): tailored_html = tailored_html[:-3]

        doc = fitz.open()
        page = doc.new_page()
        rect = fitz.Rect(50, 50, page.rect.width - 50, page.rect.height - 50)
        page.insert_htmlbox(rect, tailored_html)

        pdf_bytes = doc.write()
        pdf_b64 = base64.b64encode(pdf_bytes).decode('utf-8')

        pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        preview_img_b64 = base64.b64encode(pix.tobytes("png")).decode('utf-8')
        doc.close()

        return jsonify({'match_score': ai_data.get('match_score', 0), 'pdf_b64': pdf_b64, 'preview_img_b64': preview_img_b64, 'missing_keywords': ai_data.get('missing_keywords', []), 'improvements': ai_data.get('improvements', [])})
    except Exception as e:
        return jsonify({'error': f'Failed to tailor resume: {str(e)}'}), 500


# ---------- Converter routes ----------
@app.get('/converter')
def converter_ui():
    converter_html = PROJECT_ROOT / 'converter.html'
    if not converter_html.is_file(): return jsonify({'error': 'Converter UI not found'}), 500
    return send_file(converter_html, mimetype='text/html')

@app.get('/api/capabilities')
def converter_capabilities():
    office_available = bool(find_libreoffice())
    return jsonify({'word_to_pdf': office_available, 'excel_to_pdf': office_available, 'powerpoint_to_pdf': office_available, 'pdf_to_word': True, 'images_to_pdf': True, 'pdf_to_images': True, 'merge_pdf': True, 'compress_pdf': True, 'split_pdf': True, 'rotate_pdf': True, 'reorder_pdf': True})

MAX_PREVIEW_PAGES = 150  # thumbnails are rendered server-side; cap the work per request


@app.post('/api/pdf-preview')
def pdf_preview():
    """Return lightweight page thumbnails and metadata for organizer tools."""
    uploads = request.files.getlist('files')
    if not uploads:
        return jsonify({'error': 'Upload at least one PDF.'}), 400
    result = []
    remaining = MAX_PREVIEW_PAGES
    try:
        for file_index, upload in enumerate(uploads):
            data = upload.read()
            if not data.startswith(b'%PDF'):
                return jsonify({'error': f'{upload.filename or "File"} is not a valid PDF.'}), 400
            doc = fitz.open(stream=data, filetype='pdf')
            try:
                if doc.is_encrypted:
                    return jsonify({'error': 'Password-protected PDFs must be unlocked first.'}), 400
                pages = []
                for page_index, page in enumerate(doc):
                    if remaining <= 0:
                        break
                    pix = page.get_pixmap(matrix=fitz.Matrix(0.32, 0.32), alpha=False)
                    thumb = base64.b64encode(pix.tobytes('png')).decode('ascii')
                    pages.append({'page': page_index + 1, 'width': round(page.rect.width),
                                  'height': round(page.rect.height), 'rotation': page.rotation,
                                  'thumbnail': 'data:image/png;base64,' + thumb})
                    remaining -= 1
                result.append({'fileIndex': file_index, 'fileName': upload.filename or f'PDF {file_index+1}',
                               'pageCount': len(doc), 'shownPages': len(pages), 'pages': pages})
            finally:
                doc.close()
        return jsonify({'files': result, 'totalPages': sum(x['pageCount'] for x in result),
                        'truncated': any(x['shownPages'] < x['pageCount'] for x in result)})
    except Exception as exc:
        return jsonify({'error': f'Could not preview PDF: {exc}'}), 400


@app.post('/api/convert')
def converter_convert():
    conversion = (request.form.get('conversion') or '').strip().lower()
    uploads = request.files.getlist('files')
    work_dir = Path(tempfile.mkdtemp(prefix='converter_', dir=str(CONVERTER_UPLOAD_DIR)))
    try:
        files = []
        for upload in uploads:
            if not upload.filename: continue
            target = work_dir / safe_name(upload.filename)
            upload.save(target)
            _validate_file(target)
            files.append(target)
        _cleanup_old_outputs()
        resp, status = _run_conversion(conversion, files, target_format=request.form.get('target_format'), quality=request.form.get('quality', 85), compress_level=request.form.get('compress_level', 6), resize_mode=request.form.get('resize_mode', 'none'), scale_percent=request.form.get('scale_percent', 50), width=request.form.get('width'), height=request.form.get('height'), keep_aspect=request.form.get('keep_aspect', 'true'), page_ranges=request.form.get('page_ranges', ''), rotation=request.form.get('rotation', '90'), page_order=request.form.get('page_order', ''))
        return resp, status
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------- Image Enhancer & Background Remover ----------
def _float_field(name, default=1.0, low=0.0, high=5.0):
    raw = request.form.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f'{name.capitalize()} must be a number.')
    return max(low, min(high, value))


ENHANCE_PREVIEW_MAX = 1200   # longest side of live previews, in pixels


def _open_upload_image(file_storage, mode):
    """Open an uploaded image upright (phone photos carry their rotation in EXIF)."""
    img = Image.open(file_storage.stream)
    img = ImageOps.exif_transpose(img)
    return img.convert(mode)


def _enhance(img, brightness, contrast, color, sharpness):
    if brightness != 1.0: img = ImageEnhance.Brightness(img).enhance(brightness)
    if contrast != 1.0: img = ImageEnhance.Contrast(img).enhance(contrast)
    if color != 1.0: img = ImageEnhance.Color(img).enhance(color)
    if sharpness != 1.0: img = ImageEnhance.Sharpness(img).enhance(sharpness)
    return img


def _enhance_request(preview):
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    try:
        params = [_float_field(name) for name in ('brightness', 'contrast', 'color', 'sharpness')]
        try:
            img = _open_upload_image(file, 'RGB')
        except Exception:
            return jsonify({'error': 'That file is not a supported image.'}), 400

        if preview and max(img.size) > ENHANCE_PREVIEW_MAX:
            img.thumbnail((ENHANCE_PREVIEW_MAX, ENHANCE_PREVIEW_MAX), Image.Resampling.LANCZOS)
        img = _enhance(img, *params)

        out = io.BytesIO()
        img.save(out, format='JPEG', quality=85 if preview else 95)
        out.seek(0)
        if preview:
            response = send_file(out, mimetype='image/jpeg')
            response.headers['Cache-Control'] = 'no-store'
            return response
        return send_file(out, mimetype='image/jpeg', as_attachment=True, download_name='enhanced.jpg')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/enhance-image', methods=['POST'])
def enhance_image():
    return _enhance_request(preview=False)


@app.post('/api/enhance-image/preview')
def enhance_image_preview():
    """Same adjustments as the download, on a smaller copy, for the live preview."""
    return _enhance_request(preview=True)


# ---------- Remove Background: "Auto (AI)" mode ----------
# Runs the small U2-Net model (u2netp, 4.6 MB) with onnxruntime directly. We used
# to go through the `rembg` package, but importing it compiles pymatting's numba
# code on every start (37 s on a fast CPU, minutes on a shared one), which made
# the first AI request time out. The model is downloaded into the Docker image at
# build time (see Dockerfile); if it is missing it is fetched once on first use.
AI_MODEL_URL = 'https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx'
AI_MODEL_MD5 = '8e83ca70e441ab06c318d82300c84806'
AI_MODEL_PATH = Path(os.environ.get('AI_MODEL_PATH') or (Path.home() / '.cache' / 'pdf-suite' / 'u2netp.onnx'))
_AI_SESSION = None
_AI_LOCK = threading.Lock()


def download_ai_model(path=None):
    """Fetch the model if it isn't there yet (also run by the Dockerfile at build time)."""
    import urllib.request
    path = Path(path or AI_MODEL_PATH)
    if path.is_file():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.part')
    with urllib.request.urlopen(AI_MODEL_URL, timeout=60) as res, open(tmp, 'wb') as out:
        shutil.copyfileobj(res, out)
    if hashlib.md5(tmp.read_bytes()).hexdigest() != AI_MODEL_MD5:
        tmp.unlink(missing_ok=True)
        raise RuntimeError('The AI model download was corrupted. Please try again.')
    tmp.replace(path)
    return path


def _ai_session():
    """Load the AI model once and reuse it."""
    global _AI_SESSION
    with _AI_LOCK:
        if _AI_SESSION is None:
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            _AI_SESSION = ort.InferenceSession(str(download_ai_model()), options, providers=['CPUExecutionProvider'])
        return _AI_SESSION


def _ai_mask(img):
    """Alpha mask (PIL 'L', same size as img) of the main subject, as rembg's u2netp did it."""
    import numpy as np
    session = _ai_session()
    small = np.asarray(img.convert('RGB').resize((320, 320), Image.Resampling.LANCZOS), dtype=np.float64)
    small = small / max(small.max(), 1e-6)
    small = (small - (0.485, 0.456, 0.406)) / (0.229, 0.224, 0.225)
    batch = small.transpose(2, 0, 1)[None].astype(np.float32)
    pred = session.run(None, {session.get_inputs()[0].name: batch})[0][0, 0]
    lo, hi = float(pred.min()), float(pred.max())
    pred = (pred - lo) / (hi - lo) if hi > lo else np.zeros_like(pred)
    mask = Image.fromarray((pred * 255).astype('uint8'), 'L')
    return mask.resize(img.size, Image.Resampling.LANCZOS)


@app.route('/api/remove-bg', methods=['POST'])
def remove_bg():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    try:
        try:
            img = _open_upload_image(file, 'RGBA')
        except Exception:
            return jsonify({'error': 'That file is not a supported image.'}), 400

        # Memory protection: shrink huge images before running the model.
        max_size = 1500
        if max(img.width, img.height) > max_size:
            ratio = max_size / max(img.width, img.height)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.Resampling.LANCZOS)

        output = Image.composite(img, Image.new('RGBA', img.size, (0, 0, 0, 0)), _ai_mask(img))

        out = io.BytesIO()
        output.save(out, format='PNG')
        out.seek(0)
        return send_file(out, mimetype='image/png', as_attachment=True, download_name='no-bg.png')
    except Exception as e:
        app.logger.exception('remove-bg failed')
        return jsonify({'error': str(e)}), 500


# ---------- Remove Background without AI: "Box" mode (OpenCV GrabCut) ----------
# The browser sends its working copy of the photo (already upright), the box the
# person drew, and optionally their brush marks. We answer with a greyscale mask
# of the same size: white = keep, black = remove. GrabCut runs on a smaller copy
# (it is slow on big images) and the mask is scaled back up with a soft edge.
GRABCUT_MAX = 720
GRABCUT_INPUT_MAX = 2400


def _grabcut_mask(rgb, rect, hints=None, iterations=5):
    """rgb: HxWx3 uint8. rect: (x, y, w, h) in rgb's pixels. hints: HxW uint8 or None
    (255 = keep, 0 = remove, anything else = no mark). Returns an HxW uint8 alpha mask."""
    import cv2
    import numpy as np

    h, w = rgb.shape[:2]
    x, y, rw, rh = (int(round(v)) for v in rect)
    x, y = max(0, min(x, w - 1)), max(0, min(y, h - 1))
    rw, rh = max(1, min(rw, w - x)), max(1, min(rh, h - y))
    if rw < 4 or rh < 4:
        raise ValueError('Draw a bigger box around the subject.')

    scale = min(1.0, GRABCUT_MAX / max(w, h))
    sw, sh = max(1, round(w * scale)), max(1, round(h * scale))
    small = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA) if scale < 1 else rgb
    bgr = np.ascontiguousarray(small[:, :, ::-1])

    sx, sy = int(x * scale), int(y * scale)
    ex, ey = min(sw, int(np.ceil((x + rw) * scale))), min(sh, int(np.ceil((y + rh) * scale)))
    mask = np.full((sh, sw), cv2.GC_BGD, np.uint8)
    mask[sy:ey, sx:ex] = cv2.GC_PR_FGD

    if hints is not None:
        small_hints = cv2.resize(hints, (sw, sh), interpolation=cv2.INTER_NEAREST) if scale < 1 else hints
        mask[small_hints == 255] = cv2.GC_FGD
        mask[small_hints == 0] = cv2.GC_BGD

    # GrabCut needs some of both to learn the colours from.
    if not (mask == cv2.GC_BGD).any():
        mask[0, :] = mask[-1, :] = mask[:, 0] = mask[:, -1] = cv2.GC_BGD
    if not np.isin(mask, (cv2.GC_FGD, cv2.GC_PR_FGD)).any():
        return np.zeros((h, w), np.uint8)

    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.grabCut(bgr, mask, None, bgd, fgd, iterations, cv2.GC_INIT_WITH_MASK)
    fg = np.isin(mask, (cv2.GC_FGD, cv2.GC_PR_FGD)).astype(np.uint8)

    # Drop specks and fill pinholes: keep pieces bigger than 1% of the largest one,
    # plus anything the person marked "keep".
    count, labels, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
    if count > 2:
        areas = stats[1:, cv2.CC_STAT_AREA]
        keep = np.zeros(count, bool)
        keep[1:] = areas >= max(1, areas.max() * 0.01)
        if hints is not None:
            marked = np.unique(labels[(small_hints == 255) & (fg == 1)])
            keep[marked[marked > 0]] = True
        fg = keep[labels].astype(np.uint8)
    holes = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    fg = np.maximum(fg, holes)
    if hints is not None:
        fg[small_hints == 0] = 0

    # Back to full size with a soft, anti-aliased edge.
    alpha = fg.astype(np.float32)
    if scale < 1:
        alpha = cv2.resize(alpha, (w, h), interpolation=cv2.INTER_LINEAR)
        sigma = max(0.6, 0.5 / scale)
        alpha = cv2.GaussianBlur(alpha, (0, 0), sigma)
        alpha = np.clip((alpha - 0.5) * 2.2 + 0.5, 0, 1)
    out = (alpha * 255).round().astype(np.uint8)
    if hints is not None:
        out[hints == 255] = 255
        out[hints == 0] = 0
    return out


@app.route('/api/remove-bg/box', methods=['POST'])
def remove_bg_box():
    """Cut out the subject inside a box (no AI). Returns a greyscale PNG mask."""
    import numpy as np
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    try:
        rect = tuple(float(request.form[k]) for k in ('x', 'y', 'w', 'h'))
        if not all(np.isfinite(rect)):
            raise ValueError
    except (KeyError, ValueError):
        return jsonify({'error': 'Draw a box around the subject first.'}), 400
    try:
        img = _open_upload_image(request.files['file'], 'RGB')
    except Exception:
        return jsonify({'error': 'That file is not a supported image.'}), 400
    if max(img.size) > GRABCUT_INPUT_MAX:
        return jsonify({'error': 'That image is too large.'}), 400

    hints = None
    if 'hints' in request.files:
        try:
            hint_img = Image.open(request.files['hints'].stream).convert('L')
        except Exception:
            return jsonify({'error': 'The brush marks could not be read.'}), 400
        if hint_img.size != img.size:
            hint_img = hint_img.resize(img.size, Image.Resampling.NEAREST)
        hints = np.asarray(hint_img, dtype=np.uint8)

    try:
        mask = _grabcut_mask(np.asarray(img, dtype=np.uint8), rect, hints)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        app.logger.exception('remove-bg box failed')
        return jsonify({'error': 'The cut-out failed. Try a slightly bigger box.'}), 500

    out = io.BytesIO()
    Image.fromarray(mask, 'L').save(out, format='PNG', optimize=False, compress_level=1)
    out.seek(0)
    response = send_file(out, mimetype='image/png')
    response.headers['Cache-Control'] = 'no-store'
    return response


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)

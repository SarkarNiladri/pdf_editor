from flask import Flask, request, jsonify, send_file
import fitz
import io
import base64
import tempfile
import subprocess
import shutil
from pathlib import Path

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB for JSON body (base64 PDF)

PROJECT_ROOT = Path(__file__).resolve().parent

from converter import (
    find_libreoffice, office_to_pdf, images_to_pdf, pdf_to_word,
    pdf_to_images, merge_pdfs, safe_name, cleanup_images_dir,
    UPLOAD_DIR as CONVERTER_UPLOAD_DIR,
    _validate_file, _cleanup_old_outputs, _run_conversion,
)


@app.route('/')
def index():
    return send_file(PROJECT_ROOT / 'index.html', mimetype='text/html')

def span_color(span):
    return '#{:06x}'.format(int(span.get('color', 0)) & 0xFFFFFF)

def span_is_bold(span):
    # Font name heuristic is the most reliable indicator.
    # The PDF font-flag bit 6 (value 64) is "ForceBold" — a synthetic hint
    # that some fonts set.  We check it as a secondary signal but rely
    # primarily on the font name containing 'bold'.
    font_name = (span.get('font') or '').lower()
    if 'bold' in font_name:
        return True
    flags = int(span.get('flags', 0))
    return bool(flags & 64)  # PDF ForceBold flag (bit 6)

def span_is_italic(span):
    f = (span.get('font') or '').lower()
    return 'italic' in f or 'oblique' in f or bool(int(span.get('flags', 0)) & 2)

def choose_font(font):
    f = (font or '').lower()
    if 'courier' in f: return 'cour'
    if 'times' in f: return 'tiro'
    if 'symbol' in f: return 'symb'
    if 'zapfdingbats' in f: return 'zapfdingbats'
    return 'helv'

def hex_to_rgb01(value):
    value = (value or '#000000').lstrip('#')
    if len(value) != 6: return (0, 0, 0)
    try:
        return tuple(int(value[i:i+2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)

def rect_from_data(data):
    r = data or {}
    return fitz.Rect(float(r.get('x0', 0)), float(r.get('y0', 0)), float(r.get('x1', 0)), float(r.get('y1', 0)))

def extract_pages(data):
    doc = fitz.open(stream=data, filetype='pdf')
    pages = []
    try:
        for pno, page in enumerate(doc):
            line_records = []
            blocks = page.get_text('dict', flags=fitz.TEXTFLAGS_TEXT).get('blocks', [])
            for block in blocks:
                if block.get('type') != 0:
                    continue
                for line in block.get('lines', []):
                    spans = [sp for sp in line.get('spans', []) if sp.get('text', '')]
                    text = ''.join(sp.get('text', '') for sp in spans).rstrip()
                    if not text.strip() or not spans:
                        continue
                    lb = line.get('bbox', [0, 0, 0, 0])
                    style = max([sp for sp in spans if sp.get('text', '').strip()] or spans, key=lambda sp: float(sp.get('size', 12)))
                    line_records.append({
                        'text': text,
                        'x0': float(lb[0]), 'y0': float(lb[1]), 'x1': float(lb[2]), 'y1': float(lb[3]),
                        'fontSize': float(style.get('size', 12)), 'font': style.get('font', ''),
                        'color': span_color(style), 'bold': span_is_bold(style), 'italic': span_is_italic(style),
                    })

            line_records.sort(key=lambda r: (r['y0'], r['x0']))
            groups = []
            for line in line_records:
                placed = False
                for group in reversed(groups[-8:]):
                    prev = group[-1]
                    gap = line['y0'] - prev['y1']
                    x_delta = abs(line['x0'] - prev['x0'])
                    same_font = (line['font'] or '').lower() == (prev['font'] or '').lower()
                    similar_size = min(line['fontSize'], prev['fontSize']) / max(line['fontSize'], prev['fontSize']) >= 0.70
                    same_style = line['color'] == prev['color'] and line['bold'] == prev['bold'] and line['italic'] == prev['italic']
                    if 0 <= gap <= max(6.0, min(line['fontSize'], prev['fontSize']) * 0.30) and x_delta <= 10.0 and same_font and similar_size and same_style:
                        group.append(line)
                        placed = True
                        break
                if not placed:
                    groups.append([line])

            items = []
            for idx, group in enumerate(groups):
                text = '\n'.join(x['text'] for x in group).strip()
                if not text:
                    continue
                style = max(group, key=lambda x: x['fontSize'])
                items.append({
                    'id': f'p{pno+1}_b{idx}', 'page': pno+1, 'text': text,
                    'x0': min(x['x0'] for x in group), 'y0': min(x['y0'] for x in group),
                    'x1': max(x['x1'] for x in group), 'y1': max(x['y1'] for x in group),
                    'fontSize': style['fontSize'], 'font': style['font'], 'color': style['color'],
                    'bold': style['bold'], 'italic': style['italic'], 'singleLine': len(group) == 1,
                })
            pages.append({'page': pno+1, 'width': page.rect.width, 'height': page.rect.height, 'items': items})
        return pages
    finally:
        doc.close()

@app.post('/api/extract')
def extract():
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No PDF uploaded'}), 400
    data = file.read()
    # Validate PDF magic bytes
    if not data.startswith(b'%PDF'):
        return jsonify({'error': 'The uploaded file is not a valid PDF.'}), 400
    try:
        pages = extract_pages(data)
    except Exception as e:
        return jsonify({'error': f'Invalid PDF: {e}'}), 400
    return jsonify({'pdf': base64.b64encode(data).decode('ascii'), 'pageCount': len(pages), 'pages': pages})

def insert_text(page, rect, text, font_size, color, fontname):
    """Insert text into a PDF page. Returns True if text fit, False if truncated."""
    if not text.strip():
        return True
    result = page.insert_textbox(rect, text, fontsize=font_size, fontname=fontname, color=color, align=fitz.TEXT_ALIGN_LEFT, overlay=True)
    if result < 0:
        expanded = fitz.Rect(rect.x0, rect.y0, rect.x1, min(page.rect.y1 - 2, rect.y1 + abs(result) + font_size * 1.5))
        result2 = page.insert_textbox(expanded, text, fontsize=font_size, fontname=fontname, color=color, align=fitz.TEXT_ALIGN_LEFT, overlay=True)
        return result2 >= 0
    return True

def decode_image_data(value):
    if not value or not isinstance(value, str):
        raise ValueError('Missing image data')
    if value.startswith('data:'):
        try:
            header, encoded = value.split(',', 1)
        except ValueError:
            raise ValueError('Invalid image data URL')
        if ';base64' not in header.lower():
            raise ValueError('Image data URL must be base64 encoded')
        raw = base64.b64decode(encoded, validate=True)
    else:
        raw = base64.b64decode(value, validate=True)
    if not raw:
        raise ValueError('Empty image data')
    if raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff'):
        return raw

    try:
        from PIL import Image
        import io as _io
        with Image.open(_io.BytesIO(raw)) as im:
            buf = _io.BytesIO()
            im.convert('RGBA').save(buf, format='PNG')
            return buf.getvalue()
    except ImportError:
        raise ValueError('Unsupported image format. Please re-add the image as PNG or JPG.')
    except Exception as exc:
        raise ValueError(f'Unsupported or corrupt image format: {exc}')

def insert_image(page, edit):
    rect = rect_from_data(edit.get('rect'))
    if rect.width <= 0 or rect.height <= 0:
        return
    raw = decode_image_data(edit.get('data'))
    page.insert_image(rect, stream=raw, keep_proportion=False, overlay=True)

def apply_edits(raw, edits):
    doc = fitz.open(stream=raw, filetype='pdf')
    try:
        # Paint order: respect creation order so that later-added objects appear
        # on top, regardless of type.  Existing (edited) text has no order and
        # is rendered first as the base layer.
        existing_edits = [e for e in (edits or []) if e.get('type') not in ('shape', 'image', 'add') and not e.get('order')]
        added_edits = [e for e in (edits or []) if e.get('order')]
        added_edits.sort(key=lambda e: int(e.get('order', 0)))
        ordered_edits = existing_edits + added_edits
        for edit in ordered_edits:
            page_value = edit.get('page', edit.get('pageNum', edit.get('page_number')))
            if page_value is None:
                continue
            try:
                page_no = int(page_value) - 1
            except (TypeError, ValueError):
                continue
            if not 0 <= page_no < len(doc):
                continue
            page = doc[page_no]
            edit_type = edit.get('type')
            if edit_type == 'image':
                if edit.get('deleted'):
                    continue
                insert_image(page, edit)
                continue
            if edit_type == 'shape':
                shape_rect = rect_from_data(edit.get('rect'))
                if shape_rect.width <= 0 or shape_rect.height <= 0:
                    continue
                fill = hex_to_rgb01(edit.get('fill') or '#1677ff')
                stroke = hex_to_rgb01(edit.get('stroke') or edit.get('fill') or '#1677ff')
                try:
                    width = max(0.5, float(edit.get('strokeWidth') or 1))
                except (TypeError, ValueError):
                    width = 1
                if edit.get('shape') == 'ellipse':
                    page.draw_oval(shape_rect, color=stroke, fill=fill, width=width, overlay=True)
                else:
                    page.draw_rect(shape_rect, color=stroke, fill=fill, width=width, overlay=True)
                continue

            is_added = edit_type == 'add'
            new_text = edit.get('text', '') or ''
            target_rect = rect_from_data(edit.get('rect'))
            original_rect = rect_from_data(edit.get('originalRect') or edit.get('rect'))
            if not is_added:
                # NOTE: Redaction-based removal is an approximation. If two text
                # regions overlap (common in complex PDF layouts), redacting one
                # region may destroy parts of adjacent, unedited text within the
                # redaction zone. This is an inherent limitation of the overlay
                # editing approach.
                cleanup = fitz.Rect(original_rect.x0 - 1, original_rect.y0 - 1, original_rect.x1 + 1, original_rect.y1 + 1)
                page.add_redact_annot(cleanup, fill=False)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=fitz.PDF_REDACT_TEXT_REMOVE)
            if edit.get('deleted') or not new_text.strip():
                continue
            insert_text(page, target_rect, new_text, float(edit.get('fontSize') or 12), hex_to_rgb01(edit.get('color') or '#000000'), choose_font(edit.get('font', '')))
        out = io.BytesIO()
        doc.save(out, garbage=4, deflate=True, clean=True)
        out.seek(0)
        return out
    finally:
        doc.close()

def decode_pdf(payload):
    pdf_b64 = payload.get('pdf')
    if not pdf_b64:
        raise ValueError('Missing PDF data')
    return base64.b64decode(pdf_b64)

@app.post('/api/preview')
def preview():
    try:
        payload = request.get_json(force=True) or {}
        out = apply_edits(decode_pdf(payload), payload.get('edits', []))
        return send_file(out, mimetype='application/pdf', as_attachment=False)
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500

@app.post('/api/edit')
def edit():
    try:
        payload = request.get_json(force=True) or {}
        out = apply_edits(decode_pdf(payload), payload.get('edits', []))
        return send_file(out, mimetype='application/pdf', as_attachment=True, download_name='edited.pdf')
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500

@app.get('/converter')
def converter_ui():
    converter_html = PROJECT_ROOT / 'converter.html'
    if not converter_html.is_file():
        return jsonify({'error': f'Converter UI not found: {converter_html}'}), 500
    return send_file(converter_html, mimetype='text/html')


# --- Reuse converter.py's shared logic instead of duplicating route handlers ---

@app.get('/api/capabilities')
def converter_capabilities():
    office_available = bool(find_libreoffice())
    return jsonify({
        'word_to_pdf': office_available,
        'excel_to_pdf': office_available,
        'powerpoint_to_pdf': office_available,
        'pdf_to_word': True,
        'images_to_pdf': True,
        'pdf_to_images': True,
        'merge_pdf': True,
    })


@app.post('/api/convert')
def converter_convert():
    conversion = (request.form.get('conversion') or '').strip().lower()
    uploads = request.files.getlist('files')
    if not uploads:
        return jsonify({'error': 'Please select at least one file.'}), 400

    work_dir = Path(tempfile.mkdtemp(prefix='converter_', dir=str(CONVERTER_UPLOAD_DIR)))
    try:
        files = []
        for upload in uploads:
            if not upload.filename:
                continue
            target = work_dir / safe_name(upload.filename)
            upload.save(target)
            try:
                _validate_file(target)
            except ValueError as e:
                return jsonify({'error': str(e)}), 400
            files.append(target)
        if not files:
            return jsonify({'error': 'No valid files were uploaded.'}), 400

        _cleanup_old_outputs()
        resp, status = _run_conversion(conversion, files)
        return resp, status

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Conversion timed out.'}), 504
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)

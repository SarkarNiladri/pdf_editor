import os
import io
import base64
import tempfile
import subprocess
import shutil
import re
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, send_file
import fitz
from dotenv import load_dotenv
import openai
import pdfplumber
import dateparser

# Load environment variables from .env
load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

PROJECT_ROOT = Path(__file__).resolve().parent

# Import converter functions
from converter import (
    find_libreoffice, office_to_pdf, images_to_pdf, pdf_to_word,
    pdf_to_images, merge_pdfs, safe_name, cleanup_images_dir,
    UPLOAD_DIR as CONVERTER_UPLOAD_DIR,
    _validate_file, _cleanup_old_outputs, _run_conversion,
)


# ---------- Routes ----------
@app.route('/')
def index():
    return send_file(PROJECT_ROOT / 'index.html', mimetype='text/html')


# ---------- PDF text extraction ----------
def span_color(span):
    return '#{:06x}'.format(int(span.get('color', 0)) & 0xFFFFFF)

def span_is_bold(span):
    font_name = (span.get('font') or '').lower()
    if 'bold' in font_name:
        return True
    flags = int(span.get('flags', 0))
    return bool(flags & 64)

def span_is_italic(span):
    f = (span.get('font') or '').lower()
    return 'italic' in f or 'oblique' in f or bool(int(span.get('flags', 0)) & 2)

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
            flags = getattr(fitz, 'TEXTFLAGS_TEXT', 0)
            blocks = page.get_text('dict', flags=flags).get('blocks', [])
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
    if not data.startswith(b'%PDF'):
        return jsonify({'error': 'The uploaded file is not a valid PDF.'}), 400
    try:
        pages = extract_pages(data)
    except Exception as e:
        return jsonify({'error': f'Invalid PDF: {e}'}), 400
    return jsonify({'pdf': base64.b64encode(data).decode('ascii'), 'pageCount': len(pages), 'pages': pages})


# ---------- Render page endpoint ----------
@app.post('/api/render-page')
def render_page():
    payload = request.get_json(force=True) or {}
    pdf_b64 = payload.get('pdf')
    if not pdf_b64:
        return jsonify({'error': 'Missing PDF data'}), 400
    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception:
        return jsonify({'error': 'Invalid base64 PDF data'}), 400
    page_num = int(payload.get('page', 1)) - 1
    scale = float(payload.get('scale', 1.5))
    if scale <= 0 or scale > 5:
        scale = 1.5

    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    try:
        if page_num < 0 or page_num >= len(doc):
            return jsonify({'error': 'Page number out of range'}), 400
        page = doc[page_num]
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_data = pix.tobytes('png')
        return send_file(io.BytesIO(img_data), mimetype='image/png')
    finally:
        doc.close()


# ---------- Editing ----------
def get_pdf_font_name(family, bold=False, italic=False):
    family_lower = (family or '').lower()
    if 'times' in family_lower or 'roman' in family_lower:
        base = 'Times-Roman'
    elif 'courier' in family_lower:
        base = 'Courier'
    elif 'symbol' in family_lower:
        base = 'Symbol'
    elif 'zapf' in family_lower or 'dingbat' in family_lower:
        base = 'ZapfDingbats'
    else:
        base = 'Helvetica'

    if base in ('Symbol', 'ZapfDingbats'):
        return base

    if bold and italic:
        return f'{base}-BoldOblique' if base == 'Helvetica' else f'{base}-BoldItalic'
    elif bold:
        return f'{base}-Bold'
    elif italic:
        return f'{base}-Oblique' if base == 'Helvetica' else f'{base}-Italic'
    else:
        return base

def insert_text(page, rect, text, font_size, color, fontname, bold=False, italic=False):
    if not text or not text.strip():
        return True

    lines = text.split('\n')
    if not lines:
        return True

    pdf_font = get_pdf_font_name(fontname, bold, italic)
    line_height = font_size * 1.2

    # Shift x by 6px to the right, y by 0.8*font_size
    x = rect.x0 + 6
    y = rect.y0 + font_size * 0.8

    for line in lines:
        page.insert_text(
            (x, y),
            line,
            fontsize=font_size,
            fontname=pdf_font,
            color=color,
            overlay=True
        )
        y += line_height
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
                fill = hex_to_rgb01(edit.get('fill') or '#6366f1')
                stroke = hex_to_rgb01(edit.get('stroke') or edit.get('fill') or '#6366f1')
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
                cleanup = fitz.Rect(original_rect.x0 - 1, original_rect.y0 - 1, original_rect.x1 + 1, original_rect.y1 + 1)
                page.add_redact_annot(cleanup, fill=False)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=fitz.PDF_REDACT_TEXT_REMOVE)
            if edit.get('deleted') or not new_text.strip():
                continue
            font = edit.get('font')
            if not is_added and not edit.get('font_changed'):
                font = edit.get('originalFont', edit.get('font', 'Helvetica'))
            else:
                font = edit.get('font', 'Helvetica')
            bold = edit.get('bold', False)
            italic = edit.get('italic', False)
            insert_text(
                page, target_rect, new_text,
                float(edit.get('fontSize') or 12),
                hex_to_rgb01(edit.get('color') or '#000000'),
                font,
                bold,
                italic
            )
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


# ---------- AI Summarization ----------
def extract_all_text(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text

@app.post('/api/summarize')
def summarize_pdf():
    data = request.get_json()
    pdf_b64 = data.get('pdf')
    if not pdf_b64:
        return jsonify({'error': 'No PDF data provided'}), 400

    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        return jsonify({'error': 'OpenAI API key is not configured. Please set OPENAI_API_KEY in .env'}), 500

    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception:
        return jsonify({'error': 'Invalid base64 PDF'}), 400

    full_text = extract_all_text(pdf_bytes)
    if not full_text.strip():
        return jsonify({'error': 'No text found in PDF. The document might be scanned or image-based.'}), 400

    max_chars = 12000
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + "\n\n[Document truncated due to length...]"

    try:
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a document summarization assistant. "
                        "Summarize the provided document in 5 to 7 concise sentences, "
                        "capturing the main purpose, key arguments, and conclusions. "
                        "Use clear, professional language."
                    )
                },
                {
                    "role": "user",
                    "content": f"Document text:\n\n{full_text}"
                }
            ],
            max_tokens=300,
            temperature=0.5
        )
        summary = response.choices[0].message.content.strip()
        return jsonify({'summary': summary})

    except openai.AuthenticationError:
        return jsonify({'error': 'Invalid OpenAI API key. Please check your OPENAI_API_KEY in .env'}), 401
    except openai.RateLimitError:
        return jsonify({'error': 'OpenAI API rate limit exceeded. Please wait and try again.'}), 429
    except Exception as e:
        return jsonify({'error': f'OpenAI API error: {str(e)}'}), 500


# ---------- ATS Compatibility Checker ----------
ACTION_VERBS = {
    "achieved", "managed", "led", "developed", "built", "created", "designed",
    "implemented", "improved", "increased", "decreased", "reduced", "solved",
    "analyzed", "coordinated", "delivered", "launched", "optimized", "streamlined",
    "facilitated", "mentored", "spearheaded", "drove", "executed", "generated",
    "negotiated", "presented", "supervised", "trained", "transformed", "introduced"
}

TECH_SKILLS = {
    "python", "java", "c++", "c#", "javascript", "typescript", "react", "angular",
    "vue", "node.js", "express", "django", "flask", "spring", "sql", "mysql",
    "postgresql", "mongodb", "docker", "kubernetes", "aws", "azure", "gcp",
    "linux", "git", "jenkins", "ci/cd", "agile", "scrum", "kanban", "jira",
    "confluence", "data analysis", "machine learning", "deep learning", "nlp",
    "computer vision", "tableau", "power bi", "excel", "vba", "html", "css",
    "bootstrap", "sass", "webpack", "rest api", "graphql", "microservices",
    "unity", "unreal", "android", "ios", "swift", "kotlin", "rust", "go",
    "perl", "ruby", "php", "wordpress", "shopify", "salesforce", "workday"
}

def extract_text_from_file(file_storage):
    filename = file_storage.filename.lower()
    if filename.endswith('.pdf'):
        with pdfplumber.open(file_storage) as pdf:
            text = ""
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text += page_text + "\n"
        return text
    elif filename.endswith('.docx'):
        from docx import Document as DocxDocument
        doc = DocxDocument(file_storage)
        text = "\n".join([para.text for para in doc.paragraphs])
        return text
    else:
        raise ValueError("Unsupported file format. Please upload a PDF or DOCX.")

def ats_check(file_storage):
    try:
        text = extract_text_from_file(file_storage)
    except Exception as e:
        return {"error": f"Could not read file: {str(e)}"}

    if not text or len(text.strip()) < 50:
        return {"error": "Document appears empty or contains very little text."}

    text_lower = text.lower()
    words = text_lower.split()
    word_count = len(words)

    parseable_score = 10
    if word_count < 100:
        parseable_score = 0
    elif word_count < 200:
        parseable_score = 5
    alpha_chars = sum(c.isalpha() for c in text)
    total_chars = len(text)
    if total_chars > 0 and alpha_chars / total_chars < 0.4:
        parseable_score = max(0, parseable_score - 5)

    section_keywords = {
        "summary": ["summary", "profile", "about me", "objective"],
        "education": ["education", "academic", "university", "college", "school", "degree"],
        "experience": ["experience", "employment", "work history", "professional experience"],
        "skills": ["skills", "technical skills", "core competencies", "expertise"],
        "projects": ["projects", "personal projects", "academic projects"],
        "certifications": ["certifications", "certificates", "licenses", "courses"],
        "languages": ["languages", "language", "bilingual"]
    }
    sections_found = 0
    for section, keywords in section_keywords.items():
        if any(kw in text_lower for kw in keywords):
            sections_found += 1
    structure_score = min(20, sections_found * 3)

    formatting_score = 0
    bullet_lines = re.findall(r'^\s*[-*•\d]+\.?\s+', text, re.MULTILINE)
    if len(bullet_lines) >= 5:
        formatting_score += 7
    elif len(bullet_lines) >= 2:
        formatting_score += 4
    if "\n\n\n" not in text:
        formatting_score += 4
    elif "\n\n\n\n" not in text:
        formatting_score += 2
    formatting_score = min(15, formatting_score)

    contact_score = 0
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    if re.search(email_pattern, text):
        contact_score += 4
    phone_pattern = r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}'
    if re.search(phone_pattern, text):
        contact_score += 3
    if re.search(r'(linkedin\.com/in/|github\.com/)', text_lower):
        contact_score += 3
    contact_score = min(10, contact_score)

    date_score = 0
    date_pattern = r'\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}\b|\b\d{1,2}/\d{1,2}/\d{4}\b|\b\d{4}\b'
    raw_dates = re.findall(date_pattern, text_lower, re.IGNORECASE)
    parsed_years = []
    for d in raw_dates:
        parsed = dateparser.parse(d, languages=['en'])
        if parsed:
            parsed_years.append(parsed.year)
    if len(parsed_years) >= 2:
        years = sorted(parsed_years, reverse=True)
        if all(years[i] >= years[i+1] for i in range(len(years)-1)):
            date_score += 8
        current_year = datetime.now().year
        if any(year >= current_year - 2 for year in years):
            date_score += 4
        if max(years) - min(years) < 20:
            date_score += 3
    elif len(parsed_years) == 1:
        date_score = 5
    date_score = min(15, date_score)

    quality_score = 0
    if word_count > 300:
        quality_score += 5
    elif word_count > 150:
        quality_score += 3
    verb_count = sum(1 for w in words if w in ACTION_VERBS)
    if verb_count >= 10:
        quality_score += 5
    elif verb_count >= 5:
        quality_score += 3
    numbers = re.findall(r'\b\d+%|\$\d+|\d+\s*(percent|%)|\b\d{1,3}(,\d{3})*\b', text)
    if len(numbers) >= 3:
        quality_score += 5
    elif len(numbers) >= 1:
        quality_score += 2
    quality_score = min(15, quality_score)

    skill_count = sum(1 for skill in TECH_SKILLS if skill in text_lower)
    if skill_count >= 10:
        skill_score = 15
    elif skill_count >= 6:
        skill_score = 12
    elif skill_count >= 3:
        skill_score = 8
    else:
        skill_score = 4

    total_score = (
        (parseable_score / 10) * 10 +
        (structure_score / 20) * 20 +
        (formatting_score / 15) * 15 +
        (contact_score / 10) * 10 +
        (date_score / 15) * 15 +
        (quality_score / 15) * 15 +
        (skill_score / 15) * 15
    )
    total_score = round(total_score, 1)

    return {
        "total": total_score,
        "breakdown": {
            "parseability": {"score": parseable_score, "max": 10, "weight": 0.10},
            "structure": {"score": structure_score, "max": 20, "weight": 0.20},
            "formatting": {"score": formatting_score, "max": 15, "weight": 0.15},
            "contact": {"score": contact_score, "max": 10, "weight": 0.10},
            "date_consistency": {"score": date_score, "max": 15, "weight": 0.15},
            "content_quality": {"score": quality_score, "max": 15, "weight": 0.15},
            "skill_coverage": {"score": skill_score, "max": 15, "weight": 0.15},
        },
        "word_count": word_count,
        "sections_found": sections_found,
        "skills_detected": skill_count,
    }

@app.post('/api/ats-check')
def ats_check_endpoint():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ['pdf', 'docx']:
        return jsonify({'error': 'Unsupported file format. Please upload a PDF or DOCX.'}), 400

    try:
        result = ats_check(file)
        if 'error' in result:
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': f'Analysis failed: {str(e)}'}), 500


# ---------- Converter routes ----------
@app.get('/converter')
def converter_ui():
    converter_html = PROJECT_ROOT / 'converter.html'
    if not converter_html.is_file():
        return jsonify({'error': f'Converter UI not found: {converter_html}'}), 500
    return send_file(converter_html, mimetype='text/html')

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
    target_format = request.form.get('target_format')  # For image conversion

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
        resp, status = _run_conversion(conversion, files, target_format=target_format)
        return resp, status

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Conversion timed out.'}), 504
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
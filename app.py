import os
import io
import base64
import tempfile
import subprocess
import shutil
import re
import json
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, send_file
import fitz
from dotenv import load_dotenv
import openai
import pdfplumber
import dateparser

load_dotenv()
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

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


# ---------- PDF text extraction ----------
def span_color(span):
    return '#{:06x}'.format(int(span.get('color', 0)) & 0xFFFFFF)

def extract_pages(data):
    doc = fitz.open(stream=data, filetype='pdf')
    pages = []
    try:
        for pno, page in enumerate(doc):
            # Since we no longer edit existing text, we don't need to extract every line!
            # This makes uploading massive PDFs lightning fast.
            pages.append({'page': pno+1, 'width': page.rect.width, 'height': page.rect.height, 'items': []})
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
        return jsonify({'error': f'Invalid PDF: {e}'}), 400
    return jsonify({'pdf': base64.b64encode(data).decode('ascii'), 'pageCount': len(pages), 'pages': pages})


# ---------- Render page ----------
@app.post('/api/render-page')
def render_page():
    payload = request.get_json(force=True) or {}
    pdf_b64 = payload.get('pdf')
    if not pdf_b64: return jsonify({'error': 'Missing PDF data'}), 400
    try: pdf_bytes = base64.b64decode(pdf_b64)
    except Exception: return jsonify({'error': 'Invalid base64 PDF data'}), 400
    
    page_num = int(payload.get('page', 1)) - 1
    scale = float(payload.get('scale', 1.5))
    if scale <= 0 or scale > 5: scale = 1.5

    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    try:
        if page_num < 0 or page_num >= len(doc): return jsonify({'error': 'Page number out of range'}), 400
        page = doc[page_num]
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_data = pix.tobytes('png')
        return send_file(io.BytesIO(img_data), mimetype='image/png')
    finally:
        doc.close()
def hex_to_rgb01(value):
    value = (value or '#000000').lstrip('#')
    if len(value) != 6: return (0, 0, 0)
    try:
        return tuple(int(value[i:i+2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)

# ---------- Editing (Simplified) ----------
def insert_text(page, rect, text, font_size, color, font_family):
    if not text or not text.strip(): return True
    lines = text.split('\n')
    line_height = font_size * 1.2
    x = rect.x0 + 8
    y = rect.y0 + font_size * 0.8

    # Map UI fonts to PyMuPDF's standard built-in fonts
    if 'times' in font_family.lower():
        fontname = 'tiro'  # Times Roman
    elif 'courier' in font_family.lower():
        fontname = 'cour'  # Courier
    else:
        fontname = 'helv'  # Helvetica (Arial equivalent)

    for line in lines:
        page.insert_text((x, y), line, fontsize=font_size, color=color, fontname=fontname, overlay=True)
        y += line_height
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
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            buf = io.BytesIO()
            im.convert('RGBA').save(buf, format='PNG')
            return buf.getvalue()
    except Exception as exc:
        raise ValueError(f'Unsupported or corrupt image format: {exc}')

def apply_edits(raw, edits):
    doc = fitz.open(stream=raw, filetype='pdf')
    try:
        # Sort by order so they stack correctly
        ordered_edits = sorted(edits, key=lambda e: int(e.get('order', 0)))
        for edit in ordered_edits:
            page_value = edit.get('page')
            if page_value is None: continue
            try: page_no = int(page_value) - 1
            except (TypeError, ValueError): continue
            if not 0 <= page_no < len(doc): continue
            
            page = doc[page_no]
            edit_type = edit.get('type')
            rect_data = edit.get('rect', {})
            rect = fitz.Rect(float(rect_data.get('x0',0)), float(rect_data.get('y0',0)), float(rect_data.get('x1',0)), float(rect_data.get('y1',0)))
            
            if edit_type == 'image':
                if edit.get('deleted'): continue
                if rect.width > 0 and rect.height > 0:
                    raw_img = decode_image_data(edit.get('data'))
                    page.insert_image(rect, stream=raw_img, keep_proportion=False, overlay=True)
            
            elif edit_type == 'shape':
                if rect.width <= 0 or rect.height <= 0: continue
                fill = hex_to_rgb01(edit.get('fill') or '#6366f1')
                stroke = hex_to_rgb01(edit.get('stroke') or edit.get('fill') or '#6366f1')
                try: width = max(0.5, float(edit.get('strokeWidth') or 1))
                except (TypeError, ValueError): width = 1
                
                if edit.get('shape') == 'ellipse': page.draw_oval(rect, color=stroke, fill=fill, width=width, overlay=True)
                else: page.draw_rect(rect, color=stroke, fill=fill, width=width, overlay=True)

            elif edit_type == 'add':
                if edit.get('deleted') or not edit.get('text', '').strip(): continue
                insert_text(
                    page, rect, edit.get('text', ''),
                    float(edit.get('fontSize') or 12),
                    hex_to_rgb01(edit.get('color') or '#000000'),
                    edit.get('font', 'Arial')
                )
                
        out = io.BytesIO()
        doc.save(out, garbage=4, deflate=True, clean=True)
        out.seek(0)
        return out
    finally:
        doc.close()

def decode_pdf(payload):
    pdf_b64 = payload.get('pdf')
    if not pdf_b64: raise ValueError('Missing PDF data')
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
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500


# ---------- Summarization ----------
def extract_all_text(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    text = ""
    for page in doc: text += page.get_text()
    doc.close()
    return text

@app.post('/api/summarize')
def summarize_pdf():
    data = request.get_json()
    pdf_b64 = data.get('pdf')
    if not pdf_b64: return jsonify({'error': 'No PDF data provided'}), 400

    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key: return jsonify({'error': 'OpenAI API key is not configured.'}), 500

    try: pdf_bytes = base64.b64decode(pdf_b64)
    except Exception: return jsonify({'error': 'Invalid base64 PDF'}), 400

    full_text = extract_all_text(pdf_bytes)
    if not full_text.strip(): return jsonify({'error': 'No text found in PDF.'}), 400

    max_chars = 12000
    if len(full_text) > max_chars: full_text = full_text[:max_chars] + "\n\n[Document truncated...]"

    try:
        client = openai.OpenAI(api_key=api_key)
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
    if re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text): contact_score += 4
    if re.search(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', text): contact_score += 3
    if re.search(r'(linkedin\.com/in/|github\.com/)', text_lower): contact_score += 3
    contact_score = min(10, contact_score)

    date_score = 0
    raw_dates = re.findall(r'\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}\b|\b\d{1,2}/\d{1,2}/\d{4}\b|\b\d{4}\b', text_lower, re.IGNORECASE)
    parsed_years = [dateparser.parse(d, languages=['en']).year for d in raw_dates if dateparser.parse(d, languages=['en'])]
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
    numbers = re.findall(r'\b\d+%|\$\d+|\d+\s*(percent|%)|\b\d{1,3}(,\d{3})*\b', text)
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
        client = openai.OpenAI(api_key=api_key)
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
        img_bytes = pix.tobytes("png")
        preview_img_b64 = base64.b64encode(img_bytes).decode('utf-8')
        doc.close()

        return jsonify({'match_score': ai_data.get('match_score', 0), 'pdf_b64': pdf_b64, 'preview_img_b64': preview_img_b64, 'missing_keywords': ai_data.get('missing_keywords', []), 'improvements': ai_data.get('improvements', [])})
    except Exception as e:
        return jsonify({'error': f'Failed to tailor resume: {str(e)}'}), 500


# ---------- Converter routes ----------
@app.get('/converter')
def converter_ui():
    converter_html = PROJECT_ROOT / 'converter.html'
    if not converter_html.is_file(): return jsonify({'error': f'Converter UI not found'}), 500
    return send_file(converter_html, mimetype='text/html')

@app.get('/api/capabilities')
def converter_capabilities():
    office_available = bool(find_libreoffice())
    return jsonify({'word_to_pdf': office_available, 'excel_to_pdf': office_available, 'powerpoint_to_pdf': office_available, 'pdf_to_word': True, 'images_to_pdf': True, 'pdf_to_images': True, 'merge_pdf': True})

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
        resp, status = _run_conversion(conversion, files, target_format=request.form.get('target_format'), quality=request.form.get('quality', 85), compress_level=request.form.get('compress_level', 6), resize_mode=request.form.get('resize_mode', 'none'), scale_percent=request.form.get('scale_percent', 50), width=request.form.get('width'), height=request.form.get('height'), keep_aspect=request.form.get('keep_aspect', 'true'))
        return resp, status
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
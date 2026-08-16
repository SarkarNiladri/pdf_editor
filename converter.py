import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
import io
from pathlib import Path

import pymupdf
from docx import Document
from docx.shared import Pt as DocPt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from flask import jsonify, send_file, after_this_request

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
UPLOAD_DIR = BASE_DIR / "converter_uploads"
OUTPUT_DIR = BASE_DIR / "converter_outputs"

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# --- Magic-byte signatures for file content validation ---
_MAGIC = {
    ".pdf": b"%PDF",
    ".png": b"\x89PNG\r\n\x1a\n",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".docx": b"PK",
    ".xlsx": b"PK",
    ".pptx": b"PK",
    ".odt": b"PK",
    ".ods": b"PK",
    ".odp": b"PK",
}
_LEGACY_OFFICE = {".doc", ".xls", ".ppt"}


def _validate_file(path: Path) -> None:
    if path.stat().st_size == 0:
        raise ValueError(f"Uploaded file is empty: {path.name}")
    suffix = path.suffix.lower()
    expected = _MAGIC.get(suffix)
    if expected is None:
        if suffix in _LEGACY_OFFICE:
            return
        return
    with open(path, "rb") as f:
        header = f.read(len(expected))
    if not header.startswith(expected):
        raise ValueError(
            f"File content does not match the expected format for '{suffix}'. "
            f"The file may be corrupted or renamed."
        )


def _cleanup_old_outputs(max_age_seconds: int = 3600) -> None:
    cutoff = time.time() - max_age_seconds
    for child in OUTPUT_DIR.iterdir():
        try:
            if child.is_file() and child.stat().st_mtime < cutoff:
                child.unlink(missing_ok=True)
            elif child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


def safe_name(name: str) -> str:
    name = Path(name or "file").name
    cleaned = "".join(c for c in name if c.isalnum() or c in " ._-").strip()
    return cleaned or "file"


def find_libreoffice():
    candidates = [
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    ]
    for item in candidates:
        if not item:
            continue
        path = Path(item)
        if path.is_file():
            return str(path)
    return None


def office_to_pdf(src: Path) -> Path:
    soffice = find_libreoffice()
    if not soffice:
        raise RuntimeError(
            "LibreOffice is not installed or could not be detected. "
            "Install LibreOffice to enable Word, Excel and PowerPoint conversion."
        )

    job_dir = Path(tempfile.mkdtemp(prefix="office_pdf_", dir=str(OUTPUT_DIR)))
    try:
        result = subprocess.run(
            [
                soffice,
                "--headless",
                "--convert-to", "pdf",
                "--outdir", str(job_dir),
                str(src),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout or "LibreOffice conversion failed").strip()
            )

        pdf = job_dir / f"{src.stem}.pdf"
        if not pdf.is_file():
            pdfs = list(job_dir.glob("*.pdf"))
            if not pdfs:
                raise RuntimeError("LibreOffice did not create a PDF.")
            pdf = pdfs[0]

        final = OUTPUT_DIR / f"{uuid.uuid4().hex}.pdf"
        shutil.copy2(pdf, final)
        return final
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def images_to_pdf(files):
    if not files:
        raise ValueError("No images provided to convert.")
    doc = pymupdf.open()
    try:
        for src in files:
            pix = pymupdf.Pixmap(str(src))
            page = doc.new_page(width=pix.width, height=pix.height)
            page.insert_image(page.rect, filename=str(src))
        output = OUTPUT_DIR / f"{uuid.uuid4().hex}.pdf"
        doc.save(str(output), garbage=4, deflate=True)
        return output
    finally:
        doc.close()


# ---------- PDF to Word conversion ----------
# Try to use the more accurate pdf2docx library if available.
try:
    from pdf2docx import Converter

    def pdf_to_word(src: Path) -> Path:
        """Convert PDF to Word using pdf2docx (better layout preservation)."""
        output = OUTPUT_DIR / f"{uuid.uuid4().hex}.docx"
        cv = Converter(str(src))
        cv.convert(str(output), start=0, end=None)
        cv.close()
        return output

except ImportError:
    # Fallback to the custom implementation (may be less accurate)
    import pymupdf
    from docx import Document
    from docx.shared import Pt as DocPt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    # ... (all the helper functions for the fallback: xml_safe_text, _is_symbol_font, etc.)
    # To avoid duplication, I am including the entire fallback code here.
    # In a real file, you would keep the existing implementation inside this block.

    def xml_safe_text(value: str) -> str:
        if not value:
            return ""
        return "".join(
            ch for ch in value
            if ord(ch) in (0x09, 0x0A, 0x0D)
            or 0x20 <= ord(ch) <= 0xD7FF
            or 0xE000 <= ord(ch) <= 0xFFFD
            or 0x10000 <= ord(ch) <= 0x10FFFF
        )

    _SYMBOL_FONTS = {"wingdings", "symbol", "zapfdingbats", "wingdings2", "wingdings3",
                   "mt extra", "webdings", "marlett"}

    def _is_symbol_font(font_name: str) -> bool:
        fn = (font_name or "").lower().replace(" ", "")
        return any(sym in fn for sym in _SYMBOL_FONTS)

    def _detect_alignment(page_width: float, spans: list, col_left: float, col_right: float) -> str:
        left_edge = page_width
        right_edge = 0.0
        for s in spans:
            if not s["text"].strip():
                continue
            left_edge = min(left_edge, s["bbox"][0])
            right_edge = max(right_edge, s["bbox"][2])
        if right_edge == 0:
            return "LEFT"
        col_width = col_right - col_left
        if col_width <= 0:
            return "LEFT"
        left_dist = (left_edge - col_left) / col_width
        right_dist = (col_right - right_edge) / col_width
        if left_dist > 0.12 and right_dist > 0.12:
            return "CENTER"
        if right_dist < 0.06 and left_dist > 0.20:
            return "RIGHT"
        return "LEFT"

    def _is_bold(font_name: str) -> bool:
        fn = (font_name or "").lower()
        return "bold" in fn or "black" in fn or "heavy" in fn

    def _is_italic(font_name: str) -> bool:
        fn = (font_name or "").lower()
        return "italic" in fn or "oblique" in fn

    def _clean_font_name(font_name: str) -> str:
        fn = (font_name or "").lower()
        if not fn or fn.startswith("zzzz"):
            return "Times New Roman"
        if "times" in fn or "serif" in fn:
            return "Times New Roman"
        if "arial" in fn or "helvetica" in fn or "sans" in fn:
            return "Arial"
        if "courier" in fn or "mono" in fn:
            return "Courier New"
        if "calibri" in fn:
            return "Calibri"
        if "cambria" in fn:
            return "Cambria"
        if "georgia" in fn:
            return "Georgia"
        if "garamond" in fn:
            return "Garamond"
        return "Times New Roman"

    def _extract_block_info(block, page_width: float) -> dict:
        lines = block.get("lines", [])
        if not lines:
            return None

        block_top = lines[0]["bbox"][1]
        block_bottom = lines[-1]["bbox"][3]
        block_left = min(l["bbox"][0] for l in lines)
        block_right = max(l["bbox"][2] for l in lines)

        spans = []
        for li, line in enumerate(lines):
            if li > 0 and spans:
                last_text = spans[-1]["text"]
                if last_text and not last_text.endswith('\n'):
                    spans.append({"text": "\n", "size": spans[-1]["size"], "font": spans[-1]["font"],
                                  "bold": spans[-1]["bold"], "italic": spans[-1]["italic"],
                                  "color": spans[-1]["color"], "bbox": spans[-1]["bbox"]})
            for span in line.get("spans", []):
                text = span.get("text", "")
                font = span.get("font", "")
                size = span.get("size", 0)

                if _is_symbol_font(font):
                    continue
                if size < 3.0:
                    continue
                if not text or not text.strip():
                    continue

                spans.append({
                    "text": text,
                    "size": round(size, 1),
                    "font": font,
                    "bold": _is_bold(font),
                    "italic": _is_italic(font),
                    "color": span.get("color", 0),
                    "bbox": span.get("bbox", (0, 0, 0, 0)),
                })

        if not spans:
            return None

        preview = "".join(s["text"] for s in spans).strip()[:60]
        return {
            "spans": spans,
            "top": block_top,
            "bottom": block_bottom,
            "left": block_left,
            "right": block_right,
            "mid_x": (block_left + block_right) / 2,
            "preview": preview,
        }

    def _split_large_blocks(block_infos: list, col_width: float) -> list:
        result = []
        for blk in block_infos:
            spans = blk["spans"]
            line_breaks = [i for i, s in enumerate(spans) if s["text"] == "\n"]
            num_lines = len(line_breaks) + 1

            if num_lines <= 4 or col_width <= 0:
                result.append(blk)
                continue

            line_spans = []
            current = []
            for s in spans:
                if s["text"] == "\n":
                    if current:
                        line_spans.append(current)
                    current = []
                else:
                    current.append(s)
            if current:
                line_spans.append(current)

            total_width = 0
            measured = 0
            for ls in line_spans:
                if ls:
                    lw = ls[-1]["bbox"][2] - ls[0]["bbox"][0]
                    total_width += lw
                    measured += 1
            avg_width = total_width / max(measured, 1)

            if avg_width < col_width * 0.55:
                for li, ls in enumerate(line_spans):
                    if not ls:
                        continue
                    first_bbox = ls[0]["bbox"]
                    last_bbox = ls[-1]["bbox"]
                    result.append({
                        "spans": ls,
                        "top": first_bbox[1],
                        "bottom": last_bbox[3],
                        "left": first_bbox[0],
                        "right": last_bbox[2],
                        "mid_x": (first_bbox[0] + last_bbox[2]) / 2,
                        "preview": "".join(s["text"] for s in ls).strip()[:60],
                    })
            else:
                result.append(blk)
        return result

    def _detect_columns(block_infos: list, page_width: float) -> list:
        if not block_infos:
            return [(0, page_width)]

        mids = sorted(set(round(b["mid_x"], 1) for b in block_infos))
        if len(mids) < 2:
            return [(0, page_width)]

        max_gap = 0
        gap_idx = -1
        for i in range(len(mids) - 1):
            gap = mids[i + 1] - mids[i]
            if gap > max_gap:
                max_gap = gap
                gap_idx = i

        if max_gap > page_width * 0.18 and gap_idx >= 0:
            split_mid = (mids[gap_idx] + mids[gap_idx + 1]) / 2
            col1 = [b for b in block_infos if b["mid_x"] < split_mid]
            col2 = [b for b in block_infos if b["mid_x"] >= split_mid]
            if col1 and col2:
                c1l = min(b["left"] for b in col1) - 5
                c1r = max(b["right"] for b in col1) + 5
                c2l = min(b["left"] for b in col2) - 5
                c2r = max(b["right"] for b in col2) + 5
                return [(c1l, c1r), (c2l, c2r)]

        return [(0, page_width)]

    def _assign_column(block: dict, columns: list) -> int:
        mid = block["mid_x"]
        best_col = 0
        best_overlap = -1
        for i, (cl, cr) in enumerate(columns):
            overlap = min(mid, cr) - max(mid, cl)
            if overlap > best_overlap:
                best_overlap = overlap
                best_col = i
        if best_overlap < 0:
            best_col = 0
            best_dist = float("inf")
            for i, (cl, cr) in enumerate(columns):
                col_mid = (cl + cr) / 2
                dist = abs(mid - col_mid)
                if dist < best_dist:
                    best_dist = dist
                    best_col = i
        return best_col

    def _group_into_paragraphs(column_blocks: list, col_left: float, col_right: float, page_width: float) -> list:
        if not column_blocks:
            return []

        sorted_blocks = sorted(column_blocks, key=lambda b: b["top"])
        paragraphs = []
        current = None

        for blk in sorted_blocks:
            if current is None:
                current = {
                    "spans": list(blk["spans"]),
                    "top": blk["top"],
                    "bottom": blk["bottom"],
                    "left": blk["left"],
                }
            else:
                gap = blk["top"] - current["bottom"]
                cur_sizes = [s["size"] for s in current["spans"] if s["text"].strip()]
                blk_sizes = [s["size"] for s in blk["spans"] if s["text"].strip()]
                cur_dom = max(set(cur_sizes), key=cur_sizes.count) if cur_sizes else 12
                blk_dom = max(set(blk_sizes), key=blk_sizes.count) if blk_sizes else 12
                same_size = abs(cur_dom - blk_dom) < 1.5

                cur_align = _detect_alignment(page_width, current["spans"], col_left, col_right)
                blk_align = _detect_alignment(page_width, blk["spans"], col_left, col_right)
                same_align = cur_align == blk_align

                cur_left = current["left"]
                blk_left = blk["left"]
                same_x = abs(cur_left - blk_left) < cur_dom * 1.5

                if gap < cur_dom * 0.35 and same_size and same_align and same_x:
                    if current["spans"] and blk["spans"]:
                        last_text = current["spans"][-1]["text"]
                        first_text = blk["spans"][0]["text"]
                        if last_text and not last_text.endswith((' ', '\n')) and first_text and not first_text.startswith((' ', '\n')):
                            current["spans"].append({**blk["spans"][0], "text": " "})
                    current["spans"].extend(blk["spans"])
                    current["bottom"] = blk["bottom"]
                else:
                    paragraphs.append(current)
                    current = {
                        "spans": list(blk["spans"]),
                        "top": blk["top"],
                        "bottom": blk["bottom"],
                        "left": blk["left"],
                    }

        if current is not None:
            paragraphs.append(current)

        return paragraphs

    def _add_paragraph_to_doc(doc_obj, para_info: dict, col_left: float, col_right: float, page_width: float):
        alignment_str = _detect_alignment(page_width, para_info["spans"], col_left, col_right)
        alignment_map = {
            "LEFT": WD_ALIGN_PARAGRAPH.LEFT,
            "CENTER": WD_ALIGN_PARAGRAPH.CENTER,
            "RIGHT": WD_ALIGN_PARAGRAPH.RIGHT,
            "JUSTIFY": WD_ALIGN_PARAGRAPH.JUSTIFY,
        }
        para = doc_obj.add_paragraph()
        para.alignment = alignment_map.get(alignment_str, WD_ALIGN_PARAGRAPH.LEFT)

        # Adjust line spacing to better match PDF
        para.paragraph_format.line_spacing = 1.5
        para.paragraph_format.space_before = DocPt(2)
        para.paragraph_format.space_after = DocPt(4)

        # Indentation
        first_text_span = None
        for s in para_info["spans"]:
            if s["text"].strip():
                first_text_span = s
                break
        if first_text_span:
            left_pos = first_text_span["bbox"][0]
            indent = left_pos - col_left
            if indent > 10:
                para.paragraph_format.left_indent = DocPt(min(indent, 360))

        # Add formatted runs
        for span in para_info["spans"]:
            text = span["text"]
            if not text:
                continue
            run = para.add_run(text)
            font_size = span["size"]
            if font_size > 0:
                run.font.size = DocPt(font_size)
            run.font.name = _clean_font_name(span["font"])
            run.font.bold = span["bold"]
            run.font.italic = span["italic"]
            color_int = span.get("color", 0)
            if color_int and color_int != 0:
                try:
                    r = (color_int >> 16) & 0xFF
                    g = (color_int >> 8) & 0xFF
                    b = color_int & 0xFF
                    run.font.color.rgb = RGBColor(r, g, b)
                except Exception:
                    pass

    def pdf_to_word(src: Path) -> Path:
        """Fallback PDF to Word conversion using custom layout extraction."""
        doc = Document()
        pdf = pymupdf.open(str(src))
        try:
            for page_index, page in enumerate(pdf):
                page_w = page.rect.width

                if page_index > 0:
                    doc.add_page_break()

                raw_blocks = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE).get("blocks", [])
                block_infos = []
                for b in raw_blocks:
                    if b.get("type", -1) != 0:
                        continue
                    info = _extract_block_info(b, page_w)
                    if info:
                        block_infos.append(info)

                if not block_infos:
                    continue

                columns = _detect_columns(block_infos, page_w)

                if len(columns) == 1:
                    split = _split_large_blocks(block_infos, page_w)
                    paragraphs = _group_into_paragraphs(split, 0, page_w, page_w)
                    for para_info in paragraphs:
                        _add_paragraph_to_doc(doc, para_info, 0, page_w, page_w)
                else:
                    for col_idx, (cl, cr) in enumerate(columns):
                        col_blocks = [b for b in block_infos if _assign_column(b, columns) == col_idx]
                        if not col_blocks:
                            continue
                        col_width = cr - cl
                        split = _split_large_blocks(col_blocks, col_width)
                        paragraphs = _group_into_paragraphs(split, cl, cr, page_w)
                        for para_info in paragraphs:
                            _add_paragraph_to_doc(doc, para_info, cl, cr, page_w)
        finally:
            pdf.close()

        output = OUTPUT_DIR / f"{uuid.uuid4().hex}.docx"
        doc.save(str(output))
        return output


def pdf_to_images(src: Path):
    output_dir = OUTPUT_DIR / uuid.uuid4().hex
    output_dir.mkdir(parents=True, exist_ok=True)

    pdf = pymupdf.open(str(src))
    try:
        images = []
        for index, page in enumerate(pdf):
            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
            target = output_dir / f"page_{index + 1}.png"
            pix.save(str(target))
            images.append(target)
        return images, output_dir
    finally:
        pdf.close()


def cleanup_images_dir(output_dir: Path) -> None:
    if output_dir and output_dir.is_dir():
        shutil.rmtree(output_dir, ignore_errors=True)


def merge_pdfs(files):
    result = pymupdf.open()
    try:
        for src in files:
            pdf = pymupdf.open(str(src))
            try:
                result.insert_pdf(pdf)
            finally:
                pdf.close()

        output = OUTPUT_DIR / f"{uuid.uuid4().hex}.pdf"
        result.save(str(output), garbage=4, deflate=True)
        return output
    finally:
        result.close()


# ----- Conversion router (shared logic) -----

def _run_conversion(conversion: str, files: list) -> tuple:
    """Shared conversion logic. Returns (response, status_code) or raises."""
    if conversion == "word-to-pdf":
        if len(files) != 1 or files[0].suffix.lower() not in {".doc", ".docx", ".odt"}:
            return (jsonify({"error": "Word to PDF requires one DOC, DOCX or ODT file."}), 400)
        output = office_to_pdf(files[0])
        return (send_file(output, as_attachment=True, download_name=f"{files[0].stem}.pdf"), 200)

    if conversion == "excel-to-pdf":
        if len(files) != 1 or files[0].suffix.lower() not in {".xls", ".xlsx", ".ods"}:
            return (jsonify({"error": "Excel to PDF requires one XLS, XLSX or ODS file."}), 400)
        output = office_to_pdf(files[0])
        return (send_file(output, as_attachment=True, download_name=f"{files[0].stem}.pdf"), 200)

    if conversion == "powerpoint-to-pdf":
        if len(files) != 1 or files[0].suffix.lower() not in {".ppt", ".pptx", ".odp"}:
            return (jsonify({"error": "PowerPoint to PDF requires one PPT, PPTX or ODP file."}), 400)
        output = office_to_pdf(files[0])
        return (send_file(output, as_attachment=True, download_name=f"{files[0].stem}.pdf"), 200)

    if conversion == "pdf-to-word":
        if len(files) != 1 or files[0].suffix.lower() != ".pdf":
            return (jsonify({"error": "PDF to Word requires exactly one PDF."}), 400)
        output = pdf_to_word(files[0])
        return (send_file(output, as_attachment=True, download_name=f"{files[0].stem}.docx"), 200)

    if conversion == "images-to-pdf":
        allowed = {".png", ".jpg", ".jpeg", ".webp"}
        if any(src.suffix.lower() not in allowed for src in files):
            return (jsonify({"error": "Images to PDF accepts PNG, JPG, JPEG and WebP."}), 400)
        output = images_to_pdf(files)
        return (send_file(output, as_attachment=True, download_name="converted.pdf"), 200)

    if conversion == "pdf-to-images":
        if len(files) != 1 or files[0].suffix.lower() != ".pdf":
            return (jsonify({"error": "PDF to Images requires exactly one PDF."}), 400)
        images, img_dir = pdf_to_images(files[0])
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for img in images:
                zf.write(img, arcname=img.name)
        zip_buffer.seek(0)
        @after_this_request
        def cleanup(response):
            cleanup_images_dir(img_dir)
            return response
        return (send_file(zip_buffer, as_attachment=True, download_name="images.zip", mimetype='application/zip'), 200)

    if conversion == "merge-pdf":
        if len(files) < 2 or any(src.suffix.lower() != ".pdf" for src in files):
            return (jsonify({"error": "Merge PDFs requires at least two PDF files."}), 400)
        output = merge_pdfs(files)
        return (send_file(output, as_attachment=True, download_name="merged.pdf"), 200)

    return (jsonify({"error": f"Unsupported conversion: {conversion}"}), 400)
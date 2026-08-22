import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
import io
from pathlib import Path

import pymupdf
from flask import jsonify, send_file, after_this_request
from PIL import Image

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
try:
    from pdf2docx import Converter

    def pdf_to_word(src: Path) -> Path:
        output = OUTPUT_DIR / f"{uuid.uuid4().hex}.docx"
        cv = Converter(str(src))
        cv.convert(str(output), start=0, end=None)
        cv.close()
        return output

except ImportError:
    def pdf_to_word(src: Path) -> Path:
        raise NotImplementedError("pdf2docx is not installed. Please install it or implement the fallback converter.")


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


# ---------- Image conversion with validation ----------
def convert_image(src: Path, target_format: str, quality: int = 85,
                  resize_mode: str = 'none', scale_percent: int = 50,
                  width: int = None, height: int = None, keep_aspect: bool = True,
                  compress_level: int = 6) -> Path:
    """Convert an image with optional resizing and quality/compression settings."""
    try:
        img = Image.open(src)
    except Exception as e:
        raise ValueError(f"Invalid image file: {e}")

    # Validate and clamp quality/compress_level
    try:
        quality = int(quality)
    except (TypeError, ValueError):
        quality = 85
    quality = max(1, min(100, quality))

    try:
        compress_level = int(compress_level)
    except (TypeError, ValueError):
        compress_level = 6
    compress_level = max(0, min(9, compress_level))

    # Resize
    if resize_mode == 'percentage':
        try:
            scale_percent = float(scale_percent)
        except (TypeError, ValueError):
            scale_percent = 50.0
        scale = scale_percent / 100.0
        new_width = int(img.width * scale)
        new_height = int(img.height * scale)
        if new_width < 1 or new_height < 1:
            raise ValueError("Scale percentage too small.")
        img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
    elif resize_mode == 'custom':
        if width is None or height is None:
            raise ValueError("Width and height must be provided for custom resize.")
        try:
            w = int(width)
            h = int(height)
        except (TypeError, ValueError):
            raise ValueError("Width and height must be integers.")
        if w < 1 or h < 1:
            raise ValueError("Width and height must be positive.")
        if keep_aspect:
            ratio = min(w / img.width, h / img.height)
            new_width = int(img.width * ratio)
            new_height = int(img.height * ratio)
            if new_width < 1 or new_height < 1:
                raise ValueError("Target dimensions too small.")
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        else:
            img = img.resize((w, h), Image.Resampling.LANCZOS)

    # Convert to RGB for JPEG
    if target_format.lower() in ('jpeg', 'jpg') and img.mode in ('RGBA', 'P', 'LA'):
        img = img.convert('RGB')

    # Save
    output = OUTPUT_DIR / f"{uuid.uuid4().hex}.{target_format.lower()}"
    save_format = target_format.upper()
    if save_format == 'JPG':
        save_format = 'JPEG'

    save_kwargs = {}
    if save_format in ('JPEG', 'WEBP'):
        save_kwargs['quality'] = quality
    elif save_format == 'GIF':
        save_kwargs['optimize'] = True
        # Do not set 'quality' for GIF – Pillow ignores it anyway
    elif save_format == 'PNG':
        save_kwargs['compress_level'] = compress_level

    img.save(output, format=save_format, **save_kwargs)
    return output


# ----- Conversion router -----
def _run_conversion(conversion: str, files: list, **kwargs) -> tuple:
    target_format = kwargs.get('target_format')
    quality = kwargs.get('quality', 85)
    compress_level = kwargs.get('compress_level', 6)
    resize_mode = kwargs.get('resize_mode', 'none')
    scale_percent = kwargs.get('scale_percent', 50)
    width = kwargs.get('width')
    height = kwargs.get('height')
    keep_aspect = kwargs.get('keep_aspect', 'true').lower() == 'true'

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

    if conversion == "image-to-image":
        if not target_format:
            return (jsonify({"error": "Target format not specified."}), 400)
        if len(files) != 1:
            return (jsonify({"error": "Please upload exactly one image."}), 400)

        try:
            output = convert_image(
                files[0], target_format, quality,
                resize_mode, scale_percent, width, height, keep_aspect,
                compress_level
            )
        except Exception as e:
            return (jsonify({"error": f"Image conversion failed: {str(e)}"}), 400)

        ext = target_format.lower()
        if ext == 'jpeg':
            ext = 'jpg'
        return (send_file(output, as_attachment=True, download_name=f"converted.{ext}"), 200)

    return (jsonify({"error": f"Unsupported conversion: {conversion}"}), 400)
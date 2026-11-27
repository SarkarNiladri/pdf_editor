import shutil
import subprocess
import tempfile
import threading
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


def send_and_delete(path, **kwargs):
    """send_file() for a generated output, removing the file once it was sent."""
    path = Path(path)

    @after_this_request
    def _cleanup(response):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return response
    return send_file(str(path), **kwargs)


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


# LibreOffice is heavy and a single profile directory can't be shared by parallel
# runs, so each job gets its own profile and only a couple run at the same time.
_OFFICE_SLOTS = threading.Semaphore(2)


def office_to_pdf(src: Path) -> Path:
    soffice = find_libreoffice()
    if not soffice:
        raise RuntimeError(
            "LibreOffice is not installed or could not be detected. "
            "Install LibreOffice to enable Word, Excel and PowerPoint conversion."
        )

    job_dir = Path(tempfile.mkdtemp(prefix="office_pdf_", dir=str(OUTPUT_DIR)))
    profile_dir = job_dir / "profile"
    out_dir = job_dir / "out"
    out_dir.mkdir()
    try:
        with _OFFICE_SLOTS:
            result = subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={profile_dir.as_uri()}",
                    "--headless", "--norestore", "--nolockcheck",
                    "--convert-to", "pdf",
                    "--outdir", str(out_dir),
                    str(src),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        pdfs = sorted(out_dir.glob("*.pdf"))
        if not pdfs:
            # Only a missing PDF is a failure; LibreOffice prints harmless warnings
            # (for example about Java) that must not be reported as errors.
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(detail or "LibreOffice did not create a PDF.")
        pdf = out_dir / f"{src.stem}.pdf"
        if not pdf.is_file():
            pdf = pdfs[0]
        final = OUTPUT_DIR / f"{uuid.uuid4().hex}.pdf"
        shutil.copy2(pdf, final)
        return final
    except subprocess.TimeoutExpired:
        raise RuntimeError("The conversion took too long and was stopped.")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def images_to_pdf(files):
    """One page per image. Pillow decodes the images so WebP/BMP/etc. work too."""
    if not files:
        raise ValueError("No images provided to convert.")
    doc = pymupdf.open()
    try:
        for src in files:
            try:
                with Image.open(src) as im:
                    from PIL import ImageOps
                    im = ImageOps.exif_transpose(im)
                    has_alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info
                    buf = io.BytesIO()
                    if has_alpha:
                        im.convert("RGBA").save(buf, format="PNG")
                    else:
                        im.convert("RGB").save(buf, format="JPEG", quality=95)
                    width, height = im.size
            except Exception as exc:
                raise ValueError(f"{Path(src).name} is not a readable image: {exc}")
            page = doc.new_page(width=width, height=height)
            page.insert_image(page.rect, stream=buf.getvalue())
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
    if (target_format or '').lower() not in {'jpeg', 'jpg', 'png', 'webp', 'bmp', 'gif', 'tiff'}:
        raise ValueError("Unsupported target format.")
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



def _parse_page_ranges(value: str, page_count: int) -> list[list[int]]:
    """Parse comma-separated page ranges (1-3,5) into zero-based page groups.

    The browser preview (converter.html, parsePageRanges) follows exactly the same
    rules so what you see is what you get.
    """
    value = (value or '').strip().lower()
    if not value:
        raise ValueError('Enter at least one page number or range, e.g. 1-3,5.')
    groups = []
    for part in value.split(','):
        part = part.strip()
        if not part:
            raise ValueError('Page ranges contain an empty item.')
        if '-' in part:
            bits = [x.strip() for x in part.split('-')]
            if len(bits) != 2 or not all(x.isdigit() for x in bits):
                raise ValueError(f'Invalid page range: {part}')
            start, end = int(bits[0]), int(bits[1])
            if end < start:
                raise ValueError(f'Page range {part} goes backwards. Write it as {end}-{start}.')
            if start < 1 or end > page_count:
                raise ValueError(f'Page range {part} is outside 1-{page_count}.')
            pages = list(range(start - 1, end))
        else:
            if not part.isdigit():
                raise ValueError(f'Invalid page number: {part}')
            page = int(part)
            if page < 1 or page > page_count:
                raise ValueError(f'Page {page} is outside 1-{page_count}.')
            pages = [page - 1]
        groups.append(pages)
    return groups


def _pdf_bytes(doc) -> io.BytesIO:
    out = io.BytesIO()
    doc.save(out, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True)
    out.seek(0)
    return out


def _run_pdf_organization(conversion: str, files: list, **kwargs):
    if len(files) != 1 or files[0].suffix.lower() != '.pdf':
        return jsonify({'error': 'Please upload exactly one PDF file.'}), 400
    try:
        doc = pymupdf.open(files[0])
        if doc.is_encrypted:
            doc.close()
            return jsonify({'error': 'This PDF is password-protected. Unlock it before using this tool.'}), 400
        if len(doc) == 0:
            doc.close()
            return jsonify({'error': 'The PDF has no pages.'}), 400

        if conversion == 'compress-pdf':
            original_size = files[0].stat().st_size
            # Downsample big embedded images (most of the size in scanned/photo PDFs).
            if hasattr(doc, 'rewrite_images'):
                try:
                    doc.rewrite_images(dpi_threshold=200, dpi_target=150, quality=75)
                except Exception:
                    pass
            output = _pdf_bytes(doc)
            doc.close()
            if output.getbuffer().nbytes >= original_size:
                # Never hand back a "compressed" file that is bigger than the original.
                output = io.BytesIO(files[0].read_bytes())
            return send_file(output, mimetype='application/pdf', as_attachment=True, download_name='compressed.pdf'), 200

        if conversion == 'split-pdf':
            ranges = _parse_page_ranges(kwargs.get('page_ranges', ''), len(doc))
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                for index, pages in enumerate(ranges, start=1):
                    part = pymupdf.open()
                    for page_no in pages:
                        part.insert_pdf(doc, from_page=page_no, to_page=page_no)
                    archive.writestr(f'split_part_{index}.pdf', part.tobytes(garbage=4, deflate=True))
                    part.close()
            doc.close()
            zip_buffer.seek(0)
            return send_file(zip_buffer, mimetype='application/zip', as_attachment=True, download_name='split_pdfs.zip'), 200

        if conversion == 'rotate-pdf':
            try:
                angle = int(kwargs.get('rotation', '90'))
            except (TypeError, ValueError):
                angle = 0
            if angle not in (90, 180, 270):
                doc.close()
                return jsonify({'error': 'Rotation must be 90, 180 or 270 degrees.'}), 400
            page_value = (kwargs.get('page_ranges') or 'all').strip().lower()
            # Each page is rotated once, even if it is listed twice (for example "1,1").
            selected = list(range(len(doc))) if page_value == 'all' else sorted({n for group in _parse_page_ranges(page_value, len(doc)) for n in group})
            for page_no in selected:
                page = doc[page_no]
                page.set_rotation((page.rotation + angle) % 360)
            output = _pdf_bytes(doc)
            doc.close()
            return send_file(output, mimetype='application/pdf', as_attachment=True, download_name='rotated.pdf'), 200

        if conversion == 'reorder-pdf':
            raw_order = (kwargs.get('page_order') or '').strip()
            try:
                order = [int(x.strip()) for x in raw_order.split(',') if x.strip()]
            except ValueError:
                doc.close()
                return jsonify({'error': 'Enter page numbers separated by commas, e.g. 3,1,2.'}), 400
            expected = list(range(1, len(doc) + 1))
            if len(order) != len(doc) or sorted(order) != expected:
                doc.close()
                return jsonify({'error': f'Enter every page exactly once, using numbers 1 to {len(expected)}.'}), 400
            reordered = pymupdf.open()
            for page_no in order:
                reordered.insert_pdf(doc, from_page=page_no - 1, to_page=page_no - 1)
            output = _pdf_bytes(reordered)
            reordered.close()
            doc.close()
            return send_file(output, mimetype='application/pdf', as_attachment=True, download_name='reordered.pdf'), 200

        doc.close()
        return jsonify({'error': 'Unsupported PDF organization operation.'}), 400
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': f'PDF processing failed: {str(exc)}'}), 400

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
        return (send_and_delete(output, as_attachment=True, download_name=f"{files[0].stem}.pdf"), 200)

    if conversion == "excel-to-pdf":
        if len(files) != 1 or files[0].suffix.lower() not in {".xls", ".xlsx", ".ods"}:
            return (jsonify({"error": "Excel to PDF requires one XLS, XLSX or ODS file."}), 400)
        output = office_to_pdf(files[0])
        return (send_and_delete(output, as_attachment=True, download_name=f"{files[0].stem}.pdf"), 200)

    if conversion == "powerpoint-to-pdf":
        if len(files) != 1 or files[0].suffix.lower() not in {".ppt", ".pptx", ".odp"}:
            return (jsonify({"error": "PowerPoint to PDF requires one PPT, PPTX or ODP file."}), 400)
        output = office_to_pdf(files[0])
        return (send_and_delete(output, as_attachment=True, download_name=f"{files[0].stem}.pdf"), 200)

    if conversion == "pdf-to-word":
        if len(files) != 1 or files[0].suffix.lower() != ".pdf":
            return (jsonify({"error": "PDF to Word requires exactly one PDF."}), 400)
        output = pdf_to_word(files[0])
        return (send_and_delete(output, as_attachment=True, download_name=f"{files[0].stem}.docx"), 200)

    if conversion == "images-to-pdf":
        allowed = {".png", ".jpg", ".jpeg", ".webp"}
        if any(src.suffix.lower() not in allowed for src in files):
            return (jsonify({"error": "Images to PDF accepts PNG, JPG, JPEG and WebP."}), 400)
        output = images_to_pdf(files)
        return (send_and_delete(output, as_attachment=True, download_name="converted.pdf"), 200)

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

    if conversion in {"compress-pdf", "split-pdf", "rotate-pdf", "reorder-pdf"}:
        return _run_pdf_organization(conversion, files, **kwargs)

    if conversion == "merge-pdf":
        if len(files) < 2 or any(src.suffix.lower() != ".pdf" for src in files):
            return (jsonify({"error": "Merge PDFs requires at least two PDF files."}), 400)
        output = merge_pdfs(files)
        return (send_and_delete(output, as_attachment=True, download_name="merged.pdf"), 200)

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
        return (send_and_delete(output, as_attachment=True, download_name=f"converted.{ext}"), 200)

    return (jsonify({"error": f"Unsupported conversion: {conversion}"}), 400)
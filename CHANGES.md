# Changes: review fixes and UI refresh (November 2026)

## Critical fixes
- **Rotated pages (Edit PDF):** text, shapes and images now land exactly where you place them on pages with a rotation (including pages turned by Rotate Pages). UI coordinates are mapped through `page.derotation_matrix` and text is drawn upright (`app.py` `insert_text`, `_draw_shape`, `apply_edits`).
- **Live preview:** changes made while a preview is running are queued, not dropped; undo/redo in quick succession now shows the right result (`index.html` `refreshPreview`).
- **No more frozen editor:** preview/render errors are shown as a toast, the last good preview stays, and the editor keeps working. All render/preview endpoints return JSON errors.
- **Edit Existing Text, repeated words:** the occurrence you selected is the one replaced (closest glyph run to the selected span), never the first match on the page.
- **Edit Existing Text, empty boxes:** the embedded font is only reused if it contains every new character. Otherwise the same font family installed on the server is used, then a built-in font, with per-character fallback fonts for anything else.

## High
- Rotate Pages: the browser preview now uses exactly the same range rules as the server, shows the error inline and disables the button; `1,1` rotates once; reversed ranges get a clear message; non-numeric angles give a clean 400. Rotated thumbnails are sized to fit.
- Deleting an item no longer breaks Add Text. Double-clicking a shape opens a new Edit Shape dialog, and double-clicking an image opens Replace Image.
- Selection handles stay on while resizing (`.overlay` selector fix). Selection boxes stay aligned when the page is wider than the window.
- Add Text supports ₹, curly quotes, Hindi, Bengali and other scripts via system fallback fonts (`textfonts.py`; Docker image now installs `fonts-noto-core`). Limitation: complex-script ligatures are drawn without full shaping.
- Word/Excel/PowerPoint to PDF: each LibreOffice run gets its own profile, at most 2 run at once, and harmless warnings are no longer reported as failures.
- Images to PDF accepts WebP (and anything Pillow reads), honours EXIF orientation and keeps transparency.

## Medium
- Background fill colour in Edit Existing Text is applied; coloured backgrounds work when you pick a matching fill.
- Edit Existing Text works on rotated pages.
- The old text is now **deleted** from the PDF (thin redaction through the glyphs, verified on a scratch copy so nothing else disappears). If that can't be done safely, it is covered as before and the UI says so. There is an option to turn deletion off.
- Page-number box works (type a number and press Enter). Download checks for errors. Reorder resets its order for a new file. Text has priority over shapes when you click.
- Friendlier 400 errors for bad uploads, encrypted PDFs, non-numeric inputs. Protect and Unlock both keep spaces in passwords exactly.

## Security / deployment
- Per-visitor rate limit on Summarize and Resume Tailor (`AI_RATE_LIMIT_PER_HOUR`, default 20).
- Converter output files are deleted as soon as they are sent.
- Upload limit is now 50 MB (matches the UI); thumbnails are capped at 150 pages; the background-removal model is loaded once and reused.
- AI keywords are inserted as text, not HTML.
- Dependencies pinned to tested versions; `import pymupdf` replaces the deprecated `fitz`; AI/ATS packages are imported lazily.
- `render.yaml` now builds the Docker image (the old apt-get build can't run on Render). The Docker base image is now Python 3.13 to match the tested versions.
- Download links are revoked after a delay so downloads work in Safari and Firefox.
- Compress PDF downsamples large images and never returns a bigger file.

## UI refresh
A shared `static/theme.css` gives all three pages one look: a frosted sticky header, a gradient hero with a tool count, a larger search, dark pill category tabs, cards with soft tinted icon tiles and a hover arrow, indigo-to-violet primary buttons, rounder dialogs, a dotted canvas behind the page, round selection handles, and toast notifications instead of browser alerts. Every tool, button and flow is unchanged.

## Tests
`tests/test_fixes.py` adds 21 regression tests (24 total with the existing ones). A GitHub Actions workflow runs them on every push and pull request.

## Follow-up: replacement text longer than its space (Edit Existing Text)
- **Automatic (default):** use the free space to the right first. If that's not enough, tighten letter and word spacing by up to 8%, then shrink the font to no less than 80% of the original, then wrap onto extra lines. Wrapping happens at full size where possible, and only into empty space below: no other text, no images, and not past the page bottom.
- **If nothing fits cleanly:** the text box turns red and you pick **Shrink** (one line, as small as needed), **Wrap** (more lines even if they reach content below) or **Keep as is** (original size, may run past the space). The same choices are in a dropdown.
- The status line says what was done (for example "wrapped onto 3 lines" or "shrunk to 86%").
- Rotated/vertical text runs support shrinking only.
- 9 new tests in `tests/test_overflow.py` (33 in total).

## Follow-up: simpler Edit Existing Text page

- One slim top bar holds the file name, page and zoom controls, search, open, undo/redo and Download.
- Click text directly on the page to select it; the side panel shows only the selected text, the replacement box and Apply.
- Moving, background colour, long-text handling and the delete-old-text switch live under "More options".
- The Changes list shows each new text with the old text faded underneath, with a one-click revert per change.
- The success message now describes only the change just applied (new `X-Text-Fit-By-Id` response header).

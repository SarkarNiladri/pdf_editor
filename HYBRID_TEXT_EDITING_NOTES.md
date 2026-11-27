# Hybrid existing-text editing update

## Scope
- Changes are confined to the separate existing-text replacement path in `app.py`.
- The stable additive editor (Add Text, Shapes, Images, preview/export routes) is not changed.

## Replacement strategy
1. Locate the selected span in the original PDF.
2. Resolve selected character geometry from `get_texttrace()` by matching the selected text as a substring of a larger trace run.
3. If trace mapping is unavailable, resolve the selected substring from `rawdict` character records.
4. Reject the edit if exact glyph geometry cannot be established. The old unsafe broad-span redaction fallback is not used for destructive removal.
5. For near-white/simple backgrounds, raster-sample the glyph region and apply a tight white overlay around the selected glyph union. This avoids using the broad span rectangle that overlapped the following resume line.
6. Reject non-white/complex backgrounds for now rather than silently painting over them. A future advanced content-stream reconstruction mode is needed for those documents.
7. Reuse an embedded font face only when family and bold/italic style match. Preserve leading/trailing whitespace from the selected span.
8. Use the existing available-width calculation to extend into free horizontal space before reducing font size.

## Known limitations
- Pixel-aware whiteout is restricted to near-white/simple backgrounds. It is not suitable for gradients, images, patterns or graphics behind the text.
- The original text object remains underneath the visual whiteout; this is a visual replacement, not secure content sanitization. Do not use it for redacting confidential information.
- Long replacements are constrained by the next detected same-baseline text span and page edge.
- Some subsetted/CID fonts may still be unavailable for insertion. In those cases the engine must not silently switch regular text to a bold face.

## Regression verification
- Python syntax compilation passed.
- Core regression against the supplied `Tailored_Resume.pdf`: replacement text extracted correctly; the following `Manual Testing` line remained present; replacement span remained non-bold.
- Synthetic adjacent-line regression passed.
- The original stable additive editor routes and files were left unchanged.

"""Font helpers: choose a font that can really draw every character.

PDF built-in fonts (Helvetica, Times, Courier) only cover Latin-1. For anything
else (Hindi, Bengali, the rupee sign, curly quotes...) we look for a system
font that has the glyph and draw that run of text with it instead.
"""
import glob
import os
from pathlib import Path

import pymupdf

_FONT_ROOTS = [
    '/usr/share/fonts', '/usr/local/share/fonts', str(Path.home() / '.fonts'),
    '/System/Library/Fonts', '/Library/Fonts', 'C:/Windows/Fonts',
]
_PREFERRED = ['notosans-regular', 'notosans', 'dejavusans.', 'dejavusans-', 'freesans',
              'liberationsans-regular', 'arial']
_files = None
_fonts = {}
_char_font = {}


def _list_fonts():
    global _files
    if _files is None:
        found = []
        for root in _FONT_ROOTS:
            if not os.path.isdir(root):
                continue
            for ext in ('ttf', 'otf'):
                found += glob.glob(os.path.join(root, '**', '*.' + ext), recursive=True)

        def score(path):
            name = Path(path).name.lower()
            s = 100
            for i, key in enumerate(_PREFERRED):
                if name.startswith(key):
                    s = i
                    break
            if any(w in name for w in ('bold', 'italic', 'oblique', 'mono', 'condensed',
                                       'light', 'black', 'thin', 'emoji', 'sample')):
                s += 200
            if 'unifont' in name:
                s += 500
            return (s, len(name), name)
        _files = sorted(set(found), key=score)
    return _files


def _load(path):
    if path not in _fonts:
        try:
            _fonts[path] = pymupdf.Font(fontfile=path)
        except Exception:
            _fonts[path] = None
    return _fonts[path]


def fallback_font_for_char(ch):
    """Path of a system font that has a glyph for ``ch`` (or None)."""
    if ch not in _char_font:
        found = None
        for path in _list_fonts():
            font = _load(path)
            if font is not None and font.has_glyph(ord(ch)):
                found = path
                break
        _char_font[ch] = found
    return _char_font[ch]


def builtin_covers(ch):
    """PDF base-14 fonts reliably encode printable Latin-1 only."""
    code = ord(ch)
    return ch == ' ' or (0x21 <= code <= 0x7E) or (0xA1 <= code <= 0xFF)


def split_runs(text, covers):
    """Split ``text`` into [(font_path_or_None, run)]; None means the base font."""
    runs = []
    for ch in text:
        path = None
        if not covers(ch):
            path = fallback_font_for_char(ch)
            if path is None:  # nothing can draw it (emoji...): show a placeholder
                ch = '?' if covers('?') else ' '
        if runs and runs[-1][0] == path:
            runs[-1][1] += ch
        else:
            runs.append([path, ch])
    return [(p, s) for p, s in runs]


def runs_length(runs, base_font, fontsize):
    total = 0.0
    for path, run in runs:
        font = base_font if path is None else _load(path)
        total += font.text_length(run, fontsize=fontsize)
    return total


def draw_runs(page, origin, direction, runs, fontsize, color, rotate,
              base_fontname, base_font, inserted=None, hscale=1.0):
    """Draw runs one after another along ``direction`` starting at ``origin``.

    ``hscale`` < 1 condenses the text horizontally (slightly tighter letters and
    spaces). It is only applied to unrotated text (rotate == 0).
    """
    if rotate:
        hscale = 1.0
    inserted = inserted if inserted is not None else set()
    point = pymupdf.Point(origin)
    for path, run in runs:
        if path is None:
            fontname, font = base_fontname, base_font
        else:
            fontname = 'fb' + str(abs(hash(path)) % 10**8)
            font = _load(path)
            key = (page.number, fontname)
            if key not in inserted:
                page.insert_font(fontname=fontname, fontfile=path)
                inserted.add(key)
        morph = (point, pymupdf.Matrix(hscale, 0, 0, 1, 0, 0)) if hscale != 1.0 else None
        page.insert_text(point, run, fontsize=fontsize, fontname=fontname,
                         color=color, rotate=rotate, overlay=True, morph=morph)
        advance = font.text_length(run, fontsize=fontsize) * hscale
        point = point + pymupdf.Point(direction[0] * advance, direction[1] * advance)


_STYLE_WORDS = ('regular', 'book', 'roman', 'normal', 'medium', 'bold', 'italic', 'oblique', 'mt', 'psmt')


def _family_key(name):
    import re
    name = re.sub(r'^[A-Z]{6}\+', '', str(name or ''))
    key = re.sub(r'[^a-z0-9]', '', name.lower())
    changed = True
    while changed:
        changed = False
        for word in _STYLE_WORDS:
            if key.endswith(word) and len(key) > len(word):
                key = key[:-len(word)]
                changed = True
    return key


def find_system_font(font_name, bold=False, italic=False):
    """Path of an installed font file with the same family (and style), or None."""
    wanted = _family_key(font_name)
    if not wanted:
        return None
    best = None
    for path in _list_fonts():
        stem = Path(path).stem.lower()
        if _family_key(stem) != wanted:
            continue
        penalty = int(('bold' in stem) != bold) + int((('italic' in stem) or ('oblique' in stem)) != italic)
        if best is None or penalty < best[0]:
            best = (penalty, path)
    if best and best[0] == 0:
        return best[1]
    return None

"""Live previews for Enhance Image and Remove Background."""
import io
import re
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import app as app_module

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    app_module.app.config['TESTING'] = True
    return app_module.app.test_client()


def _jpeg(size=(400, 300), colour=(120, 90, 60), exif_orientation=None):
    img = Image.new('RGB', size, colour)
    img.paste((240, 240, 240), (0, 0, size[0] // 2, size[1] // 4))
    buf = io.BytesIO()
    kwargs = {}
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        kwargs['exif'] = exif.tobytes()
    img.save(buf, format='JPEG', quality=95, **kwargs)
    return buf.getvalue()


def _post(client, url, data, **form):
    payload = {'file': (io.BytesIO(data), 'photo.jpg'), **form}
    return client.post(url, data=payload, content_type='multipart/form-data')


def _open(res):
    return Image.open(io.BytesIO(res.data))


def test_enhance_preview_is_inline_and_shows_the_adjustment(client):
    raw = _jpeg()
    plain = _post(client, '/api/enhance-image/preview', raw)
    brighter = _post(client, '/api/enhance-image/preview', raw, brightness='1.6')
    assert plain.status_code == brighter.status_code == 200
    assert brighter.mimetype == 'image/jpeg'
    assert 'attachment' not in brighter.headers.get('Content-Disposition', '')
    assert 'no-store' in brighter.headers['Cache-Control']
    assert np.asarray(_open(brighter)).mean() > np.asarray(_open(plain)).mean() + 20


def test_enhance_preview_matches_the_download(client):
    raw = _jpeg()
    form = {'brightness': '1.3', 'contrast': '1.4', 'color': '0.4', 'sharpness': '2.0'}
    preview = np.asarray(_open(_post(client, '/api/enhance-image/preview', raw, **form))).astype(int)
    full = np.asarray(_open(_post(client, '/api/enhance-image', raw, **form))).astype(int)
    assert preview.shape == full.shape
    assert np.abs(preview - full).mean() < 3   # only JPEG quality differs


def test_enhance_preview_is_downsized_for_speed(client):
    res = _post(client, '/api/enhance-image/preview', _jpeg(size=(3000, 1500)), contrast='1.2')
    assert res.status_code == 200
    assert max(_open(res).size) == app_module.ENHANCE_PREVIEW_MAX
    full = _post(client, '/api/enhance-image', _jpeg(size=(3000, 1500)), contrast='1.2')
    assert _open(full).size == (3000, 1500)
    assert 'enhanced.jpg' in full.headers['Content-Disposition']


def test_enhance_respects_phone_photo_rotation(client):
    # EXIF orientation 6 = shown rotated 90°: the preview and download must be upright.
    raw = _jpeg(size=(400, 200), exif_orientation=6)
    for url in ('/api/enhance-image/preview', '/api/enhance-image'):
        assert _open(_post(client, url, raw)).size == (200, 400)


@pytest.mark.parametrize('url', ['/api/enhance-image/preview', '/api/enhance-image'])
def test_enhance_rejects_bad_input(client, url):
    assert client.post(url, data={}, content_type='multipart/form-data').status_code == 400
    assert _post(client, url, b'not an image').status_code == 400
    assert _post(client, url, _jpeg(), brightness='lots').status_code == 400


def test_remove_bg_returns_upright_png(client, monkeypatch):
    seen = {}

    def fake_remove(img, session=None):
        seen['size'] = img.size
        out = img.copy()
        out.putalpha(128)
        return out

    monkeypatch.setitem(sys.modules, 'rembg', types.SimpleNamespace(remove=fake_remove, new_session=lambda name: object()))
    monkeypatch.setattr(app_module, '_REMBG_SESSION', None)
    res = _post(client, '/api/remove-bg', _jpeg(size=(400, 200), exif_orientation=6))
    assert res.status_code == 200 and res.mimetype == 'image/png'
    assert seen['size'] == (200, 400)
    result = _open(res)
    assert result.mode == 'RGBA' and result.size == (200, 400)


# ---------- the browser component ----------
def test_image_tools_script_is_served_loaded_and_cached(client):
    res = client.get('/static/image-tools.js')
    assert res.status_code == 200
    res.close()
    assert '<script src="/static/image-tools.js" defer></script>' in (ROOT / 'index.html').read_text()
    assert "'/static/image-tools.js'" in (ROOT / 'static/sw.js').read_text()
    assert 'static/image-tools.js' in app_module.PWA_SHELL_FILES


def test_every_element_the_component_needs_exists_in_index_html():
    """If the page is redesigned, these ids must survive or the previews stop working."""
    script = (ROOT / 'static/image-tools.js').read_text() + (ROOT / 'static/remove-bg.js').read_text()
    html = (ROOT / 'index.html').read_text()
    ids = set(re.findall(r"\$\('([A-Za-z]+)'\)", script))
    ids |= {f'{prefix}{name}' for prefix in ('slider', 'val')
            for name in ('Brightness', 'Contrast', 'Color', 'Sharpness')}
    assert ids, 'no ids found'
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert not missing, missing
    assert '/api/enhance-image/preview' in script and '/api/remove-bg' in script

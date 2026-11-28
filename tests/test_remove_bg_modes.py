"""Remove Background without AI: the "Draw box" mode (GrabCut) and the new script."""
import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

import app as app_module

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    app_module.app.config['TESTING'] = True
    return app_module.app.test_client()


def _photo(size=(600, 400)):
    """Blue noisy background with an orange circle in the middle."""
    w, h = size
    img = Image.new('RGB', size, (40, 120, 200))
    ImageDraw.Draw(img).ellipse((w // 2 - 120, h // 2 - 120, w // 2 + 120, h // 2 + 120), fill=(230, 90, 40))
    arr = np.asarray(img).astype(int) + np.random.default_rng(1).integers(-12, 12, (h, w, 3))
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def _circle(size=(600, 400)):
    w, h = size
    yy, xx = np.mgrid[:h, :w]
    return (xx - w // 2) ** 2 + (yy - h // 2) ** 2 <= 120 ** 2


def _png(img):
    buf = io.BytesIO(); img.save(buf, format='PNG'); return buf.getvalue()


def _post(client, image, hints=None, **form):
    data = {'file': (io.BytesIO(_png(image)), 'photo.png'), **{k: str(v) for k, v in form.items()}}
    if hints is not None:
        data['hints'] = (io.BytesIO(_png(hints)), 'hints.png')
    return client.post('/api/remove-bg/box', data=data, content_type='multipart/form-data')


def _iou(mask, truth):
    m = mask > 127
    return (m & truth).sum() / (m | truth).sum()


def test_box_cuts_out_the_subject(client):
    res = _post(client, _photo(), x=150, y=50, w=300, h=300)
    assert res.status_code == 200 and res.mimetype == 'image/png'
    assert 'no-store' in res.headers['Cache-Control']
    mask = Image.open(io.BytesIO(res.data))
    assert mask.mode == 'L' and mask.size == (600, 400)
    assert _iou(np.asarray(mask), _circle()) > 0.95


def test_big_photo_is_cut_on_a_small_copy_but_answered_full_size(client):
    size = (1800, 1200)
    img = _photo((600, 400)).resize(size, Image.Resampling.BILINEAR)
    res = _post(client, img, x=450, y=150, w=900, h=900)
    mask = np.asarray(Image.open(io.BytesIO(res.data)))
    assert mask.shape == (1200, 1800)
    truth = np.asarray(Image.fromarray(_circle().astype(np.uint8) * 255).resize(size)) > 127
    assert _iou(mask, truth) > 0.93
    edge = mask[(mask > 0) & (mask < 255)]
    assert edge.size > 0, 'the scaled-up edge should be soft, not jagged'


def test_brush_marks_win(client):
    hints = np.full((400, 600), 128, np.uint8)
    hints[180:220, 280:320] = 0      # erase a patch in the middle of the subject
    hints[10:30, 10:30] = 255        # keep a patch of background, outside the box
    res = _post(client, _photo(), hints=Image.fromarray(hints, 'L'), x=150, y=50, w=300, h=300)
    mask = np.asarray(Image.open(io.BytesIO(res.data)))
    assert (mask[180:220, 280:320] == 0).all()
    assert (mask[10:30, 10:30] == 255).all()
    assert mask[120:160, 280:320].mean() > 200   # the rest of the subject is still kept


def test_box_covering_the_whole_picture_still_works(client):
    res = _post(client, _photo(), x=0, y=0, w=600, h=400)
    assert res.status_code == 200
    mask = np.asarray(Image.open(io.BytesIO(res.data)))
    assert (mask[:, 0] == 0).all() and mask[200, 300] == 255


@pytest.mark.parametrize('form', [{}, {'x': 1, 'y': 1, 'w': 'wide', 'h': 5}, {'x': 1, 'y': 1, 'w': 2, 'h': 2},
                                  {'x': 'nan', 'y': 0, 'w': 10, 'h': 10}])
def test_bad_boxes_are_explained(client, form):
    res = _post(client, _photo(), **form)
    assert res.status_code == 400 and res.get_json()['error']


def test_not_an_image(client):
    res = client.post('/api/remove-bg/box', data={'file': (io.BytesIO(b'nope'), 'a.png'), 'x': 0, 'y': 0, 'w': 50, 'h': 50},
                      content_type='multipart/form-data')
    assert res.status_code == 400


def test_script_is_loaded_cached_and_keeps_all_three_modes(client):
    res = client.get('/static/remove-bg.js')
    assert res.status_code == 200
    res.close()
    html = (ROOT / 'index.html').read_text()
    assert html.index('/static/image-tools.js') < html.index('/static/remove-bg.js') < html.index('/static/tool-page.js')
    assert "'/static/remove-bg.js'" in (ROOT / 'static/sw.js').read_text()
    assert 'static/remove-bg.js' in app_module.PWA_SHELL_FILES
    script = (ROOT / 'static/remove-bg.js').read_text()
    for needle in ("'/api/remove-bg'", "'/api/remove-bg/box'", "'colour'", "'box'", "'ai'", "'keep'", "'erase'", 'pinch'):
        assert needle in script, needle

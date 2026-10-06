"""The REST views: /api/sextant/cords, /api/sextant/selftest, /api/sextant/upload_thing_icon."""
import asyncio
import io
import types

import sextant
from sextant import storage as st

from conftest import make_hass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _hass(tmp_path):
    hass = make_hass(tmp_path)
    hass.data["sextant"] = {}
    hass.states = types.SimpleNamespace(async_all=lambda domain=None: [])
    return hass


def test_cords_is_404_until_the_loop_has_published_and_then_the_payload(tmp_path):
    hass = _hass(tmp_path)
    view = sextant.SextantCordsAPI(hass)
    assert run(view.get(None)).status == 404
    hass.data["sextant"]["apitricords"] = {"phone": {"ent": "phone", "zone": "Kitchen", "cords": {"x": 1, "y": 2}}}
    response = run(view.get(None))
    assert response.status == 200 and response.json_body["phone"]["zone"] == "Kitchen"


def test_selftest_answers_even_with_nothing_to_solve(tmp_path):
    hass = _hass(tmp_path)
    run(st.save_layout(hass, {"floor": [{"name": "F", "scale": 100.0, "receivers": [], "zones": [], "subzones": []}], "tuning": {}}))
    response = run(sextant.SextantSelfTestAPI(hass).get(None))
    assert response.status == 200 and isinstance(response.json_body, dict)


class _Request(dict):
    """An aiohttp request stand-in: a mapping (hass_user lives there) with app and post."""

    def __init__(self, hass, form, admin=True):
        super().__init__(hass_user=types.SimpleNamespace(is_admin=admin))
        self.app = {"hass": hass}
        self._form = form

    async def post(self):
        return self._form


def _upload(hass, field, admin=True):
    request = _Request(hass, {"icon": field} if field is not None else {}, admin)
    return run(sextant.SextantUploadThingIconAPI().post(request))


def test_icon_upload_is_for_admins_and_raster_images_under_2mb(tmp_path):
    hass = _hass(tmp_path)
    ok = types.SimpleNamespace(filename="cat.png", file=io.BytesIO(b"PNGDATA"))
    assert _upload(hass, ok, admin=False).status == 403
    svg = types.SimpleNamespace(filename="cat.svg", file=io.BytesIO(b"<svg onload=alert(1)/>"))
    assert _upload(hass, svg).status == 400  # would run script on HA's own origin from /local/
    big = types.SimpleNamespace(filename="cat.png", file=io.BytesIO(b"x" * (sextant.MAX_ICON_UPLOAD_BYTES + 1)))
    assert _upload(hass, big).status == 413
    assert not (tmp_path / "www" / "sextant_icons" / "cat.svg").exists()


def test_map_image_view_serves_only_images_inside_the_maps_folder(tmp_path, monkeypatch):
    hass = _hass(tmp_path)
    maps = tmp_path / "sextant_maps"
    maps.mkdir()
    (maps / "Ground.png").write_bytes(b"PNG")
    (tmp_path / "secrets.yaml").write_text("token: x")
    view = sextant.SextantMapImageView()
    request = _Request(hass, {})
    served = []
    monkeypatch.setattr(sextant.web, "FileResponse", lambda path: served.append(path) or types.SimpleNamespace(status=200, headers={}))
    assert run(view.get(request, "Ground.png")).status == 200
    assert served == [str(maps / "Ground.png")]
    assert run(view.get(request, "missing.png")).status == 404
    assert run(view.get(request, "../secrets.yaml")).status == 404
    assert run(view.get(request, "secrets.yaml")).status == 404
    assert view.requires_auth is True


def test_save_text_is_for_admins(tmp_path):
    hass = _hass(tmp_path)
    request = _Request(hass, {"coordinates": "{}"}, admin=False)
    assert run(sextant.SextantSaveAPIText().post(request)).status == 403


def test_icon_upload_stores_a_sanitised_name_under_www(tmp_path):
    hass = _hass(tmp_path)
    field = types.SimpleNamespace(filename="../../dog face?.png", file=io.BytesIO(b"PNGDATA"))
    response = _upload(hass, field)
    assert response.status == 200
    assert response.json_body == {"icon_url": "/local/sextant_icons/dog_face_.png", "icon_name": "dog_face_.png"}
    assert (tmp_path / "www" / "sextant_icons" / "dog_face_.png").read_bytes() == b"PNGDATA"
    assert not (tmp_path / "dog face?.png").exists()


def test_icon_upload_without_a_file_is_a_400(tmp_path):
    assert _upload(_hass(tmp_path), None).status == 400


def test_the_unauthenticated_frontend_view_never_leaves_its_folder(tmp_path, monkeypatch):
    # No login is needed for it, so it cannot lean on Home Assistant's request
    # filter alone: a decoded "../" must be refused here too.
    hass = _hass(tmp_path)
    view = sextant.SextantFrontendView()
    request = _Request(hass, {})
    served = []
    monkeypatch.setattr(sextant.web, "FileResponse", lambda path: served.append(path) or types.SimpleNamespace(status=200, headers={}))
    assert run(view.get(request, "sextant-ui.js")).status == 200
    for name in ("../manifest.json", "../../../secrets.yaml", "sub/../sextant-ui.js", "/etc/passwd", ".."):
        assert run(view.get(request, name)).status == 404, name
    assert len(served) == 1


def test_svg_maps_are_served_sandboxed(tmp_path, monkeypatch):
    hass = _hass(tmp_path)
    maps = tmp_path / "sextant_maps"
    maps.mkdir()
    (maps / "Ground.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>")
    monkeypatch.setattr(sextant.web, "FileResponse", lambda path: types.SimpleNamespace(status=200, headers={}))
    response = run(sextant.SextantMapImageView().get(_Request(hass, {}), "Ground.svg"))
    assert response.headers["Content-Security-Policy"].startswith("sandbox")
    assert response.headers["X-Content-Type-Options"] == "nosniff"

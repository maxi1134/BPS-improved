"""Path-safety of the (unauthenticated) save_text file handling — audit #2.

_safe_maps_child is the single choke point that keeps client-supplied
filenames from escaping www/sextant_maps; these lock its behaviour down, plus the
_write_save write-after-validate ordering.
"""
import asyncio
import io

import sextant
from sextant import storage as st_mod
from conftest import make_hass


MAPS = "/config/www/sextant_maps"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Dict(dict):
    """Stand-in for aiohttp's multidict form data (only .get is used)."""


class _Upload:
    def __init__(self, filename, data=b"img"):
        self.filename = filename
        self.file = io.BytesIO(data)


def child(name, exts=sextant._ALLOWED_MAP_EXTS):
    return sextant._safe_maps_child(MAPS, name, exts)


def test_plain_filename_is_contained():
    got = child("first_floor.png")
    assert got is not None
    assert got.name == "first_floor.png"
    assert str(got).replace("\\", "/").endswith("www/sextant_maps/first_floor.png")


def test_parent_traversal_is_stripped_to_basename():
    # '../' components are removed by Path(...).name, never escaping the dir.
    got = child("../../secret.png")
    assert got is not None and got.name == "secret.png"
    base = sextant.Path(MAPS).resolve()
    assert got.parent == base


def test_absolute_path_is_neutralised():
    got = child("/etc/passwd.png")
    assert got is not None and got.name == "passwd.png"
    assert got.parent == sextant.Path(MAPS).resolve()


def test_windows_absolute_path_is_neutralised():
    # Backslash form must not survive as a directory component either.
    got = child(r"C:\windows\system32\evil.png")
    assert got is None or got.parent == sextant.Path(MAPS).resolve()


def test_disallowed_extension_rejected():
    assert child("payload.exe") is None
    assert child("script.html") is None
    assert child("bpsdata.txt") is None  # can't target the layout file


def test_allowed_image_extensions_accepted():
    for name in ("a.png", "b.jpg", "c.jpeg", "d.gif", "e.webp", "f.bmp", "g.svg"):
        assert child(name) is not None, name


def test_extension_check_is_case_insensitive():
    assert child("FLOOR.PNG") is not None


def test_empty_and_dot_names_rejected():
    assert child("") is None
    assert child(".") is None
    assert child("..") is None
    assert child(None) is None


def test_no_extension_filter_still_contains():
    # Without an extension allowlist the containment guarantee must still hold.
    got = sextant._safe_maps_child(MAPS, "../../../x", None)
    assert got is not None and got.parent == sextant.Path(MAPS).resolve()


# --- _write_save: map-image handling + layout goes to the store, not a file --- #
def _write(hass, maps, data, coords):
    return sextant.SextantSaveAPIText()._write_save(hass, str(maps), data, coords)


def _layout(hass):
    return hass._store_backing.get("sextant")


def test_bad_upload_leaves_layout_untouched(tmp_path):
    hass = make_hass(tmp_path)
    run(sextant.save_layout(hass, {"floor": [{"name": "F"}]}))  # existing saved layout
    data = _Dict(new_floor="true", file=_Upload("evil.exe"))
    err = run(_write(hass, tmp_path, data, {"floor": []}))
    assert err is not None and err.status == 400
    # A rejected upload must not have replaced the stored layout.
    assert _layout(hass) == {"floor": [{"name": "F"}]}


def test_valid_new_floor_writes_map_and_stores_layout(tmp_path):
    hass = make_hass(tmp_path)
    data = _Dict(new_floor="true", file=_Upload("ground.png", b"PNGDATA"))
    err = run(_write(hass, tmp_path, data, {"floor": [1]}))
    assert err is None
    assert _layout(hass) == {"floor": [1]}                    # layout -> store
    assert (tmp_path / "ground.png").read_bytes() == b"PNGDATA"  # image -> www/


def test_protected_files_not_deletable(tmp_path):
    hass = make_hass(tmp_path)
    (tmp_path / "sextant_calibration_state.json").write_text("{}")
    for name in ("bpsdata.txt", "../../bpsdata.txt", "sextant_calibration_state.json"):
        err = run(_write(hass, tmp_path, _Dict(remove=name), {}))
        assert err is not None and err.status == 400, name
    assert (tmp_path / "sextant_calibration_state.json").exists()


def test_existing_map_deletable_regardless_of_extension(tmp_path):
    # A map stored under any earlier-accepted extension must stay deletable —
    # the upload allowlist must not strand an existing floor (regression guard).
    hass = make_hass(tmp_path)
    for ext in (".jfif", ".tiff", ".png"):
        target = tmp_path / f"ground{ext}"
        target.write_bytes(b"img")
        err = run(_write(hass, tmp_path, _Dict(remove=f"ground{ext}"), {}))
        assert err is None, ext
        assert not target.exists(), ext


def test_jfif_upload_accepted(tmp_path):
    hass = make_hass(tmp_path)
    data = _Dict(new_floor="true", file=_Upload("attic.jfif", b"JPEG"))
    err = run(_write(hass, tmp_path, data, {"floor": [1]}))
    assert err is None
    assert (tmp_path / "attic.jfif").read_bytes() == b"JPEG"


def test_all_api_views_require_auth_static_stays_public():
    # Every /api/sextant/* data/mutation view must require auth; the static
    # /sextant/{file} view stays public so the custom panel can load (audit #1).
    import inspect
    api_views, static_views = [], []
    for obj in vars(sextant).values():
        if inspect.isclass(obj) and isinstance(getattr(obj, "url", None), str):
            if obj.url.startswith("/api/sextant/"):
                api_views.append(obj)
            elif obj.url.startswith("/sextant/"):
                static_views.append(obj)
    # save_text, upload_thing_icon, cords, selftest, map (read_text was retired: nothing used it).
    assert len(api_views) == 5, [v.__name__ for v in api_views]
    for v in api_views:
        assert getattr(v, "requires_auth", None) is True, f"{v.__name__} ({v.url})"
    assert static_views, "expected the static frontend view"
    for v in static_views:
        assert getattr(v, "requires_auth", None) is False, f"{v.__name__} must stay public"


def test_delete_traversal_still_blocked(tmp_path):
    # Containment must still reject an attempt to escape the maps dir.
    hass = make_hass(tmp_path)
    maps = tmp_path / "sextant_maps"
    maps.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"x")
    run(_write(hass, maps, _Dict(remove="../secret.png"), {}))
    assert outside.exists()  # '../' collapsed to basename; the real file survives


# --- thing ref-power trim (issue #92), now a websocket command ------------- #
from sextant import ws as ws_mod


class _Conn:
    def __init__(self):
        self.results, self.errors = [], []

    def send_result(self, msg_id, result=None):
        self.results.append(result)

    def send_error(self, msg_id, code, message):
        self.errors.append(message)


def _tune(hass, **fields):
    conn = _Conn()
    run(ws_mod.ws_thing_tune(hass, conn, {"id": 1, "type": "sextant/thing/tune", **fields}))
    return conn


def _layout(hass):
    return hass._store_backing.get("sextant")


def _seed(hass):
    run(st_mod.save_layout(hass, {"floor": [{"name": "F", "scale": 100.0, "receivers": [], "zones": []}], "tuning": {"zone_switch_secs": 30}}))


def test_tune_sets_and_persists_offset(tmp_path):
    hass = make_hass(tmp_path); _seed(hass)
    conn = _tune(hass, entity="phone", ref_offset_db=3.5)
    assert conn.results[-1]["ref_offset_db"] == 3.5
    assert _layout(hass)["thing_ref_offsets"] == {"phone": 3.5}


def test_tune_zero_or_null_clears_the_entry(tmp_path):
    hass = make_hass(tmp_path); _seed(hass)
    _tune(hass, entity="phone", ref_offset_db=3.5)
    _tune(hass, entity="phone", ref_offset_db=0)
    assert _layout(hass)["thing_ref_offsets"] == {}
    _tune(hass, entity="phone", ref_offset_db=-2)
    _tune(hass, entity="phone", ref_offset_db=None)
    assert _layout(hass)["thing_ref_offsets"] == {}


def test_tune_preserves_the_rest_of_the_layout(tmp_path):
    hass = make_hass(tmp_path); _seed(hass)
    _tune(hass, entity="phone", ref_offset_db=1.0)
    layout = _layout(hass)
    assert layout["floor"][0]["name"] == "F" and layout["tuning"] == {"zone_switch_secs": 30}


def test_tune_rejects_bad_input(tmp_path):
    hass = make_hass(tmp_path); _seed(hass)
    assert _tune(hass, entity="phone", ref_offset_db=99).errors
    assert _tune(hass, entity="phone", ref_offset_db=float("nan")).errors
    assert _layout(hass).get("thing_ref_offsets", {}) == {}


def test_tune_without_a_layout_is_rejected(tmp_path):
    hass = make_hass(tmp_path)
    run(st_mod.load_layout(hass))
    assert "No layout" in _tune(hass, entity="phone", ref_offset_db=1.0).errors[0]

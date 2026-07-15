"""Path-safety of the (unauthenticated) save_text file handling — audit #2.

_safe_maps_child is the single choke point that keeps client-supplied
filenames from escaping www/bps_maps; these lock its behaviour down, plus the
_write_save write-after-validate ordering.
"""
import asyncio
import io

import bps


MAPS = "/config/www/bps_maps"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Dict(dict):
    """Stand-in for aiohttp's multidict form data (only .get is used)."""


class _Upload:
    def __init__(self, filename, data=b"img"):
        self.filename = filename
        self.file = io.BytesIO(data)


def child(name, exts=bps._ALLOWED_MAP_EXTS):
    return bps._safe_maps_child(MAPS, name, exts)


def test_plain_filename_is_contained():
    got = child("first_floor.png")
    assert got is not None
    assert got.name == "first_floor.png"
    assert str(got).replace("\\", "/").endswith("www/bps_maps/first_floor.png")


def test_parent_traversal_is_stripped_to_basename():
    # '../' components are removed by Path(...).name, never escaping the dir.
    got = child("../../secret.png")
    assert got is not None and got.name == "secret.png"
    base = bps.Path(MAPS).resolve()
    assert got.parent == base


def test_absolute_path_is_neutralised():
    got = child("/etc/passwd.png")
    assert got is not None and got.name == "passwd.png"
    assert got.parent == bps.Path(MAPS).resolve()


def test_windows_absolute_path_is_neutralised():
    # Backslash form must not survive as a directory component either.
    got = child(r"C:\windows\system32\evil.png")
    assert got is None or got.parent == bps.Path(MAPS).resolve()


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
    got = bps._safe_maps_child(MAPS, "../../../x", None)
    assert got is not None and got.parent == bps.Path(MAPS).resolve()


# --- _write_save ordering: a rejected upload must not truncate bpsdata.txt --- #
def test_bad_upload_does_not_truncate_bpsdata(tmp_path):
    maps = tmp_path
    bpsdata = maps / "bpsdata.txt"
    original = '{"floor":[{"name":"F"}]}'
    bpsdata.write_text(original)
    view = bps.BPSSaveAPIText()
    data = _Dict(new_floor="true", file=_Upload("evil.exe"))
    err = run(view._write_save(bpsdata, str(maps), data, '{"floor":[]}'))
    assert err is not None and err.status == 400
    # The layout on disk is untouched — no partial-write corruption.
    assert bpsdata.read_text() == original


def test_valid_new_floor_writes_map_and_layout(tmp_path):
    maps = tmp_path
    bpsdata = maps / "bpsdata.txt"
    bpsdata.write_text("{}")
    view = bps.BPSSaveAPIText()
    data = _Dict(new_floor="true", file=_Upload("ground.png", b"PNGDATA"))
    err = run(view._write_save(bpsdata, str(maps), data, '{"floor":[1]}'))
    assert err is None
    assert bpsdata.read_text() == '{"floor":[1]}'
    assert (maps / "ground.png").read_bytes() == b"PNGDATA"


def test_protected_files_not_deletable(tmp_path):
    maps = tmp_path
    bpsdata = maps / "bpsdata.txt"
    bpsdata.write_text('{"floor":[]}')
    (maps / "bps_calibration_state.json").write_text("{}")
    view = bps.BPSSaveAPIText()
    for name in ("bpsdata.txt", "../../bpsdata.txt", "bps_calibration_state.json"):
        err = run(view._write_save(bpsdata, str(maps), _Dict(remove=name), "{}"))
        assert err is not None and err.status == 400, name
    # Neither protected file was touched.
    assert bpsdata.read_text() == '{"floor":[]}'
    assert (maps / "bps_calibration_state.json").exists()


def test_existing_map_deletable_regardless_of_extension(tmp_path):
    # A map stored under any earlier-accepted extension must stay deletable —
    # the upload allowlist must not strand an existing floor (regression guard).
    maps = tmp_path
    bpsdata = maps / "bpsdata.txt"
    bpsdata.write_text("{}")
    for ext in (".jfif", ".tiff", ".png"):
        target = maps / f"ground{ext}"
        target.write_bytes(b"img")
        err = run(view_delete(maps, bpsdata, f"ground{ext}"))
        assert err is None, ext
        assert not target.exists(), ext


def view_delete(maps, bpsdata, name):
    return bps.BPSSaveAPIText()._write_save(bpsdata, str(maps), _Dict(remove=name), "{}")


def test_jfif_upload_accepted(tmp_path):
    maps = tmp_path
    bpsdata = maps / "bpsdata.txt"
    bpsdata.write_text("{}")
    data = _Dict(new_floor="true", file=_Upload("attic.jfif", b"JPEG"))
    err = run(bps.BPSSaveAPIText()._write_save(bpsdata, str(maps), data, '{"floor":[1]}'))
    assert err is None
    assert (maps / "attic.jfif").read_bytes() == b"JPEG"


def test_delete_traversal_still_blocked(tmp_path):
    # Containment must still reject an attempt to escape the maps dir.
    maps = tmp_path / "bps_maps"
    maps.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"x")
    bpsdata = maps / "bpsdata.txt"
    bpsdata.write_text("{}")
    run(view_delete(maps, bpsdata, "../secret.png"))
    assert outside.exists()  # '../' collapsed to basename; the real file survives

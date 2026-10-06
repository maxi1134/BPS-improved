// The version of the frontend files on disk. Bumped with manifest.json on every
// release (tests/test_version.py fails when the two differ). The page takes
// its own version from here rather than from its URL: the panel URL carries
// the version Home Assistant STARTED with, while the files it serves are
// whatever is on disk now, so after an update a plain reload already runs the
// new frontend.
export const VERSION = "2026.10.05.1";

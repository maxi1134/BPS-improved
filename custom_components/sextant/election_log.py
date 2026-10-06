"""An opt-in record of every floor election, for replaying them later.

Each cycle publishes, per thing, the elected floor, the smoothed odds and each
contending floor's own fix, fit, proximity score and the proxies behind it
(``floor_cands``). The panel shows the current cycle and forgets it; a wrong
floor at five in the morning is gone by the time anyone looks. Until now the
only way to keep the cycles was an external subscriber left running overnight
(tools/floor_capture.py).

With ``election_log_hours`` set on the Tuning page, Sextant keeps them itself:
one JSON line per thing per cycle, in hourly files under
``config/sextant_election_log``, dropped once they are older than the window.
tools/replay_floors.py reads the directory and replays the elections under
other settings. Measured at about 6 MB an hour for twenty things at
fifteen-second cycles (1.2 KB a row, most of it the per-floor candidates); a
day is some 150 MB, a week a gigabyte, which is why it is off by default.

This module imports nothing from the rest of the package.
"""
import asyncio
import logging
import os
import re
import time

_LOGGER = logging.getLogger(__name__)

LOG_DIRNAME = "sextant_election_log"
# What is kept of a published row: everything the replay needs and nothing the
# panel alone wants (the trilateration circles, the fingerprint telemetry).
KEEP = (
    "ent", "floor", "floors", "floor_cands", "speed", "zone", "zone_raw", "zone_locked",
    "nearest_zone", "sub_zone", "anchor", "conf", "cords", "raw", "rms_m", "estimator", "updated",
)
_NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})Z\.jsonl$")


def file_for(stamp: float) -> str:
    """The hourly file a cycle at ``stamp`` belongs in, UTC."""
    return time.strftime("%Y-%m-%dT%HZ.jsonl", time.gmtime(stamp))


def hour_of(name: str):
    """The epoch of the hour a log file covers, or ``None`` if it is not one of ours."""
    m = _NAME.match(name)
    if not m:
        return None
    try:
        return _timegm(*(int(g) for g in m.groups()))
    except (ValueError, OverflowError):
        return None


def _timegm(year, month, day, hour):
    import calendar
    return calendar.timegm((year, month, day, hour, 0, 0, 0, 0, 0))


def expired(names, now: float, hours: float):
    """Which of the directory's files are older than the window.

    A file covers the hour it is named for; it expires once the END of that
    hour is more than ``hours`` ago, so a 24 h window keeps 24 whole hours
    plus the one in progress. Files that are not ours are never named.
    """
    cutoff = now - hours * 3600
    out = []
    for name in names:
        start = hour_of(name)
        if start is not None and start + 3600 < cutoff:
            out.append(name)
    return out


def slim(row: dict, stamp: float) -> dict:
    """One published row, cut down to what the replay reads."""
    rec = {"t": round(stamp, 3)}
    for key in KEEP:
        if key in row:
            rec[key] = row[key]
    rec["radii_n"] = len(row.get("radii") or [])
    return rec


class ElectionLog:
    """Buffers a cycle's rows and appends them to the hour's file in one write."""

    def __init__(self, dirpath: str):
        self.dirpath = dirpath
        self._buffer = []
        self._pruned_hour = None

    def add(self, row: dict, stamp: float | None = None):
        if isinstance(row, dict) and row.get("floor_cands"):
            self._buffer.append(slim(row, time.time() if stamp is None else stamp))

    async def flush(self, hours: float, now: float | None = None):
        """Write what the cycle recorded, and drop files past the window.

        ``hours`` <= 0 means the log is off: the buffer is discarded and the
        files already written are left alone (turning it back on continues
        them; the window prunes them in time).
        """
        rows, self._buffer = self._buffer, []
        if not rows or not hours or hours <= 0:
            return 0
        now = time.time() if now is None else now
        try:
            await asyncio.to_thread(self._write, rows, now, hours)
        except Exception as e:  # noqa: BLE001 - a log must never stop the cycle
            _LOGGER.warning("Election log not written: %s", e)
            return 0
        return len(rows)

    def _write(self, rows, now, hours):
        import json
        os.makedirs(self.dirpath, exist_ok=True)
        with open(os.path.join(self.dirpath, file_for(now)), "a", encoding="utf-8") as fh:
            for rec in rows:
                fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        hour = int(now // 3600)
        if self._pruned_hour == hour:
            return
        self._pruned_hour = hour
        for name in expired(os.listdir(self.dirpath), now, hours):
            try:
                os.remove(os.path.join(self.dirpath, name))
            except OSError as e:
                _LOGGER.debug("Election log %s not removed: %s", name, e)


async def clear(dirpath: str):
    """Delete every log file (ours only), returning (files, bytes) removed.

    Turning the log off stops the writing but keeps what was written - the
    window prunes only while it writes - so this is how the files go.
    """
    def _clear():
        files = size = 0
        try:
            names = os.listdir(dirpath)
        except FileNotFoundError:
            return 0, 0
        for name in names:
            if hour_of(name) is None:
                continue
            path = os.path.join(dirpath, name)
            try:
                size += os.path.getsize(path)
                os.remove(path)
                files += 1
            except OSError as e:
                _LOGGER.debug("Election log %s not removed: %s", name, e)
        return files, size
    return await asyncio.to_thread(_clear)


def get(hass) -> ElectionLog:
    """The one log for this hass, under config/sextant_election_log."""
    log = hass.data.get("sextant_election_log")
    if log is None:
        log = hass.data["sextant_election_log"] = ElectionLog(hass.config.path(LOG_DIRNAME))
    return log

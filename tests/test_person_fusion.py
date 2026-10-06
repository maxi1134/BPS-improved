"""A person's location fused from BLE and GPS (persons.py: judge_source,
choose_gps, presence_of_things, fuse, tracker_fix), and the command that sets
their GPS sources."""
import asyncio
import types
from datetime import datetime, timedelta, timezone

from sextant import persons
from sextant import ws
from sextant import storage as st
from conftest import make_hass

NOW_DT = datetime(2026, 9, 24, 21, 0, tzinfo=timezone.utc)
NOW = NOW_DT.timestamp()


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def state(value, attrs=None, ago=60.0):
    """An HA State stand-in: state, attributes, last_updated/last_reported."""
    when = NOW_DT - timedelta(seconds=ago)
    return types.SimpleNamespace(state=value, attributes=attrs or {}, last_updated=when, last_reported=when)


GPS = {"latitude": 41.30, "longitude": -81.70, "gps_accuracy": 15, "source_type": "gps"}
LAYOUT = {"person_trackers": {"person.eilee": ["device_tracker.eilees_iphone", "device_tracker.eilee_bauer"]}}


# --- one source -------------------------------------------------------------

def test_a_fresh_gps_tracker_is_usable_with_its_zone_and_coordinates():
    ok, reason, info = persons.judge_source("device_tracker.t", state("Lawrence Upper School", GPS), NOW)
    assert ok and reason is None
    assert info["zone"] == "Lawrence Upper School" and info["latitude"] == 41.30 and info["accuracy"] == 15


def test_a_missing_or_unavailable_tracker_is_broken():
    assert persons.judge_source("device_tracker.t", None, NOW)[:2] == (False, "not found")
    assert persons.judge_source("device_tracker.t", state("unavailable", GPS), NOW)[:2] == (False, "unavailable")
    assert persons.judge_source("device_tracker.t", state("unknown"), NOW)[:2] == (False, "unknown")


def test_a_tracker_that_stopped_reporting_is_stale():
    """Eilee's Companion app: frozen on home for six days after losing its
    location permission. Six days without a report is not a location."""
    ok, reason, _ = persons.judge_source("device_tracker.t", state("home", GPS, ago=6 * 86400), NOW, stale_secs=7200)
    assert not ok and reason.startswith("no report for 144.0 h")


def test_a_zone_only_tracker_still_gives_its_zone():
    ok, _, info = persons.judge_source("device_tracker.t", state("Kent State Dorm"), NOW)
    assert ok and info["zone"] == "Kent State Dorm" and info["latitude"] is None


# --- choosing among sources ---------------------------------------------------

def test_the_first_usable_source_wins_and_the_passed_over_are_named():
    states = {"device_tracker.eilees_iphone": state("home", GPS, ago=6 * 86400),
              "device_tracker.eilee_bauer": state("Lawrence Upper School", GPS)}
    chosen, ignored = persons.choose_gps(states.get, LAYOUT, "person.eilee", NOW)
    assert chosen["entity"] == "device_tracker.eilee_bauer"
    assert ignored == [{"entity": "device_tracker.eilees_iphone", "reason": "no report for 144.0 h"}]


def test_preference_order_is_honoured_when_both_are_usable():
    states = {"device_tracker.eilees_iphone": state("home", GPS), "device_tracker.eilee_bauer": state("not_home", GPS)}
    chosen, ignored = persons.choose_gps(states.get, LAYOUT, "person.eilee", NOW)
    assert chosen["entity"] == "device_tracker.eilees_iphone" and ignored == []


def test_no_sources_configured_means_nothing_chosen():
    assert persons.choose_gps(lambda e: None, {}, "person.eilee", NOW) == (None, [])


def test_a_person_is_as_present_as_their_most_present_thing():
    assert persons.presence_of_things(["away", "quiet", "here"]) == "here"
    assert persons.presence_of_things(["away", "quiet"]) == "quiet"
    assert persons.presence_of_things([]) == "away"


# --- the fused answer ---------------------------------------------------------

def _best():
    return {"ent": "watch", "cls": "watch", "updated": NOW - 5, "arrived": NOW - 600, "zone": "Eilee Room",
            "sub_zone": "unknown", "floor": "Second Floor", "area": ("eilee_room", "second_floor")}


def test_here_is_the_room_by_ble_with_gps_alongside():
    gps = {"entity": "device_tracker.eilee_bauer", "zone": "home", "latitude": 41.3001, "longitude": -81.7001, "accuracy": 10}
    out = persons.fuse(_best(), [], "here", None, gps, [], home=(41.30, -81.70))
    loc_state, loc = out["sextant_person_location"]
    assert loc_state == "Eilee Room" and loc["source"] == "ble" and loc["presence"] == "here"
    assert loc["zone"] == "home" and loc["tracker"] == "device_tracker.eilee_bauer" and 0 < loc["distance_m"] < 30
    assert out["sextant_person_room"][0] == "Eilee Room" and out["sextant_person_room"][1]["source"] == "ble"


def test_quiet_holds_the_last_place_they_were_put():
    out = persons.fuse(None, None, "quiet", _best(), None, [], home=None)
    loc_state, loc = out["sextant_person_location"]
    assert loc_state == "Eilee Room" and loc["source"] == "held" and loc["presence"] == "quiet"
    assert out["sextant_person_floor"][0] == "Second Floor"


def test_away_is_the_gps_zone_and_the_rooms_go_unknown():
    gps = {"entity": "device_tracker.eilee_bauer", "zone": "Lawrence Upper School", "latitude": 41.40, "longitude": -81.60, "accuracy": 20}
    out = persons.fuse(None, None, "away", None, gps, [{"entity": "device_tracker.eilees_iphone", "reason": "no report for 144.0 h"}], home=(41.30, -81.70))
    loc_state, loc = out["sextant_person_location"]
    assert loc_state == "Lawrence Upper School" and loc["source"] == "gps" and loc["kind"] == "zone"
    assert loc["distance_m"] > 10000 and loc["gps_ignored"][0]["entity"] == "device_tracker.eilees_iphone"
    assert out["sextant_person_room"] == ("unknown", {"area_id": None, "via": None, "source": "gps", "presence": "away"})


def test_away_with_gps_saying_not_home_reads_away():
    gps = {"entity": "device_tracker.t", "zone": "not_home", "latitude": 41.4, "longitude": -81.6, "accuracy": 5}
    assert persons.fuse(None, None, "away", None, gps, [])["sextant_person_location"][0] == "away"


def test_away_with_nothing_usable_says_so():
    loc_state, loc = persons.fuse(None, None, "away", None, None, [{"entity": "device_tracker.t", "reason": "unavailable"}])["sextant_person_location"]
    assert loc_state == "away" and loc["source"] == "none" and loc["latitude"] is None


def test_a_quiet_person_with_nothing_held_falls_through_to_gps():
    """Quiet only holds a place that was ever known; with none, GPS or away."""
    gps = {"entity": "device_tracker.t", "zone": "home", "latitude": 41.3, "longitude": -81.7, "accuracy": 5}
    assert persons.fuse(None, None, "quiet", None, gps, [])["sextant_person_location"][0] == "home"


# --- the device_tracker's word ------------------------------------------------

def test_the_tracker_is_home_on_ble_and_gps_once_lost():
    gps = {"entity": "device_tracker.t", "zone": "Lawrence Upper School", "latitude": 41.4, "longitude": -81.6, "accuracy": 20}
    assert persons.tracker_fix("here", gps)["location_name"] == "home"
    assert persons.tracker_fix("quiet", None)["source_type"] == "bluetooth_le"
    fix = persons.tracker_fix("away", gps)
    assert fix["location_name"] is None and fix["latitude"] == 41.4 and fix["source_type"] == "gps"
    assert persons.tracker_fix("away", {"entity": "device_tracker.t", "zone": "Kent State Dorm", "latitude": None})["location_name"] == "Kent State Dorm"
    assert persons.tracker_fix("away", None)["location_name"] == "not_home"


# --- setting the sources ----------------------------------------------------

class _Conn:
    def __init__(self):
        self.results, self.errors = [], []

    def send_result(self, msg_id, result=None):
        self.results.append((msg_id, result))

    def send_error(self, msg_id, code, message):
        self.errors.append((msg_id, code, message))


def test_the_command_writes_the_order_and_clears_an_empty_list(tmp_path):
    hass = make_hass(tmp_path)
    run(st.save_layout(hass, {"floor": [{"name": "F", "scale": 100.0, "receivers": [], "zones": [], "subzones": []}]}))
    conn = _Conn()
    run(ws.ws_person_trackers_set(hass, conn, {"id": 1, "type": "sextant/person/trackers/set", "person": "person.eilee",
                                              "trackers": ["device_tracker.b", "device_tracker.a", "device_tracker.b", "sensor.nope"]}))
    assert not conn.errors and conn.results[0][1]["trackers"] == ["device_tracker.b", "device_tracker.a"]
    assert st.get_layout(hass)["person_trackers"] == {"person.eilee": ["device_tracker.b", "device_tracker.a"]}
    run(ws.ws_person_trackers_set(hass, conn, {"id": 2, "type": "sextant/person/trackers/set", "person": "person.eilee", "trackers": []}))
    assert "person_trackers" not in st.get_layout(hass)
    run(ws.ws_person_trackers_set(hass, conn, {"id": 3, "type": "sextant/person/trackers/set", "person": "eilee", "trackers": []}))
    assert conn.errors

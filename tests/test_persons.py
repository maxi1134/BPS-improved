"""A person's location from the things they own (sextant.persons)."""
import sextant  # noqa: F401
from sextant import persons
from sextant import sensor as sensor_mod

NOW = 10_000.0


def thing(ent, cls, still_for=None, heard_ago=5.0, zone="Great Room", spot=None, floor="Ground Floor"):
    # still_for: how long since it arrived where it is (None: just now).
    return {"ent": ent, "cls": cls, "updated": NOW - heard_ago,
            "arrived": NOW - (still_for or 0),
            "zone": zone, "sub_zone": spot or "unknown", "floor": floor}


def test_owners_groups_things_by_person():
    layout = {"thing_owners": {"watch": "person.david", "phone": "person.david", "mphone": "person.michelle", "x": "david"}}
    assert persons.owners(layout) == {"person.david": ["watch", "phone"], "person.michelle": ["mphone"]}
    assert persons.owners({}) == {}


def test_the_thing_being_carried_speaks_for_its_owner():
    # Phone on the couch for an hour; the watch walked to the kitchen a minute ago.
    phone = thing("phone", "phone", still_for=3600, spot="Couch")
    watch = thing("watch", "watch", still_for=60, zone="Kitchen")
    assert persons.pick([phone, watch], NOW, 120)["ent"] == "watch"


def test_among_things_moved_recently_the_one_usually_worn_wins():
    phone = thing("phone", "phone", still_for=30)
    watch = thing("watch", "watch", still_for=200)
    pods = thing("pods", "headphones", still_for=10)
    assert persons.pick([phone, watch, pods], NOW, 120)["ent"] == "watch"


def test_unheard_or_unplaced_things_do_not_count():
    old = thing("watch", "watch", still_for=10, heard_ago=900)
    lost = thing("phone", "phone", still_for=10, zone="unknown")
    keys = thing("keys", "keys", still_for=4000)
    assert persons.pick([old, lost, keys], NOW, 120)["ent"] == "keys"
    assert persons.pick([old, lost], NOW, 120) is None


def test_states_give_the_spot_or_the_room_and_say_which_thing():
    s = persons.states(thing("phone", "phone", spot="Couch"))
    assert s["sextant_person_location"] == ("Couch", {"kind": "spot", "room": "Great Room", "spot": "Couch", "floor": "Ground Floor",
                                                       "area_id": None, "floor_id": None, "via": "phone", "considered": []})
    linked = persons.states({**thing("phone", "phone", spot="Couch"), "area": ("great_room", "ground")})
    assert linked["sextant_person_location"][1]["area_id"] == "great_room"
    assert linked["sextant_person_location"][1]["floor_id"] == "ground"
    assert linked["sextant_person_room"] == ("Great Room", {"area_id": "great_room", "via": "phone"})
    assert persons.considered([thing("phone", "phone", still_for=600, spot="Couch")], NOW) == [{"thing": "phone", "where": "Couch", "here_for_min": 10}]
    assert s["sextant_person_room"] == ("Great Room", {"area_id": None, "via": "phone"})
    assert persons.states(None)["sextant_person_location"][0] == "unknown"


def test_person_sensors_are_never_mistaken_for_an_untracked_things():
    for suffix, _label in persons.PERSON_SENSOR_KINDS:
        assert sensor_mod.thing_of_unique_id(f"{suffix}_david_coulson") is None


def test_headphones_keys_and_bags_do_not_locate_their_owner_unless_asked():
    layout = {"thing_locates_owner": {"pods2": True, "phone2": False}}
    assert persons.locates_owner(layout, "watch", "watch") and persons.locates_owner(layout, "meg", "cat")
    assert not persons.locates_owner(layout, "pods", "headphones")
    assert not persons.locates_owner(layout, "bag", "bag") and not persons.locates_owner(layout, "x", None)
    assert persons.locates_owner(layout, "pods2", "headphones")      # switched on for this pair
    assert not persons.locates_owner(layout, "phone2", "phone")      # switched off for this phone


def test_when_nothing_moves_the_latest_to_arrive_speaks_not_the_watch_on_its_charger():
    # The watch has sat on the nightstand since last night; the phone came
    # downstairs an hour ago. Neither is moving now.
    watch = thing("watch", "watch", still_for=9 * 3600, zone="Master Bedroom", spot="David Bedside Table")
    phone = thing("phone", "phone", still_for=3600, zone="Morning Room")
    assert persons.pick([watch, phone], NOW, 120)["ent"] == "phone"
    # A phone just carried somewhere keeps it ahead too.
    assert persons.pick([watch, thing("phone", "phone")], NOW, 120)["ent"] == "phone"


def test_settled_since_counts_metres_not_room_names_and_ignores_one_stray_fix():
    here = ("Up", 5.0, 5.0)
    pts = [(0, "Down", 1.0, 1.0), (200, "Up", 20.0, 5.0), (400, "Up", 20.0, 6.0),  # elsewhere for minutes
           (600, "Up", 5.5, 5.0), (700, "Up", 5.2, 4.8),                           # arrived
           (720, "Down", 9.0, 5.0), (760, "Down", 9.0, 5.0),                       # a minute a floor down: noise
           (800, "Up", 5.1, 5.1)]
    assert persons.settled_since(pts, here) == 600
    assert persons.settled_since([], here) is None
    assert persons.settled_since([(0, "Down", 5.0, 5.0)], here) is None



def test_arrival_survives_a_restart_with_a_late_history_and_a_wrong_floor_blip(monkeypatch):
    """What happened on 2026-09-19: after a restart the history had not loaded on
    the first cycle, the watch was placed a floor down for a minute, and every
    thing looked freshly arrived - so the watch on its charger won on class."""
    sextant._arrivals.clear()
    layout = {"floor": [{"name": "Up", "scale": 100.0}, {"name": "Down", "scale": 100.0}]}
    night = 1000.0                       # when the watch really came to rest
    answers = {"ready": False}

    def history(hass, ent, floor, x, y, now):
        return night if answers["ready"] and floor == "Up" else None

    monkeypatch.setattr(sextant, "_history_arrival", history)
    at_table = {"floor": "Up", "cords": [500.0, 500.0]}
    downstairs = {"floor": "Down", "cords": [900.0, 400.0]}
    t = 50_000.0
    # First cycle after the restart: history not loaded yet.
    assert sextant._arrived_at(None, layout, "watch", at_table, t) == t
    # It loads; the next cycle corrects the provisional answer.
    answers["ready"] = True
    assert sextant._arrived_at(None, layout, "watch", at_table, t + 15) == night
    # A wrong-floor blip for a minute: not a move.
    for dt in (30, 45, 60, 75):
        assert sextant._arrived_at(None, layout, "watch", downstairs, t + dt) == night
    assert sextant._arrived_at(None, layout, "watch", at_table, t + 90) == night
    # Really carried downstairs (over two minutes): a new arrival, from when it left.
    for dt in (100, 160, 230):
        got = sextant._arrived_at(None, layout, "watch", downstairs, t + dt)
    assert got == t + 100
    sextant._arrivals.clear()


def test_a_thing_on_its_charger_does_not_speak_for_its_owner():
    now = NOW
    watch = {**thing("watch", "watch", still_for=60, zone="Master Bedroom"), "on_charger": True}
    phone = thing("phone", "phone", still_for=5000, zone="Kitchen")
    # The watch just arrived and outranks the phone on class - but it is charging.
    assert persons.pick([watch, phone], now, 300)["ent"] == "phone"
    assert persons.pick([{**watch, "on_charger": False}, phone], now, 300)["ent"] == "watch"
    # Charging and nothing else: nobody is placed by it.
    assert persons.pick([watch], now, 300) is None
    # And the sensor says why.
    why = persons.considered([watch, phone], now)
    assert why[0]["on_charger"] is True and "on_charger" not in why[1]


def test_what_counts_as_on_the_charger():
    for s in ("Charging", "charging", "Charged", "Full", "on", " Charging "):
        assert persons.on_charger(s), s
    for s in ("Not Charging", "NotCharging", "not_charging", "Discharging", "off", "unavailable", "unknown", "", None, 42):
        assert not persons.on_charger(s), s

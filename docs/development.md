[Sextant](../README.md) › Data and development

# Data and development

## Versions

Releases are dated: `YYYY.MM.DD.N`, with `N` counting that day's releases
from 1 and starting again the next day - the same scheme as the proxies'
firmware and Strimmer. A version
says when something shipped, which is the only thing a running house needs
to know; the numbers before it (up to 3.17.76) were sequential.

## Where the data lives

| What | Where |
|---|---|
| Layout: floors, rooms, spots, proxies, heights, corrections, tuning, thing names and classes | `config/.storage/sextant` |
| Calibration solves and the rolling sample window | `config/.storage/sextant_calibration_state` |
| Stability baselines | `config/.storage/sextant_kpi_baselines` |
| Location pins with their samples | `config/.storage/sextant_truth` |
| Learned fingerprint gains, saved every five minutes so a restart starts warm | `config/.storage/sextant_fingerprint_gains` |
| Position history, one NDJSON segment per day, pruned to the retention | `config/.storage/sextant_history/` |
| Floor-plan images, served only to a signed-in user via `/api/sextant/map/<file>` | `config/sextant_maps/` |

Nothing under `.storage` is served over HTTP. Edit the layout from the
panel or the services, not the file: a hand edit under a running Home
Assistant is lost on the next save.

## The test bed

Every release runs in the author's house before it ships, which is where most
of the odd cases in this codebase came from. It is three floors: 21 rooms and
12 spots over a Ground Floor, a Second Floor and a Basement, with 58 placed
proxies. They are a mix of bare ESP boards and Shelly hardware, but the
distinction is only in the casing: the Shellys run ESPHome too, so every
proxy in the house speaks the same firmware.

What is tracked is mostly animals, and they are the hard case. A phone sits
on a table at chest height and stays there; a cat sleeps in a cardboard box
under a sideboard, moves three metres in a second, and spends the evening on
a landing that is open to the room below. The pets:

| Name | |
|---|---|
| Meg | he/him, short for Megatron |
| Socks | he/him |
| Fry | he/him |
| Leela | she/her |
| Lilibet | she/her |
| Willow | she/her |
| Primrose | she/her, the dog |

**Every one of them wears the same collar tag: a
[Holyiot beacon built on a Nordic nRF54L15](https://www.aliexpress.com/i/1005009152214994.html),
advertising as an iBeacon.** That matters for reading any
result from this house. One hardware model across seven animals means a
fixed advertising interval and transmit power, and a reference power that is
right for all of them at once - so a per-thing difference in the numbers is
the animal, the collar's position on its neck, or the room, and not the tag.
It also means the house cannot tell you how the code behaves with a tag that
advertises more slowly or weakly.

The single tracked Tile in the layout is there to exercise the
rotation-following code and is not on an animal, so a change that only works
for Tiles has not been tested on anything that moves like a cat.

The phones and watches come in as Private BLE Devices by their Identity
Resolving Key, and the Find My accessories (two AirTags, a wallet tag and
several AirPods) by their pairing keys. Between them that covers every
identity family Bermuda can follow, which is why the house catches problems
a synthetic fixture does not: a spot that never matches because the floor
above it wins, a key schedule that drifts months behind wall-clock, a room
lock that will not release.

## Development

```bash
pip install -r requirements_test.txt
pytest tests
```

- `tools/flap_kpi.py` computes the stability KPI from the recorder from a
  shell (`--hours 12 --json before.json`, later `--baseline before.json`).
- `tools/sextant_eval.py` replays a layout against recorded readings;
  `tools/solver_bench.py` benchmarks the numpy solver against SciPy.
- `tools/replay_floors.py` replays every thing's floor election from an
  election log (set `election_log_hours` on the Tuning page, then point it at
  `config/sextant_election_log`) under other margins, dwells and proximity
  weights, and grades a thing against where it really was;
  `tools/floor_capture.py` records the same cycles from outside, over the
  websocket, when the log is not on.
- `tools/brand/` builds the logo from the same compass rose the sidebar uses.
- The panel is plain Lit modules under `custom_components/sextant/frontend/`
  with no build step. The backend registers the panel module with the
  manifest version in its URL, so a page loaded before an update offers a
  reload on its own.

Issues and pull requests are welcome on
[davidcoulson/sextant](https://github.com/davidcoulson/sextant).

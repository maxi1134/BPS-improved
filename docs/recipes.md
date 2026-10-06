[Sextant](../README.md) › Recipes: automations and dashboards

# Recipes: automations and dashboards

Copy-paste starting points, each built on the entities in
[Sensors, card, services and API](automation.md). The entity ids are from
one real house; swap in your own.

## Which entity for what

| You want to know | Read | Why this one |
|---|---|---|
| where a **person** is, in words | `sensor.<person>_sextant_person_location` | the finest answer there is: a spot, a room, a GPS zone, or `away` |
| the **room** a person is in, to act on it | `sensor.<person>_sextant_person_room` | stable (hysteresis and the stationary lock), and carries `area_id` |
| whether a person is **home** | `device_tracker.<person>_sextant` | `home` on Sextant's word, the GPS zone once Sextant has lost them |
| where a **thing** is | `sensor.<thing>_sextant_location` (or `_room`, `_floor`, `_spot`) | same shape as the person sensors, one per thing |
| whether a thing is still **heard** | attribute `presence` on any of its sensors | `here` / `quiet` (2–15 min) / `away` |
| where a **robot vacuum** is | `sensor.vacuum_<name>_sextant_room` | placed from the robot's own map, not Bluetooth; `vacuum_state` rides along on `_location` |
| how long since it was heard | attribute `last_heard` | an ISO timestamp; see [seconds since](#seconds-since-a-thing-was-heard) |

Trigger on `_room` rather than `_location` when you act on a room: the
location changes every time a thing moves on or off a spot, the room does
not.

## Automations

### Lights follow a person from room to room

The room sensor carries the Home Assistant `area_id` of the room, so the
automation never needs a list of rooms and lights: it lights the area the
person walked into and darkens the one they left.

```yaml
alias: Lights follow David
triggers:
  - trigger: state
    entity_id: sensor.david_coulson_sextant_person_room
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.area_id is not none }}"
actions:
  - action: light.turn_on
    target:
      area_id: "{{ trigger.to_state.attributes.area_id }}"
  - if:
      - condition: template
        value_template: >
          {{ trigger.from_state.attributes.area_id is not none
             and trigger.from_state.attributes.area_id != trigger.to_state.attributes.area_id }}
    then:
      - action: light.turn_off
        target:
          area_id: "{{ trigger.from_state.attributes.area_id }}"
mode: queued
```

Rooms are linked to areas on the Edit page (a room's **Home Assistant
area**); an unlinked room has `area_id: none` and the condition above skips
it.

### Nobody left in a room

A helper that is on while anyone is in the kitchen, from every person's room
sensor at once. Use it the way you would a motion sensor, without the
"walked in and sat still" problem.

```yaml
template:
  - binary_sensor:
      - name: Kitchen occupied
        state: >
          {{ states.sensor
             | selectattr('entity_id', 'search', '_sextant_person_room$')
             | selectattr('state', 'eq', 'Kitchen')
             | list | count > 0 }}
        delay_off:
          minutes: 2
```

Then `Kitchen occupied` off for 10 minutes → lights off, media off, and so on.

### A cat at the litter boxes

The spot sensor reads the spot's name while the cat is on it. `for` keeps a
cat that walks past from counting.

```yaml
alias: Leela used the litter box
triggers:
  - trigger: state
    entity_id: sensor.leela_sextant_spot
    to: Litter Boxes
    for: "00:00:20"
actions:
  - action: counter.increment
    target:
      entity_id: counter.leela_litter_visits
```

A daily count that stops going up is the earliest sign of a cat that is
unwell; the same trigger with `notify` covers a cat that has not been in a
day.

### Arriving and leaving

The person's device_tracker is `home` while any of their things is heard in
the house, and the GPS zone once Sextant has lost them, so it changes once
per real arrival or departure rather than every time a phone's GPS wanders
across the property line.

```yaml
alias: Michelle is home
triggers:
  - trigger: state
    entity_id: device_tracker.michelle_bauer_sextant
    to: home
actions:
  - action: notify.mobile_app_davids_iphone
    data:
      message: Michelle is home
```

```yaml
alias: Everyone has left
triggers:
  - trigger: state
    entity_id:
      - device_tracker.david_coulson_sextant
      - device_tracker.michelle_bauer_sextant
      - device_tracker.eilee_bauer_sextant
    from: home
conditions:
  - condition: template
    value_template: >
      {{ ['device_tracker.david_coulson_sextant',
          'device_tracker.michelle_bauer_sextant',
          'device_tracker.eilee_bauer_sextant']
         | map('states') | select('eq', 'home') | list | count == 0 }}
actions:
  - action: script.house_away
```

### A phone left behind

The person is away by GPS, but the phone is still heard in the house.

```yaml
alias: David left his phone
triggers:
  - trigger: state
    entity_id: device_tracker.david_coulson_sextant
    from: home
    for: "00:05:00"
conditions:
  - condition: state
    entity_id: sensor.private_ble_device_david_phone_sextant_location
    attribute: presence
    state: here
actions:
  - action: notify.mobile_app_michelles_iphone
    data:
      message: "David's phone is still at home, in the {{ states('sensor.private_ble_device_david_phone_sextant_room') }}"
```

### A thing that has gone missing

`presence` goes `quiet` two minutes after a thing was last heard and `away`
after fifteen (both tunable). The wallet that was last seen in the Kitchen
at 5:10 PM:

```yaml
alias: Wallet not seen
triggers:
  - trigger: state
    entity_id: sensor.david_s_wallet_sextant_location
    attribute: presence
    to: away
actions:
  - action: notify.mobile_app_davids_iphone
    data:
      message: >
        Wallet not heard since
        {{ as_datetime(state_attr('sensor.david_s_wallet_sextant_location', 'last_heard')) | as_local | as_timestamp | timestamp_custom('%-I:%M %p') }},
        last in the {{ state_attr('sensor.david_s_wallet_sextant_location', 'room') }}
```

Watch a tag's battery the same way: a tag that goes `quiet` more and more
often before it goes `away` is a battery on its way out.

### A robot vacuum that has stopped moving

A robot placed on the plan (see [Robot vacuums](things.md#robot-vacuums)) has
the same room sensor as anything else, and its location sensor carries the
vacuum's own state. Stuck under the dining table for twenty minutes:

```yaml
alias: Rocky is stuck
triggers:
  - trigger: state
    entity_id: sensor.vacuum_rocky_sextant_room
    for: "00:20:00"
conditions:
  - condition: state
    entity_id: vacuum.rocky
    state: cleaning
actions:
  - action: notify.mobile_app_davids_iphone
    data:
      message: "Rocky has been in the {{ states('sensor.vacuum_rocky_sextant_room') }} for 20 minutes"
```

And one that waits for a room to be empty before cleaning it:

```yaml
alias: Clean the master bedroom once nobody is in it
triggers:
  - trigger: time
    at: "10:00:00"
conditions:
  - condition: template
    value_template: >
      {{ states.sensor | selectattr('entity_id', 'search', '_sextant_person_room$')
         | selectattr('state', 'eq', 'Master Bedroom') | list | count == 0 }}
actions:
  - action: vacuum.start
    target:
      entity_id: vacuum.katniss_everclean_s8
```

### Seconds since a thing was heard

`last_heard` is a timestamp, good to within a minute (it is written once a
minute while nothing else about the thing changes). A number for a dashboard
or a condition:

```yaml
template:
  - sensor:
      - name: Socks last heard
        unit_of_measurement: s
        state: >
          {% set t = state_attr('sensor.socks_sextant_location', 'last_heard') %}
          {{ ((now() - as_datetime(t)).total_seconds()) | round(0) if t else none }}
```

## Dashboards

### The floor plan

The map card draws exactly what the Live page draws, on the same live
subscription. One card per floor, or `follow: true` to switch floors with
the first entity listed.

```yaml
type: custom:sextant-map-card
floor: Ground Floor
title: Downstairs
height: 360
trails: true
labels: true
subzones: true
```

```yaml
type: custom:sextant-map-card
follow: true
entities:
  - socks
height: 300
```

### Who is where

An entities card, one line per person, with how long they have been there:

```yaml
type: entities
title: Who is where
entities:
  - entity: sensor.david_coulson_sextant_person_location
    name: David
    secondary_info: last-changed
  - entity: sensor.michelle_bauer_sextant_person_location
    name: Michelle
    secondary_info: last-changed
  - entity: sensor.eilee_bauer_sextant_person_location
    name: Eilee
    secondary_info: last-changed
  - entity: sensor.jack_bauer_sextant_person_location
    name: Jack
    secondary_info: last-changed
```

The same as a markdown card that writes itself from whatever people exist,
and says how each one was placed:

```yaml
type: markdown
title: Who is where
content: >
  {% for p in states.sensor | selectattr('entity_id', 'search', '_sextant_person_location$') | sort(attribute='name') %}
  {% set a = p.attributes %}
  **{{ p.name | replace(' Sextant Location', '') }}** — {{ p.state }}
  {%- if a.source == 'held' %} *(not heard for a bit)*{% endif %}
  {%- if a.source == 'gps' and a.distance_m %} *({{ (a.distance_m / 1000) | round(1) }} km away)*{% endif %}
  {%- if a.source == 'none' %} *(no location)*{% endif %}

  {% endfor %}
```

### A person as a tile

The tile card can show attributes next to the state:

```yaml
type: tile
entity: sensor.eilee_bauer_sextant_person_location
name: Eilee
state_content:
  - state
  - presence
  - floor
```

### The pets at a glance

```yaml
type: glance
title: Pets
columns: 4
entities:
  - entity: sensor.meg_sextant_room
    name: Meg
  - entity: sensor.socks_sextant_room
    name: Socks
  - entity: sensor.fry_sextant_room
    name: Fry
  - entity: sensor.leela_sextant_room
    name: Leela
  - entity: sensor.lilibet_sextant_room
    name: Lilibet
  - entity: sensor.willow_sextant_room
    name: Willow
  - entity: sensor.primrose_sextant_room
    name: Primrose
```

### Where they have been today

```yaml
type: history-graph
title: Rooms today
hours_to_show: 24
entities:
  - sensor.socks_sextant_room
  - sensor.leela_sextant_room
  - sensor.david_coulson_sextant_person_room
```

### Only when it matters

A card that appears while a cat is at the litter boxes, and is gone
otherwise:

```yaml
type: conditional
conditions:
  - condition: state
    entity: sensor.leela_sextant_spot
    state: Litter Boxes
card:
  type: markdown
  content: "🐈 Leela is at the litter boxes"
```

### Away people on the map

Home Assistant's own map card takes the Sextant device_trackers, so someone
away shows at their GPS position and someone home shows at home:

```yaml
type: map
entities:
  - device_tracker.jack_bauer_sextant
  - device_tracker.eilee_bauer_sextant
hours_to_show: 12
```

## Things to know

- **`presence` and the room can disagree on purpose.** A person whose things
  have gone `quiet` keeps their last room (`source: held`); the room sensor is
  the best answer for a person who has not left the house. Once they are
  `away`, the room sensors read `unknown` and the location sensor reads the
  GPS zone.
- **GPS sources are per person and in order** (the People card on the Things
  page). A source that is unknown, unavailable, or has not reported for
  `gps_stale_secs` (two hours by default, on the Tuning page) is passed over;
  the sensor's `gps_ignored` attribute and the card say which and why.
- **A restart re-stamps every restored entity as fresh**, so a tracker that
  is frozen looks alive for `gps_stale_secs` after each restart. Put the
  source you trust most first.
- **`last_heard` is not recorded** (it moves every minute while a thing is
  heard); `presence` is, so history and the logbook show a thing
  going quiet and coming back.

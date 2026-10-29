# Ondol Heating

A Home Assistant integration that runs radiant floor heating (온돌, district heating) through each
room's thermostat, planning around the slab's lag instead of reacting to it. Everything it reads
and writes is a Home Assistant entity, so it runs inside Home Assistant with no add-on.

An ondol room responds hours late: after a valve opens the air keeps drifting for 1–2 hours, then
rises slowly (about 0.1–0.2 °C/h); after it closes the slab still lifts the room for an hour and
keeps it warm for many more. A thermostat that only reacts runs all night towards a target the
slab can't reach and overshoots after it gets there. This integration:

- **Schedules** each room by week, with four presets: 편안 (comfort), 수면 (sleep), 절약 (eco: house
  occupied, room unused) and 외출 (away: house certainly empty).
- **Preheats**: starts a warmer block early by the learned dead time and rise rate, and only when
  the room would not be warm enough anyway.
- **Stops early**: before a cooler block it closes the valve as soon as the slab's stored heat will
  carry the room to the end of the block, within a tolerance.
- **Lets the slab coast**: during a long heat-up the thermostat aims 0.5 °C low, then holds the
  target once the coast has peaked.
- **Follows presence and daycare**: away on a certain-absence sensor; a child's room uses eco
  instead of comfort while a daycare sensor is on.
- **Respects people**: a change made at the thermostat (or in its app, or by an automation) holds
  until the next schedule block. A preheat for that block may raise it, never lower it.
- **Learns every room** from the thermostat's own history, every night at 04:30 and at start-up.

It starts in an advisory mode that only logs what it would do.

## How it learns

Each night it reads the last 7 days of recorder history (the thermostat's `hvac_action` and
`current_temperature`, plus the outdoor temperature) and turns every finished heating run into a
sample: dead time, rise rate, coast and idle drift. Samples are kept for 90 days; recent ones
weigh more (30-day half-life) so the model follows the season. The rise rate is fitted as a line
over the outdoor temperature, since district heating sends hotter water when it is colder outside.
Medians keep one run warmed by people at bedtime from dragging the numbers.

Nest thermostats only report a new temperature when their 0.5 °C display flips (25.2 ⇄ 25.3 means
the room sits at 25.25). The learner detects this and reads those reports as the flip they mark.

## Install

HACS: add this repository as a custom repository (category *Integration*), download **Ondol
Heating**, restart Home Assistant. Or copy `custom_components/ondol` into your config folder.

Then **Settings → Devices & services → Add integration → Ondol Heating**:

| Hub setting | |
|---|---|
| Nobody home (certain) | binary sensor, on only while the house is certainly empty |
| Child out (daycare) | binary sensor, on while the child is out (optional) |
| Outdoor temperature | temperature sensor |
| Comfort tolerance | how far below its target a room may be when a block starts or ends (0.5 °C) |
| Cost per room heating hour | ₩ per room-hour from a heating bill; 0 hides the cost sensors |

Add each room with **Add room**: its thermostat, the four preset temperatures (on the thermostat's
own scale, 18–30 °C), whether it follows the daycare sensor, and its weekly schedule:

```text
월-금 06:30 편안, 09:30 절약, 17:00 편안, 22:30 수면
토,일 08:00 편안, 22:30 수면
```

Days are 월…일 or mon…sun, ranges (`월-금`), lists (`토,일`) or `매일`/`daily`. Presets are
편안/수면/절약/외출 or comfort/sleep/eco/away. A block lasts until the next entry, across days.

## Entities

Hub: `switch.…_control` (난방 제어: off = advisory), `sensor.…_cost_today`, `sensor.…_cost_this_month`.

Per room (`<room> 온돌` device):

| Entity | |
|---|---|
| `climate.<room>_ondol` | the room as planned: target, preset, the Nest setpoint it sends, the plan in words. Setting a temperature or preset here goes to the thermostat at once and holds until the next block. |
| `sensor.<room>_ondol_next_action` | e.g. `20:58 예열 시작 → 22:30 수면 26.0°C` |
| `sensor.<room>_ondol_heating_today` | hours the valve was open today |
| `sensor.<room>_ondol_planned_heating` | 100 while the plan would have the valve open (judged on the real room), so advisory plans can be charted next to actual heating |
| dead time, rise rate, coast, idle drift | learned model, diagnostic |

Decisions go to the logbook, one line each: `제안: Nest 27.0°C → 26.0°C — 일정 수면 26.0°C` in
advisory mode, `Nest 25.5°C — 예열: 22:30 수면 26.0°C` once it controls, plus manual changes it
noticed and any failed commands.

## Taking control

Compare a few days of advisory lines and planned heating with what the thermostats actually did,
then turn on **난방 제어**. Disable any automation that also sets these thermostats first;
otherwise each of its changes becomes a hold until the next block.

## Safety

- Never writes a setpoint below 18 °C, so a room left alone by it (or by a failure) stays warm.
- At most 2 commands a minute and 12 an hour per thermostat (Nest allows 5 and 100, shared with
  every other app). A command the thermostat doesn't confirm within 10 minutes counts as failed.
  Failures back off 5, 10, 20… minutes up to an hour; after three the room shows `error`.
- A thermostat that is off, in eco or unavailable is left alone. Unavailable presence or daycare
  sensors read as "home" and "not at daycare".

## Cost

Billing by heat (Gcal) makes cost follow delivered heat, so the estimate is room heating hours
times a price per room-hour: take a bill's heating charge and divide it by the sum of all rooms'
heating hours over the same billing period, then enter it as *Cost per room heating hour*.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/python -m pytest
```

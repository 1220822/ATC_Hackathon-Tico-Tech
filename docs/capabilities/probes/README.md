# Write probes (approved tests, 2026-11-17)

Light was offline (no communication since ~2026-11-04) during all tests.

| # | Tool | Result |
|---|------|--------|
| 1 | `update_device_capabilities` Light `color_rgb` 255,140,0 | `tool_error` `SH_10500` (server 500). Light state unchanged. Likely because the Light is offline; repeat once it is powered. |
| 2 | `post_new_scenario` `TEST_janela_aberta_ambar` (inactive) | OK, id `defe45f0-ba95-483c-8d32-17c29ebaf049`. Device triggers are accepted and stored as sent: `{"type":"device","device":{"id":...},"capabilities":[{"name":"","type":"sensorState","operator":"=","value":"alarm"}]}`, `triggerLogic:"AND"`. Not yet verified that it fires when active. |
| 3 | `post_timer_device` Light, body `{}` and body `{name,timestamp,isActive,capabilities:[power=off]}` | Both `HTTP 404` from upstream, independent of body. Probably route/device not supported for timers (bulb offline or timers only for some device types). No timer created. |

## Light retest (online, 2026-11-17 ~17:00)

| # | Tool | Result |
|---|------|--------|
| 1b | `update_device_capabilities` Light `color_rgb` 255,140,0 (brightness was 40) | `device updated`. Reads back `102,56,0`: **reported colour = requested × brightness/100**. |
| 1c | `update_device_capabilities` Light `brightness` 100 | `device updated`. Colour now reads `255,140,0`. Brightness rescales the stored colour; `workMode` was already `colour`. |
| — | Restore | Set back to `0,255,0` + brightness 40 (reads `0,102,0`, as before the test). |

So test 1 failed only because the Light was offline. To show a status colour: send `color_rgb`
then `brightness` (100 for emergencies). Compare colours after normalising (`hue_rgb`).

Scenario payload that works: see `test2_scenario_create_response.json`.

## Timer retest with Light online

`post_timer_device` on the online Light with `{name,timestamp,isActive,capabilities:[power=off]}`
still returns `HTTP 404`; `get_device_timers` stays `[]`. Not caused by the Light being offline.
Until the body format is known, persistent plans use scenarios (`post_new_scenario`) instead.

## Colour + brightness race (2026-11-18, light online, colour mode)

Two writes sent back to back (each answered `device updated`) can undo each other: the second one
is applied with the cloud's previous value of the other.

| Sequence | Result (read back after 1–6 s) |
|---|---|
| `color_rgb` 255,0,0 then `brightness` 100, from green at 40 % | 2/2: **green** at 100 % (colour lost) |
| `brightness` 100 then `color_rgb` 255,0,0, from green at 40 % | 2/2: red at 100 % |
| same, a third time (through the Coordinator) | red at **40 %** (brightness lost), fixed by the read-back |

So the Coordinator sends brightness before colour **and** reads the light back ~2 s later,
re-sending what did not stick (up to 2 times). Seen live: intrusion alarm left the light at 100 %
in its old colour before this change.

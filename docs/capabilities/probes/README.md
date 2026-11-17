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

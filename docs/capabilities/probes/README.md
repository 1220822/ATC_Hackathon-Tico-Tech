# Write probes (approved tests, 2026-11-17)

Light was offline (no communication since ~2026-11-04) during all tests.

| # | Tool | Result |
|---|------|--------|
| 1 | `update_device_capabilities` Light `color_rgb` 255,140,0 | `tool_error` `SH_10500` (server 500). Light state unchanged. Likely because the Light is offline; repeat once it is powered. |
| 2 | `post_new_scenario` `TEST_janela_aberta_ambar` (inactive) | OK, id `defe45f0-ba95-483c-8d32-17c29ebaf049`. Device triggers are accepted and stored as sent: `{"type":"device","device":{"id":...},"capabilities":[{"name":"","type":"sensorState","operator":"=","value":"alarm"}]}`, `triggerLogic:"AND"`. Not yet verified that it fires when active. |
| 3 | `post_timer_device` Light, body `{}` and body `{name,timestamp,isActive,capabilities:[power=off]}` | Both `HTTP 404` from upstream, independent of body. Probably route/device not supported for timers (bulb offline or timers only for some device types). No timer created. |

Scenario payload that works: see `test2_scenario_create_response.json`.

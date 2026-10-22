# Id reservations

Ranges handed to parallel workers before they start, so two branches never allocate the same
id. A range is a promise not to use those ids elsewhere; unused ids in a range may be released
by striking the row. Allocate inside your range in order; the registries stay the source of
truth for what each id means.

| Reserved | For | Ranges |
|---|---|---|
| 2026-10-22 | Plan W2 (touch filter unit, StateDirectory) | SR-084; LLR-1142..1146; TC-1265..1274 |
| 2026-10-22 | Plan W3a (hub continuous monitoring, SN-048) | SR-085..092; LLR-1147..1179; TC-1275..1314; IF-065..066 |
| 2026-10-22 | Plan W5 (panel power button: press twice) | SN-049; SR-093..094; LLR-1180..1189; TC-1315..1329; IF-067 |
| 2026-10-22 | Plan W11 (usage gauges every minute) | SR-095..096; LLR-1190..1199; TC-1330..1344 |
| 2026-10-22 | Plan W12 (finance on Sure; tracker feed token) | SR-097..099; LLR-1200..1209; TC-1345..1359; IF-068..069 |
| 2026-10-22 | Plan W10 (gate health; used by lane W10-sA, the backlog shortlist) | SR-100..101; LLR-1210..1214; TC-1360..1369 |
| 2026-10-22 | Plan W4 (scratch volume, door relay, recorder) | SN-050; SR-102..107; LLR-1215..1239; TC-1370..1399; IF-070..071 |
| 2026-10-22 | Plan W6 (meeting recording, system half) | SR-108..111; LLR-1240..1254; TC-1400..1419 |

The plan is HomeHub `docs/IMPLEMENTATION_PLAN_2026-10-22.md`.

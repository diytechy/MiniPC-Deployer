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
| 2026-10-22 | Plan W10, lane W10-timer (the frame-sync timer; W10's first range was full) | SR-112; LLR-1255..1259; TC-1420..1429 (used: SR-112, LLR-1255..1257, TC-1420..1423) |
| 2026-10-22 | Plan W4 (scratch volume, door relay, recorder) | SN-050; SR-102..107; LLR-1215..1239; TC-1370..1399; IF-070..071 |
| 2026-10-22 | Plan W6 (meeting recording, system half) | SR-108..111; LLR-1240..1254; TC-1400..1419 (used: SR-108..111, LLR-1240..1252, TC-1400..1415) |
| 2026-10-22 | Plan W3b (watches: door recorder segments, scratch identity, meeting pull) | SR-119..120; LLR-1290..1299; TC-1460..1469 (used: LLR-1290..1292, TC-1460..1466; SR-088 amended, no new SR) |
| 2026-10-22 | Plan W8c (OtterTube on the hub: downloader, catalogue, site) | SN-051; SR-113..118; LLR-1260..1289; TC-1430..1459; IF-072..073 (used: SN-051, SR-113..117, LLR-1260..1283, TC-1430..1454, IF-072, IF-073) |
| 2026-10-23 | defs-guard growth fix | LLR-1310..1312; TC-1480..1484 (used: none - IF-036 amended instead; tests are D23..D28 in stack/tracker/tests/defs-guard.test.sh) |
| 2026-10-25 | heart long-term goal from the Sheet (glide path) | LLR-1320; TC-1493 (used: both; SR-073 amended) |
| 2026-10-25 | heart gauge from 5-minute samples (HEART_SOURCE=samples) | LLR-1314..1319; TC-1486..1492 (used: all; SR-073 amended) |
| 2026-10-25 | myAir usage minutes kept by the sleep feeder (not posted) | LLR-1313; TC-1485 (used: LLR-1313, TC-1485; SR-070 and LLR-1085 amended) |
| 2026-10-23 | nightly backup into the unified lane | SR-121; LLR-1300; TC-1470..1479 (used: SR-121, LLR-1300, TC-1470..1475; SR-018, IF-003, IF-006 amended) |
| 2026-10-26 | Hub metrics log: 5-min snapshot, 10:00/14:05 daily row, raw watch samples, chart page (HomeHub open-items A79) | SR-122..124; LLR-1321..1340; TC-1494..1520; IF-074 (used: SR-122..124, LLR-1321..1332, TC-1494..1510, IF-074; LLR-1330 also refs SR-073) |
| 2026-10-26 | OtterTube update channel on the hub: updates folder, Caddy routes, site/library knob split (HomeHub open-items A77) | SR-125..127; LLR-1341..1355; TC-1521..1540; IF-075 |
| 2026-10-27 | FileBackup bounded-run-loop contract (branch `filebackup-bounded-contract`; renumbered 2026-10-27 to the next range free after the 2026-10-26 reservations - it was first numbered SR-122, LLR-1320.., TC-1490.., which the heart and hub-metrics work also reserved) | SR-128; LLR-1356..1360; TC-1541..1550 (used: SR-128, LLR-1356..1360, TC-1541..1545; SR-018, SR-049, LLR-1004, TC-1065 amended) |

The plan is HomeHub `docs/archive/2026-10-23/IMPLEMENTATION_PLAN_2026-10-22.md`.

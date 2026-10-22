# door — relay and recorder for the door camera (SN-050)

Implements **SR-105** (the relay), **SR-106** (measurement and the cutover
tooling) and **SR-107** (the recorder). Interface to the panel: **IF-070**.

The RemoBell S accepts **one** video connection, and the panel's door broker has
held it. The relay — go2rtc, pinned in [`door-relay-image.pin`](door-relay-image.pin)
— takes that one connection and re-serves it on the hub's LAN address, so the
panel's door view and the recorder watch at once while the camera still sees a
single client. Recording goes to the NVMe scratch volume
([`../scratch/README.md`](../scratch/README.md)).

| Piece | Where | Switch |
|---|---|---|
| relay (go2rtc 1.9.14, by digest) | compose `door-relay`, run by `homehub-door-relay.service` | `DOOR_RELAY_ENABLED` |
| recorder | `door_recorder.py run`, `homehub-door-recorder.service` | `DOOR_RECORDER_ENABLED` |
| measurement, credentials | `door_relay.py` (probe, gate, streams, upstream-count, cost, import-upstream) | — |
| the panel half of the cutover | `door-cutover-panel.sh` (copied to the panel's /tmp, run as root) | — |

**Listeners, all named**: RTSP on `LAN_IP:8554` with the doorbell's **own**
username and password (no new secret); the API on `127.0.0.1:1984` behind the
same credential with `local_auth` (go2rtc otherwise lets loopback in without it,
and the API reports the camera URL); WebRTC, SRTP and RTMP disabled. Host
networking, so those binds are the host's. **No Caddy route, no DNS name.**
Video only (`#media=video`, `default_query: video`).

**The relay's output never carries the credential.** go2rtc's producer warns
with the whole camera URL - credential and camera address included - every time
the camera drops, and `info` is kept because those warnings are the record of a
dropout. So the unit runs the attached `compose up` under `door_relay.py
redact-run`, which merges stdout and stderr and removes, line by line, every
spelling of the password (raw, percent-encoded, the API's Basic token) and the
camera's host:port before anything reaches the journal; and the compose service
has `logging: driver: none`, so docker keeps no second copy (`docker logs
homehub-door-relay` answers "configured logging driver does not support
reading" - that is the design, not a fault; read the journal).

**No consumer is handed the credential in argv or in its environment.** The
relay's consumer URL carries the doorbell's username and password, and an
argument is readable from `/proc/<pid>/cmdline` by any local user (a
container's processes are host processes), an environment variable from
`docker inspect` and `/proc/<pid>/environ`. So every consumer the hub runs -
the recorder, `probe`, the gate's consumers, the `cost` decoder - is built by
one helper, `relay_consumer()`, as the panel's broker does it: ffmpeg/ffprobe
is the container's entrypoint (no shell), reads `-f concat -i pipe:0`, and the
URL arrives in a one-entry ffconcat document written to its stdin (`docker run
-i`) and then closed. The rtsp options ride in the document (`option
rtsp_transport tcp`, the recorder's `option timeout`). Lab check 11 samples every
running consumer and finds the credential in no process's cmdline or environ and
in no `docker inspect` field.

**Credentials on the hub** are `DOOR_RELAY_UPSTREAM_HOST/PORT/PATH/USERNAME/
PASSWORD` — deliberately not the panel's `DOORBELL_RTSP_*` names, because after
the cutover the panel's `DOORBELL_RTSP_HOST` is the relay. The password is the
same deploy-store key the panel uses (HomeHub `FieldSchema.psd1`), so a hub
reimage materialises it; on the live hub it is carried from the panel by a pipe.

**The recorder** reads the relay once (ffmpeg from the same pinned image,
`-c copy -an`, MPEG-TS on stdout) and cuts files itself at keyframes, so the
scratch guard runs before **every** new segment:
`/srv/scratch/door/YYYY-MM-DD/door-YYYYmmddTHHMMSSZ.ts` (UTC), ten minutes each,
whole oldest segments pruned under `DOOR_RECORDER_CAP_MIB` (7.5 GiB, inside the
8 GiB quota). Exit 75 = door/ full, 76 = guard refused: both alert and are not
retried. A relay restart or a stall ends the run and systemd restarts it 15 s
later. A segment ended by anything but a rotation is cut back to its last whole
frame, so every file decodes.

## Tests

* `tests/test_door_relay.py` (pytest): config, validation, import, pin, units,
  redaction, the TS cutter, pruning, guard and quota stops, continuity over a
  window, the gate's verdicts (TC-1377..TC-1399).
* `tests/door-relay-lab.sh` (root + docker; TC-1385..TC-1392): the real relay
  against a fake single-session camera — listeners, auth, geometry, the gate,
  the panel broker's own input path, decodable video-only segments across a
  relay restart, no secret in any log, the cost tool, and (check 10) the relay
  run from its unit file's own Exec lines with the camera killed under a
  consumer: the dropout warning arrives redacted and docker holds nothing;
  (checks 4b, 4c) the gate fails when its survivor is SIGSTOPped before or just
  after its first sample; (check 11) no
  running consumer holds the credential in its cmdline, environ or docker
  inspect.
* `tests/door-cutover-panel.test.sh` (hermetic; TC-1398): the panel script's
  file verbs on a fake tree, and the probe and compat verdicts (their own Python
  bodies, extracted from the script) against a fake broker, `ss` and decoder.

---

## The coordinator's procedures

Placeholders: `<hub>` the hub's LAN address, `<panel>`, `<cam>` the doorbell's
address (all in HomeHub's identity table), `$W` a copy of this repo's `stack/`
on each machine (`scp -r` to `/tmp/w4`). Every command that needs a value from a
secret reads it itself; **nothing below prints one**.

### 1. Create the scratch volume (W4a)

```sh
# capacity first, in exact units: expect 5949 free extents of 4194304 B
sudo vgs --units b -o vg_free,vg_extent_size,vg_free_count ubuntu-vg
sudo install -d /opt/homehub/stack/scratch && sudo install -m 0644 /tmp/w4/scratch/homehub_scratch.py /opt/homehub/stack/scratch/
sudo python3 /opt/homehub/stack/scratch/homehub_scratch.py plan
#   exit 0, "would create LV ubuntu-vg/hub-scratch, 4608 extents (18432 MiB), leaving 1341 extents (5.24 GiB) free"
#   exit 3 = STOP: record the numbers in FOLLOWUPS and do not continue. Exit 4 = something is there already: read it.
sudo python3 /opt/homehub/stack/scratch/homehub_scratch.py ensure
#   JSON: uuid, usable_mib, quota_mib {door, meetings, ottertube}, free_at_full_mib (>= 2048). Record them.
```

Then, in `/opt/homehub/stack/.env` **and** HomeHub `config.homehub.psd1`:
`HOMEHUB_SCRATCH_ENABLED=true`, `HOMEHUB_SCRATCH_UUID=<uuid>`, and
`HOMEHUB_DISK_MOUNTS="/ /var/lib/docker /boot /srv/scratch"`. Verify:
`sudo python3 .../homehub_scratch.py status` (`identity_ok: true`,
`prjquota: true`, the three limits); `findmnt /srv/scratch`;
`sudo systemctl start homehub-disk-check.service` and the disk record lists
`/srv/scratch` ok. The mount coming back after a reboot is NOT PROVEN until one
happens (do not reboot for it).

Optional, proves quota enforcement on the hub itself first (1 GiB loop file in
`/tmp`, its own throwaway VG, all removed on exit):
`sudo bash /tmp/w4/scratch/tests/scratch-fs.root-test.sh` → `15 passed, 0 failed`.

### 2. Install the relay and carry the credential (no camera contact yet)

```sh
sudo docker pull alexxit/go2rtc:1.9.14@sha256:675c318b23c06fd862a61d262240c9a63436b4050d177ffc68a32710d9e05bae
sudo docker image inspect --format '{{index .RepoDigests 0}}' alexxit/go2rtc:1.9.14   # the same digest
# payload: stack/door, the compose file and the new .env keys (from .env.example) onto /opt/homehub/stack;
sudo install -m 0644 /opt/homehub/stack/door/homehub-door-*.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable homehub-door-relay homehub-door-recorder
# the doorbell's in-use fields, panel -> hub, through a pipe (values never printed, never in argv):
ssh panel@<panel> 'sudo -n bash /tmp/w4/door/door-cutover-panel.sh export-upstream --refuse-host <hub>' \
  | ssh hub@<hub> 'sudo -n python3 /opt/homehub/stack/door/door_relay.py import-upstream'
#   "wrote DOOR_RELAY_UPSTREAM_HOST ... _USERNAME into /opt/homehub/stack/.env (values not shown)"
```

Set `DOOR_RELAY_ENABLED=true` (the recorder stays `false` until after the gate).
Do not start the relay yet: it would take the camera from the panel.

### 3. The compatibility gate (a planned window: the door view is down)

Note the start time.

```sh
# panel: rollback material, then the broker off
sudo bash /tmp/w4/door/door-cutover-panel.sh prepare            # -> /var/lib/wall-door-cutover/<UTC>/ + checksums
sudo bash /tmp/w4/door/door-cutover-panel.sh suppress
# hub: relay up; the readiness gate, then the hub half of the gate
sudo systemctl start homehub-door-relay
sudo python3 /opt/homehub/stack/door/door_relay.py probe --seconds 10
#   {"codec": "h264", "width": 1536, "height": 1536, "packets": > 0, "rc": 0, "pass": true, "reasons": []}
#   exit 0 ONLY on rc 0, packets > 0 and exactly DOOR_RELAY_EXPECT_CODEC / _SIZE (h264, 1536x1536:
#   the raw fisheye, unchanged). A camera that really sends hevc: set DOOR_RELAY_EXPECT_CODEC=hevc first.
#   A relay that accepts the connection but never delivers: after SECONDS + 60 s
#   {"rc": -1, "timeout": true, "removed": "homehub-door-probe", "pass": false}, exit 1. The probe
#   (and the gate's gate-1..3, and cost's grey decoder) `docker rm -f` their own containers on a
#   timeout, Ctrl-C, SIGTERM (an operator's `timeout`) or SIGHUP (a dropped ssh session), so a
#   rerun is never refused with a name conflict and no consumer is left holding a session. A
#   signal that lands while a removal (or the gate's cleanup) is already under way waits for it
#   to finish, then stops the verb. A signalled verb exits 128+N (143, 129) and logs ONE of:
#     door: <verb> stopped by signal N; its consumers were removed (<names>)
#     door: <verb> stopped by signal N; removal NOT confirmed for <names> - check `docker ps -a ...`
#     door: <verb> stopped by signal N; it had started no consumer
#   The second means `docker rm -f` timed out, failed or never ran for those names: run
#   `sudo docker ps -a --filter name=homehub-door-` and `sudo docker rm -f` what is listed.
sudo python3 /opt/homehub/stack/door/door_relay.py gate --seconds 15
#   readiness+geometry {"pass": true}; two-consumers {upstream_connections: 1, producers: 1, consumers >= 2};
#   one-consumer-left {upstream_connections: 1, survivor_running: true, survivor_progressing: true,
#   survivor_frames: [a, b] with b > a, relay_sent_bytes: [..]}; {"check": "gate-hub-half", "pass": true}
#   survivor_frames is the surviving consumer's OWN ffmpeg -progress frame count: the verdict, with
#   survivor_last_frame_age <= 2.5 s (it must still be rising when the window ends).
#   relay_sent_bytes is go2rtc's per-consumer sender counter, for the record only - it rises
#   when a packet is queued for a consumer, so it keeps rising for one that reads nothing.
# panel: the broker's OWN input path against the relay (the broker stays suppressed)
sudo bash /tmp/w4/door/door-cutover-panel.sh compat <hub> 8554 /door 20
#   {"frames": > 0, "stalled": false, "ended_early": false, ...}; it always ends within 20 s (+5 s to
#   kill the decoder) even against a silent session, and exit 1 = no frames, a stall or an early end.
# hub: segments, a relay restart and a camera dropout, with 60 s segments for the window
printf 'DOOR_RECORDER_SEGMENT_SEC=60\n' | sudo tee -a /etc/homehub-monitor/override.env
sudo sed -i 's/^DOOR_RECORDER_ENABLED=.*/DOOR_RECORDER_ENABLED=true/' /opt/homehub/stack/.env
T0="$(date +%s)"; sudo systemctl start homehub-door-recorder; sleep 200
sudo systemctl restart homehub-door-relay; sleep 120          # relay restart: the recorder is back within ~30 s
sudo ss -K dst <cam> dport = :554; sleep 120                   # camera dropout: go2rtc reconnects
sudo journalctl -u homehub-door-relay --since "@$T0" | grep -c 'url=rtsp://\*\*\*@<camera>'   # >= 1: the dropout, redacted (`<camera>` is literal)
sudo journalctl -u homehub-door-relay --since "@$T0" | grep -cF '<cam>'                        # 0: no camera address
sudo python3 /opt/homehub/stack/door/door_recorder.py continuity --since "$T0"   # "between" gaps = the two events, each < 60 s
sudo python3 /opt/homehub/stack/door/door_recorder.py verify /srv/scratch/door/*/door-*.ts   # every line OK
sudo python3 /opt/homehub/stack/door/door_relay.py upstream-count             # 1
```

End of the window (the rollback transaction, step 5, without the wall.env part):
`sudo systemctl stop homehub-door-recorder homehub-door-relay`;
`door_relay.py upstream-count` → 0; on the panel `release`, then
`probe <cam> 554` → `"live": true, "on_target": true`, exit 0 (both are
required: a peer that was already there before the view was asked for, or a
connection with a dead decoder behind it, is not a pass). Remove the override line. Record the
window's duration. **Stop if** the relay drops the camera for more than a minute
or the panel's view or motion degrade.

### 4. Cutover (serialised; nothing else changing on the panel)

```sh
# panel
sudo bash /tmp/w4/door/door-cutover-panel.sh prepare            # keep the printed directory: $D
sudo bash /tmp/w4/door/door-cutover-panel.sh suppress
# hub: relay and recorder up, readiness gate must pass before the panel moves
sudo systemctl start homehub-door-relay homehub-door-recorder
sudo python3 /opt/homehub/stack/door/door_relay.py probe --seconds 10      # packets > 0, or STOP and roll back
# panel: point at the relay and regenerate through the supported path
sudo bash /tmp/w4/door/door-cutover-panel.sh point $D <hub> 8554 /door
sudo bash /tmp/w4/door/door-cutover-panel.sh regenerate $D       # wall-firstboot.sh; host/port/path "match wall.env"
sudo bash /tmp/w4/door/door-cutover-panel.sh release             # only if regenerate left the unit masked
sudo bash /tmp/w4/door/door-cutover-panel.sh probe <hub> 8554    # "live": true, "on_target": true, no :554 peer
```

Verify: hub `upstream-count` = 1 and `ss -tn` shows one connection to `<cam>`;
panel `ss -tn` shows none to `<cam>` and one to `<hub>:8554` while the view is
live; the kiosk journal `[door] ... state=live`; the panel's motion detector
still raises the door view (a real visitor within 24 h, else an open-items C
row). `wall-firstboot.sh` restarts the kiosk; expect the screen to blank once.

**The recorder's 24 h acceptance** (at most 3 gaps, 5 minutes in all):
`door_recorder.py continuity --hours 24`. It counts the gaps between segments
**and** both uncovered edges of the window (the open `.part` is the recording in
progress), and no segment at all is a fail. It reads the tree, so the window must
lie inside what the cap retains: if 7.5 GiB holds less than a day at the camera's
bitrate (the oldest day directory says), the leading edge is pruning, not
downtime - run `continuity --hours 6` every 6 hours instead and add the four
results' `gaps` and `downtime_seconds`. Record which.

**At the same time, in HomeHub** `config.wall.psd1`: `DOORBELL_RTSP_HOST` to the
hub's identity reference, `DOORBELL_RTSP_PORT = '8554'`, `DOORBELL_RTSP_PATH =
'/door'` — so a panel reimage comes back on the relay. Not before the cutover: a
reimage would then point at a relay that is not running.

### 5. Rollback (the cutover reversed)

```sh
sudo bash /tmp/w4/door/door-cutover-panel.sh suppress                      # panel
sudo systemctl stop homehub-door-recorder homehub-door-relay                # hub: the upstream FIRST
sudo python3 /opt/homehub/stack/door/door_relay.py upstream-count           # 0
sudo bash /tmp/w4/door/door-cutover-panel.sh restore $D                    # panel: byte-identical lines
sudo bash /tmp/w4/door/door-cutover-panel.sh regenerate $D
sudo bash /tmp/w4/door/door-cutover-panel.sh compare $D                    # every credential byte-identical
sudo bash /tmp/w4/door/door-cutover-panel.sh probe <cam> 554               # live on the camera
```

and revert the `config.wall.psd1` lines. A return to the camera is only ever
this transaction; the panel never falls back by itself.

### 6. Cost (task 11; declared before testing)

Budget: relay + recorder at or under 10 % of one core averaged over an hour,
and PSI cpu `some avg60` within 5 points of the W3a baseline.

```sh
sudo python3 /opt/homehub/stack/door/door_relay.py cost --seconds 3600 \
    --container homehub-door-relay --container homehub-door-recorder \
    --cgroup recorder-unit=/sys/fs/cgroup/system.slice/homehub-door-recorder.service
# the detector question (measure, do not move): 30 min of 5 fps 256x256 grey decode from the relay
sudo python3 /opt/homehub/stack/door/door_relay.py cost --seconds 1800 --decode-grey-fps 5 \
    --container homehub-door-relay
```

Compare `psi_some_avg60_mean` with `python3 /opt/homehub/stack/monitoring/homehub-telemetry.py summary --hours 24` from before the relay.

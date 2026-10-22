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
  the TS cutter, pruning, guard and quota stops, continuity (TC-1377..TC-1397).
* `tests/door-relay-lab.sh` (root + docker; TC-1385..TC-1392): the real relay
  against a fake single-session camera — listeners, auth, geometry, the gate,
  the panel broker's own input path, decodable video-only segments across a
  relay restart, no secret in any log, the cost tool.
* `tests/door-cutover-panel.test.sh` (hermetic; TC-1398): the panel script's
  file verbs on a fake tree.

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
#   {"codec": "h264"|"hevc", "width": 1536, "height": 1536, "packets": > 0}   (geometry unchanged, raw fisheye)
sudo python3 /opt/homehub/stack/door/door_relay.py gate --seconds 15
#   readiness+geometry; two-consumers {upstream_connections: 1, producers: 1, consumers >= 2};
#   one-consumer-left {upstream_connections: 1, survivor_running: true}; {"check": "gate-hub-half", "pass": true}
# panel: the broker's OWN input path against the relay (the broker stays suppressed)
sudo bash /tmp/w4/door/door-cutover-panel.sh compat <hub> 8554 /door 20      # frames > 0, decoder counts
# hub: segments, a relay restart and a camera dropout, with 60 s segments for the window
printf 'DOOR_RECORDER_SEGMENT_SEC=60\n' | sudo tee -a /etc/homehub-monitor/override.env
sudo sed -i 's/^DOOR_RECORDER_ENABLED=.*/DOOR_RECORDER_ENABLED=true/' /opt/homehub/stack/.env
sudo systemctl start homehub-door-recorder; sleep 200
sudo systemctl restart homehub-door-relay; sleep 120          # relay restart: the recorder is back within ~30 s
sudo ss -K dst <cam> dport = :554; sleep 120                   # camera dropout: go2rtc reconnects
sudo python3 /opt/homehub/stack/door/door_recorder.py continuity --hours 1   # gaps = the two events, each < 60 s
sudo python3 /opt/homehub/stack/door/door_recorder.py verify /srv/scratch/door/*/door-*.ts   # every line OK
sudo python3 /opt/homehub/stack/door/door_relay.py upstream-count             # 1
```

End of the window (the rollback transaction, step 5, without the wall.env part):
`sudo systemctl stop homehub-door-recorder homehub-door-relay`;
`door_relay.py upstream-count` → 0; on the panel `release`, then
`probe <cam> 554` → `"on_target": true`. Remove the override line. Record the
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

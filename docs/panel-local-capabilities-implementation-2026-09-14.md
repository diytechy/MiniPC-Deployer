# Panel local-capability image contract

Status: implemented and independently reviewed in the isolated provisioning
checkout; not deployed.

The wall image stages the hash-locked sensor wheelhouse and installs
`wall-sensors.service` on first boot even when no gateway registration exists.
The app artifact must declare `local-capabilities-v1` and carry
`electron/panel-access.cjs`, `electron/sensor-observations.cjs`, and
`sensors/config.py`. A private model bundle is optional. When supplied, its
manifest must name exactly `det_10g.onnx` and `w600k_r50.onnx` with lowercase
SHA-256 digests; verified bytes and the canonical raw mapping land at
`/opt/wall-sensors/models/manifest.json` atomically. Missing models leave face
unready without blocking motion, Bluetooth or PIN.

`wall-local-setup.service` owns `/run/wall-local-setup/service.sock` and accepts
one bounded JSON request from the `panel` UID. `status` returns only revision,
bootstrapAllowed, localConfigured, and remoteConfigured. `configure` accepts
`expectedRevision`, `accessMode:"local"`, and, for a protected gateway panel,
the current session and PIN. The helper repeats that authorization directly to
the root-read gateway registration and never logs or stores either value. It
then atomically merges the local fields into `host.json`, preserves all gateway,
renderer, PIN and enrollment state, and persists the choice in root-owned
`local-capabilities.json`. Firstboot reapplies that source of truth.

Camera discovery is separate from capture. Firstboot removes only the retired
product-owned `/etc/modprobe.d/wall-camera-off.conf` and may load `uvcvideo`; it
does not open a video node. Saved schema-v2 sensor configuration with
`cameraConsentVersion:1` is the authority. An absent or false legacy flag cannot
block explicit local opt-in, and migration does not turn capture on.

Software camera/Bluetooth wake keeps the CPU running and uses display-off.
`wall-sleep.sh` derives this from `/var/lib/wall-sensors/config.json`; missing or
invalid config conservatively selects display-off because suspend would make an
intended software wake impossible. A `wake` helper request is checked against a
fresh positive protocol-v2 sensor status before the helper invokes
`wall-sleep.sh sensor-wake`. That operation raises the backlight and clears the
absence clock; it does not unlock the UI.

## Rollback

Stop and disable `wall-local-setup.service`, then remove its unit, executable and
runtime socket. Keep `/etc/wall-panel/host.pre-local-capabilities.json` private;
it is the pre-change backup and may contain gateway credentials. Prefer removing
only `localAccessEnabled`, `localCapabilitiesVersion`, `localFacePolicyVersion`,
`localSetupSocket`, and `sensorModelManifest` from the current host JSON so newer
renderer or gateway values survive. Restore the backup only when the whole host
file must return to its exact earlier state. Install it as `root:panel` mode
0640 for this app contract. If the app is also rolled back to the legacy reader,
install it as `panel:panel` mode 0600, which is that reader's known private-file
contract. Never print either file.

To restore the prior driver policy, use the one-time private record firstboot
created. If `/etc/wall-panel/wall-camera-off.pre-local-capabilities.conf`
exists, stop the sensor service and install that exact file back at
`/etc/modprobe.d/wall-camera-off.conf` as `root:root` mode 0644. If the mutually
exclusive `.absent` marker exists, leave the policy absent. Never invent a
blacklist when the recorded prior state was absent. Reboot after restoring the
recorded policy. To roll back sensor provisioning, stop and disable
`wall-sensors.service` and restore the earlier app and image together. Preserve
`/var/lib/wall-panel` and `/var/lib/wall-sensors` unless the Owner explicitly
intends to delete PIN, keys, enrollment and saved sensor choices. Removing those
directories is credential recovery, not ordinary rollback.

No live deployment or hardware result is claimed here. Camera LED/capture,
physical wake, model inference and reboot behavior remain real-panel acceptance.

"""The camera clock from the hub (SN-055, SR-161).

What runs here, on the dev PC: the settings and refusals, the SDK pin (bytes,
missing and unpinned files), the login struct's layout against the header's
pinned offset, the set decision (a fresh power-up, drift either way, in step),
one sync pass against a fake SDK (what is set, what is logged, that the
credential appears in no log or status, that a camera which does not keep the
time fails), main's cleanup on every path, and the unit/timer/template contract.

What does not: Hikvision's library and the camera. Proven by hand 2026-11-09
15:33 (login, read 1970, set, read back, the overlay right); the deploy step
repeats it through the unit.
Implements: TC-1748..TC-1756.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
for sub in ("camera-clock", "camera-relay", "door", "monitoring"):
    sys.path.insert(0, str(STACK / sub))
import camera_clock as cc  # noqa: E402

PASSWORD = "ABCDEF"
T = datetime.datetime


def write_env(tmp_path, **over):
    lines = {
        "LAN_IP": "192.0.2.10",
        "CAMERA_LIVINGROOM_RELAY_RTSP_PORT": "8564",
        "CAMERA_LIVINGROOM_RELAY_API_PORT": "1994",
        "CAMERA_LIVINGROOM_RELAY_UPSTREAM_HOST": "192.0.2.69",
        "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PATH": "/h264/ch1/sub/av_stream",
        "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD": PASSWORD,
        "CAMERA_LIVINGROOM_RELAY_EXPECT_SIZE": "768x432",
        "CAMERA_LIVINGROOM_CLOCK_ENABLED": "true",
    }
    lines.update(over)
    path = tmp_path / "env"
    path.write_text("".join("%s=%s\n" % kv for kv in lines.items() if kv[1] is not None))
    return str(path)


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))


class FakeSdk:
    def __init__(self, camera_time, keeps=True, login_ok=True, login_code=1, get_raises=None):
        self.camera_time, self.keeps, self.login_ok = camera_time, keeps, login_ok
        self.login_code, self.get_raises = login_code, get_raises
        self.calls, self.closed, self.login_args = [], False, None

    def login(self, host, port, username, password):
        self.calls.append("login")
        self.login_args = (host, port, username, password)
        if not self.login_ok:
            raise cc.SdkError("login refused (SDK error %d)" % self.login_code, code=self.login_code)

    def get_time(self):
        self.calls.append("get")
        if self.get_raises:
            raise self.get_raises
        return self.camera_time

    def set_time(self, when):
        self.calls.append("set")
        if self.keeps:
            self.camera_time = when

    def close(self):
        self.closed = True


# ── TC-1748 settings ─────────────────────────────────────────────────────────


def test_tc1748_settings_take_the_relays_credential_and_their_own_knobs(tmp_path):
    s = cc.ClockSettings(write_env(tmp_path), "livingroom")
    assert (s.host, s.username, s.password, s.port, s.max_drift) == ("192.0.2.69", "admin", PASSWORD, 8000, 5)
    assert s.enabled and s.sdk_dir == "/opt/homehub-camera-clock/sdk"


@pytest.mark.parametrize("over, key", [
    ({"CAMERA_LIVINGROOM_CLOCK_PORT": "80000"}, "CAMERA_LIVINGROOM_CLOCK_PORT"),
    ({"CAMERA_LIVINGROOM_CLOCK_MAX_DRIFT_SEC": "0"}, "CAMERA_LIVINGROOM_CLOCK_MAX_DRIFT_SEC"),
    ({"CAMERA_LIVINGROOM_CLOCK_MAX_DRIFT_SEC": "2.5"}, "CAMERA_LIVINGROOM_CLOCK_MAX_DRIFT_SEC"),
    ({"CAMERA_CLOCK_SDK_DIR": "relative/sdk"}, "CAMERA_CLOCK_SDK_DIR"),
    ({"CAMERA_CLOCK_SDK_DIR": "/opt/../etc"}, "CAMERA_CLOCK_SDK_DIR"),
    ({"CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD": "x" * 64}, "longer than the SDK accepts"),
])
def test_tc1748_refusals_name_the_key_and_never_the_value(tmp_path, over, key):
    with pytest.raises((cc.Invalid, cc.door_relay.Invalid)) as err:
        cc.ClockSettings(write_env(tmp_path, **over), "livingroom")
    assert key in str(err.value) and PASSWORD not in str(err.value)


# ── TC-1749 the SDK pin ──────────────────────────────────────────────────────


def sdk_tree(tmp_path, files):
    lib = tmp_path / "sdk" / "lib"
    for rel, data in files.items():
        (lib / rel).parent.mkdir(parents=True, exist_ok=True)
        (lib / rel).write_bytes(data)
    return str(tmp_path / "sdk"), {"files": {rel: hashlib.sha256(d).hexdigest() for rel, d in files.items()}}


def test_tc1749_only_the_pinned_files_with_their_bytes_verify(tmp_path):
    sdk_dir, manifest = sdk_tree(tmp_path, {"libhcnetsdk.so": b"a", "HCNetSDKCom/libHCPreview.so": b"b"})
    assert cc.verify_sdk(sdk_dir, manifest).endswith("lib")
    (Path(sdk_dir) / "lib" / "HCNetSDKCom" / "libHCPreview.so").write_bytes(b"changed")
    with pytest.raises(cc.Invalid, match="not the pinned bytes"):
        cc.verify_sdk(sdk_dir, manifest)


def test_tc1749_a_missing_or_an_extra_file_refuses(tmp_path):
    sdk_dir, manifest = sdk_tree(tmp_path, {"libhcnetsdk.so": b"a"})
    (Path(sdk_dir) / "lib" / "planted.so").write_bytes(b"x")
    with pytest.raises(cc.Invalid, match="unpinned"):
        cc.verify_sdk(sdk_dir, manifest)
    with pytest.raises(cc.Invalid, match="missing"):
        cc.verify_sdk(sdk_dir, dict(manifest, files=dict(manifest["files"], **{"libgone.so": "0" * 64})))


def test_tc1749_the_committed_manifest_pins_the_release_and_its_libraries():
    m = cc.read_manifest()
    assert re.fullmatch(r"[0-9a-f]{64}", m["zip"]["sha256"]) and m["zip"]["url"].startswith("https://assets.hikvision.com/")
    for name in ("libhcnetsdk.so", "libcrypto.so.3", "libssl.so.3", "HCNetSDKCom/libHCCoreDevCfg.so"):
        assert re.fullmatch(r"[0-9a-f]{64}", m["files"][name]), name
    assert all(not r.startswith("/") and ".." not in r.split("/") for r in m["files"])


# ── TC-1750 the SDK's struct layout ──────────────────────────────────────────


def test_tc1750_the_login_struct_puts_the_v40_callback_where_the_header_pins_it():
    assert cc._LoginInfo.cbLoginResultV40.offset == 296
    assert cc._LoginInfo.sUserName.offset == 132 and cc._LoginInfo.sPassword.offset == 196
    assert cc.ctypes.sizeof(cc._Time) == 24


# ── TC-1751 the decision ─────────────────────────────────────────────────────


@pytest.mark.parametrize("camera, hub, expect", [
    (T(1970, 1, 1, 0, 20, 6), T(2026, 11, 9, 15, 33, 38), True),   # a fresh power-up (measured)
    (T(2026, 11, 9, 15, 33, 0), T(2026, 11, 9, 15, 33, 38), True),  # 38 s slow
    (T(2026, 11, 9, 16, 33, 38), T(2026, 11, 9, 15, 33, 38), True),  # an hour off (a DST change)
    (T(2026, 11, 9, 15, 33, 40), T(2026, 11, 9, 15, 33, 38), False),  # in step
    (T(2023, 12, 31, 23, 59, 59), T(2023, 12, 31, 23, 59, 59), True),  # before SANE_YEAR: always set
])
def test_tc1751_set_when_off_by_more_than_the_drift_or_freshly_powered_up(camera, hub, expect):
    assert cc.needs_set(camera, hub, 5)[0] is expect


# ── TC-1752 one sync pass ────────────────────────────────────────────────────


def test_tc1752_a_fresh_power_up_is_set_and_logged_without_the_credential(tmp_path):
    s = cc.ClockSettings(write_env(tmp_path), "livingroom")
    sdk, lines = FakeSdk(T(1970, 1, 1, 0, 20, 6)), []
    rec = cc.sync(s, sdk, now=lambda: T(2026, 11, 9, 15, 33, 51), log=lambda m, warning=False: lines.append(m),
                  synchronized=lambda: True)
    assert sdk.calls == ["login", "get", "set", "get"] and sdk.login_args == ("192.0.2.69", 8000, "admin", PASSWORD)
    assert rec["set"] is True and rec["after"] == "2026-11-09T15:33:51"
    assert len(lines) == 1 and "1970-01-01 00:20:06" in lines[0]
    assert PASSWORD not in json.dumps(rec) + "".join(lines) and "192.0.2.69" not in "".join(lines)


def test_tc1752_a_clock_in_step_is_left_alone_and_logs_nothing(tmp_path):
    s = cc.ClockSettings(write_env(tmp_path), "livingroom")
    sdk, lines = FakeSdk(T(2026, 11, 9, 15, 33, 52)), []
    rec = cc.sync(s, sdk, now=lambda: T(2026, 11, 9, 15, 33, 51), log=lambda m, warning=False: lines.append(m),
                  synchronized=lambda: True)
    assert sdk.calls == ["login", "get"] and rec["set"] is False and lines == []


def test_tc1752_a_camera_that_does_not_keep_the_time_fails(tmp_path):
    s = cc.ClockSettings(write_env(tmp_path), "livingroom")
    with pytest.raises(cc.SdkError, match="did not keep"):
        cc.sync(s, FakeSdk(T(1970, 1, 1), keeps=False), now=lambda: T(2026, 11, 9, 15, 0, 0),
                log=lambda *a, **k: None, synchronized=lambda: True)


# ── TC-1753 main: cleanup on every path, status, refusals ────────────────────


def run_main(tmp_path, monkeypatch, sdk, verb="sync", **env_over):
    path = write_env(tmp_path, **env_over)
    monkeypatch.setattr(cc, "verify_sdk", lambda d, m: "/fake/lib")
    monkeypatch.setattr(cc, "hub_clock_synchronized", lambda: True)
    monkeypatch.setattr(cc, "STATE_DIR", str(tmp_path / "state"))
    status = {}
    monkeypatch.setattr(cc, "write_status", lambda cam, rec, state_dir=None: status.update(rec))
    return cc.main(["--env-file", path, "--camera", "livingroom", verb], sdk_factory=lambda lib: sdk), status


def test_tc1753_the_sdk_is_closed_after_a_refused_login_and_the_failure_is_recorded(tmp_path, monkeypatch):
    sdk = FakeSdk(T(1970, 1, 1), login_ok=False)
    rc, status = run_main(tmp_path, monkeypatch, sdk)
    assert rc == 1 and sdk.closed and status["ok"] is False and "login refused" in status["error"]


def test_tc1753_a_good_pass_closes_and_records(tmp_path, monkeypatch):
    sdk = FakeSdk(T(1970, 1, 1))
    rc, status = run_main(tmp_path, monkeypatch, sdk)
    assert rc == 0 and sdk.closed and status["ok"] is True and status["set"] is True
    assert PASSWORD not in json.dumps(status)


def test_tc1753_an_unpinned_sdk_refuses_with_78_before_any_sdk_call(tmp_path, monkeypatch):
    called = []
    path = write_env(tmp_path, CAMERA_CLOCK_SDK_DIR=str(tmp_path / "nosdk"))
    rc = cc.main(["--env-file", path, "--camera", "livingroom", "sync"], sdk_factory=lambda lib: called.append(1))
    assert rc == cc.EXIT_REFUSED and called == []


def test_tc1753_condition_follows_the_knob_exactly(tmp_path):
    assert cc.main(["--env-file", write_env(tmp_path), "--camera", "livingroom", "condition"]) == 0
    off = write_env(tmp_path, CAMERA_LIVINGROOM_CLOCK_ENABLED="yes")
    assert cc.main(["--env-file", off, "--camera", "livingroom", "condition"]) == 1


# ── TC-1754..TC-1756 unit, timer, template, installer ────────────────────────


def unit(name):
    return (STACK / "camera-clock" / name).read_text(encoding="utf-8")


def test_tc1754_the_unit_confines_the_vendor_library():
    live = [l for l in unit("homehub-camera-clock@.service").splitlines() if not l.startswith("#")]
    for line in ("Type=oneshot", "CapabilityBoundingSet=", "NoNewPrivileges=true", "ProtectSystem=strict",
                 "PrivateDevices=true", "IPAddressDeny=any", "StateDirectory=homehub-camera-clock",
                 "WorkingDirectory=/var/lib/homehub-camera-clock",
                 "IPAddressAllow=localhost 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"):
        assert line in live, line
    text = "\n".join(live)
    assert "camera_clock.py --camera %i condition" in text and "camera_clock.py --camera %i sync" in text
    assert not [l for l in live if l.startswith(("Requires=", "PartOf=")) and "docker" in l]


def test_tc1755_the_timer_runs_after_boot_and_every_ten_minutes():
    t = unit("homehub-camera-clock@.timer").splitlines()
    assert "OnBootSec=2min" in t and "OnUnitActiveSec=10min" in t and "WantedBy=timers.target" in t


def env_example(key):
    m = re.search(r"^%s=(.*)$" % key, (STACK / ".env.example").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def test_tc1756_the_template_ships_it_off_and_the_installer_checks_before_it_swaps():
    assert env_example("CAMERA_LIVINGROOM_CLOCK_ENABLED") == "false"
    assert env_example("CAMERA_LIVINGROOM_CLOCK_PORT") == "8000"
    sh = unit("install-camera-clock-sdk.sh")
    assert sh.index('[ "$(sha256sum "$zip"') < sh.index("zipfile.ZipFile") < sh.index("verify_sdk") < sh.index('mv -T "$stage"')
    # One rename switches releases: a symlink, never two moves (review 2026-11-09).
    assert 'ln -sfn "$release" "$root/.sdk.new"' in sh and 'mv -T "$root/.sdk.new" "$root/sdk"' in sh
    assert sh.index('mv -T "$stage" "$release"') < sh.index('ln -sfn')


# ── review 2026-11-09: each fix pinned ───────────────────────────────────────


def test_tc1752_an_unsynchronised_hub_never_writes_its_clock_to_the_camera(tmp_path):
    s = cc.ClockSettings(write_env(tmp_path), "livingroom")
    for synced, hub in ((False, T(2026, 11, 9, 15, 0, 0)), (True, T(2019, 6, 1, 0, 0, 0))):
        sdk, lines = FakeSdk(T(1970, 1, 1)), []
        rec = cc.sync(s, sdk, now=lambda: hub, log=lambda m, warning=False: lines.append(m),
                      synchronized=lambda: synced)
        assert "set" not in sdk.calls and rec["set"] is False and "not synchronised" in rec["skipped"]
        assert len(lines) == 1


def test_tc1752_hub_clock_synchronized_reads_the_flag_then_timedatectl(tmp_path):
    flag = tmp_path / "synchronized"
    flag.write_text("")
    assert cc.hub_clock_synchronized(str(flag))
    class Done:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode = out, rc
    absent = str(tmp_path / "absent")
    assert cc.hub_clock_synchronized(absent, run=lambda *a, **k: Done("yes" + chr(10)))
    assert not cc.hub_clock_synchronized(absent, run=lambda *a, **k: Done("no" + chr(10)))
    def boom(*a, **k):
        raise OSError("no timedatectl")
    assert not cc.hub_clock_synchronized(absent, run=boom)


def test_tc1753_an_unexpected_error_leaves_a_red_status_not_a_stale_green_one(tmp_path, monkeypatch):
    sdk = FakeSdk(T(1970, 1, 1), get_raises=ValueError("month 0"))
    rc, status = run_main(tmp_path, monkeypatch, sdk)
    assert rc == 1 and sdk.closed and status["ok"] is False and "ValueError" in status["error"]


def test_tc1753_a_refused_credential_backs_off_and_a_network_failure_does_not(tmp_path, monkeypatch):
    sdk = FakeSdk(T(1970, 1, 1), login_ok=False, login_code=1)
    rc, status = run_main(tmp_path, monkeypatch, sdk)
    assert rc == 1 and (tmp_path / "state" / "livingroom.backoff").exists()
    assert "wrong username or password" in status["error"]
    again = FakeSdk(T(1970, 1, 1))
    rc, status = run_main(tmp_path, monkeypatch, again)
    assert rc == 0 and again.calls == [] and "backing off" in status["error"]
    (tmp_path / "state" / "livingroom.backoff").unlink()
    net = FakeSdk(T(1970, 1, 1), login_ok=False, login_code=7)
    run_main(tmp_path, monkeypatch, net)
    assert not (tmp_path / "state" / "livingroom.backoff").exists()


def test_tc1753_the_sdk_runs_from_an_empty_private_directory(tmp_path, monkeypatch):
    seen = []
    class Spy(FakeSdk):
        def login(self, *a):
            seen.append(cc.os.getcwd())
            super().login(*a)
    before = cc.os.getcwd()
    run_main(tmp_path, monkeypatch, Spy(T(1970, 1, 1)))
    assert seen and seen[0] != before and "camera-clock-" in seen[0] and cc.os.getcwd() == before
    assert not cc.os.path.exists(seen[0])


class FakeLib:
    def __init__(self, log_ok):
        self.log_ok, self.cleaned = log_ok, False

    def NET_DVR_SetSDKInitCfg(self, *a):
        return 1

    def NET_DVR_Init(self):
        return 1

    def NET_DVR_SetLogToFile(self, *a):
        return 1 if self.log_ok else 0

    def NET_DVR_GetLastError(self):
        return 103

    def NET_DVR_SetConnectTime(self, *a):
        return 1

    def NET_DVR_Cleanup(self):
        self.cleaned = True


def test_tc1750_a_log_that_cannot_be_closed_refuses_and_cleans_up():
    lib = FakeLib(log_ok=False)
    with pytest.raises(cc.SdkError, match="103"):
        cc.HikSdk("/fake/lib", loader=lambda path: lib)
    assert lib.cleaned
    assert cc.HikSdk("/fake/lib", loader=lambda path: FakeLib(log_ok=True)).uid == -1

"""The hub's NVMe scratch volume: capacity, budgets, the provisioner's refusals,
and the mount guard's verdicts (SN-050, SR-102..SR-104).

Pure functions and the provisioner's decision walk with every command and file
faked, so this runs on the dev PC. The filesystem-level cases (a real ext4 with
project quotas: a writer hitting its quota, one writer's exhaustion not touching
another, the 2 GiB floor with all three full, CAP_SYS_RESOURCE ignoring the
limit, unmount mid-run) need root and a kernel with ext4 quota support, and
live in stack/scratch/tests/scratch-fs.root-test.sh. Implements: TC-1370..TC-1375.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stack" / "scratch"))
sys.path.insert(0, str(ROOT / "stack" / "monitoring"))
import homehub_scratch as hs  # noqa: E402

GIB, MIB = hs.GIB, hs.MIB
UUID = "0f1e2d3c-4b5a-4968-8776-655443322110"


# ── TC-1370 capacity ─────────────────────────────────────────────────────────


def test_tc1370_the_measured_hub_vg_gives_18_gib_and_keeps_5_24():
    # 2026-10-22, the hub: 5949 free extents of 4 MiB (23.24 GiB).
    plan = hs.capacity_plan(5949, 4 * MIB, 18 * 1024, 4 * 1024)
    assert plan["lv_extents"] == 4608
    assert plan["leftover_extents"] == 1341
    assert round(plan["leftover_bytes"] / GIB, 2) == 5.24


def test_tc1370_a_vg_that_cannot_keep_the_reserve_is_a_stop_with_numbers():
    with pytest.raises(hs.Stop) as err:
        hs.capacity_plan(5500, 4 * MIB, 18 * 1024, 4 * 1024)  # would leave 892 extents
    assert "892" in str(err.value) and "do not shrink" in str(err.value)
    # exactly the reserve is allowed
    assert hs.capacity_plan(4608 + 1024, 4 * MIB, 18 * 1024, 4 * 1024)["leftover_extents"] == 1024


def test_tc1370_size_rounds_up_to_whole_extents():
    assert hs.capacity_plan(10, 32 * MIB, 33, 0)["lv_extents"] == 2


# ── TC-1371 budgets ──────────────────────────────────────────────────────────

WANT = {"door": 8192, "meetings": 2048, "clips": 0, "ottertube": 6144}  # clips asks nothing here


def test_tc1371_budgets_that_fit_are_granted_unchanged():
    assert hs.size_quotas(20 * GIB, WANT, 2048, 512) == WANT


def test_tc1371_ottertube_is_reduced_first_so_the_floor_holds():
    usable = 17_800 * MIB  # an 18 GiB ext4 after metadata, roughly
    got = hs.size_quotas(usable, WANT, 2048, 512)
    assert got["door"] == 8192 and got["meetings"] == 2048
    assert got["ottertube"] == 17_800 - 2048 - 8192 - 2048
    assert usable // MIB - sum(got.values()) == 2048


def test_tc1371_meetings_only_after_ottertube_is_at_its_minimum_and_a_starved_set_stops():
    # usable 11752 - floor 2048 = 9704 for 16384 wanted: ottertube down to its
    # 512 minimum first, then meetings gives up what is still over; door untouched.
    got = hs.size_quotas(11752 * MIB, WANT, 2048, 512)
    assert got == {"door": 8192, "meetings": 1000, "clips": 0, "ottertube": 512}
    with pytest.raises(hs.Stop):
        hs.size_quotas((512 * 3 + 2048 - 1) * MIB, WANT, 2048, 512)
    with pytest.raises(hs.Stop):
        hs.size_quotas(1000 * MIB, WANT, 2048, 512)


# ── TC-1374 the guard's verdicts ─────────────────────────────────────────────


def good_facts(**over):
    facts = dict(writer="door", known_writer=True, expected_uuid=UUID, uuid_malformed=False, mounted=True,
                 mount_majmin="253:3", uuid_majmin="253:3", fstype="ext4", superopts="rw,prjquota",
                 read_only=False, has_cap_sys_resource=False, dir_kind="dir", dir_dev_ok=True,
                 project_id=7101, project_inherit=True, probe_error=None)
    facts.update(over)
    return facts


@pytest.mark.parametrize("change, words", [
    (dict(writer="x", known_writer=False), "unknown writer"),
    (dict(expected_uuid="", uuid_malformed=True), "not a filesystem UUID"),
    (dict(expected_uuid=""), "unset"),
    (dict(mounted=False), "not a mount point"),
    (dict(uuid_majmin=None), "absent"),
    (dict(uuid_majmin="253:0"), "wrong device"),
    (dict(fstype="tmpfs"), "not ext4"),
    (dict(superopts="rw,quota"), "without prjquota"),
    (dict(read_only=True), "read-only"),
    (dict(has_cap_sys_resource=True), "CAP_SYS_RESOURCE"),
    (dict(dir_kind="missing"), "not a directory"),
    (dict(dir_kind="symlink"), "not a directory"),
    (dict(dir_dev_ok=False), "not on the scratch filesystem"),
    (dict(project_id=0), "quota project"),
    (dict(project_inherit=False), "quota project"),
    (dict(probe_error="EDQUOT"), "not writable (EDQUOT)"),
])
def test_tc1374_every_failed_condition_refuses_and_says_which(change, words):
    ok, reason = hs.judge_guard(good_facts(**change))
    assert not ok and words in reason


def test_tc1374_all_conditions_true_is_the_only_pass():
    assert hs.judge_guard(good_facts()) == (True, "ok")
    assert hs.judge_guard(good_facts(writer="ottertube", project_id=7103)) == (True, "ok")
    assert not hs.judge_guard(good_facts(writer="meetings"))[0]  # door's project is not meetings'


# ── TC-1375 the guard's fact gathering, on a fake root ───────────────────────


def test_tc1375_mountinfo_keeps_superopts_and_unescapes():
    m = hs.parse_mountinfo(
        "29 1 253:0 / / rw,relatime shared:1 - ext4 /dev/mapper/vg-root rw\n"
        "88 29 253:3 / /srv/scr\\040atch rw,noatime shared:40 - ext4 /dev/mapper/ubuntu--vg-hub--scratch rw,prjquota\n")
    assert hs.mount_of(m, "/srv/scr atch")["superopts"] == "rw,prjquota"
    assert hs.mount_of(m, "/nope") is None


def test_tc1375_cap_sys_resource_is_read_from_capeff(tmp_path):
    status = tmp_path / "status"
    status.write_text("Name:\tx\nCapEff:\t000001ffffffffff\n")
    assert hs.holds_cap_sys_resource(str(status))
    status.write_text("Name:\tx\nCapEff:\t000001fffeffffff\n")  # bit 24 cleared
    assert not hs.holds_cap_sys_resource(str(status))
    assert not hs.holds_cap_sys_resource(str(tmp_path / "missing"))


def test_tc1375_an_unreadable_fact_is_a_refusal_never_an_exception(tmp_path):
    paths = hs.Paths(str(tmp_path))
    paths.mountinfo = str(tmp_path / "no-mountinfo")
    paths.by_uuid = str(tmp_path / "by-uuid")
    paths.status = str(tmp_path / "no-status")
    s = hs.Settings({"HOMEHUB_SCRATCH_UUID": UUID})
    ok, reason = hs.guard("door", s, paths)
    assert not ok and "not a mount point" in reason


def test_tc1375_settings_reject_a_malformed_uuid_and_read_budgets():
    s = hs.Settings({"HOMEHUB_SCRATCH_UUID": "not-a-uuid", "HOMEHUB_SCRATCH_QUOTA_DOOR_MIB": "4096"})
    assert s.uuid == "" and s.uuid_malformed
    assert s.quota_mib["door"] == 4096 and s.quota_mib["ottertube"] == 6144
    assert hs.Settings({"HOMEHUB_SCRATCH_UUID": UUID.upper()}).uuid == UUID


# ── TC-1372 / TC-1373 the provisioner: non-destructive, end-state ────────────


class FakeHost:
    """Every command the provisioner may run, answered from a small model."""

    def __init__(self, lv_exists=False, signature=None, free=5949):
        self.lv_exists, self.signature, self.free = lv_exists, signature, free
        self.ran = []

    def run(self, argv, check=True):
        self.ran.append(list(argv))

        class Done:
            returncode, stdout, stderr = 0, "", ""
        d = Done()
        if argv[0] == "vgs":
            d.stdout = "  %d 4194304\n" % self.free
        elif argv[0] == "lvs":
            if self.lv_exists:
                d.stdout = "  /dev/ubuntu-vg/hub-scratch\n"
            else:
                d.returncode = 5
        elif argv[0] == "blkid":
            if self.signature:
                d.stdout = "".join("%s=%s\n" % kv for kv in self.signature.items())
            else:
                d.returncode = 2
        elif argv[0] == "dumpe2fs":
            d.stdout = "Filesystem features:      has_journal ext_attr extent quota project\n"
        return d


@pytest.fixture
def host(monkeypatch, tmp_path):
    fake = FakeHost()
    monkeypatch.setattr(hs, "run", fake.run)
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc" / "mountinfo").write_text("29 1 253:0 / / rw - ext4 /dev/mapper/vg-root rw\n")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "fstab").write_text("UUID=abc / ext4 defaults 0 1\n")
    paths = hs.Paths(str(tmp_path))
    paths.mountinfo = str(tmp_path / "proc" / "mountinfo")
    paths.by_uuid = str(tmp_path / "by-uuid")
    return fake, paths


def provisioner(paths, apply=False, **env):
    return hs.Provisioner(hs.Settings(env), apply, paths, vg="ubuntu-vg")


def test_tc1373_plan_on_the_measured_hub_describes_every_step_and_runs_nothing(host):
    fake, paths = host
    report = provisioner(paths).walk()
    joined = "\n".join(report["actions"])
    assert "create LV ubuntu-vg/hub-scratch, 4608 extents" in joined
    assert "make ext4" in joined and "append to" in joined and "mode 000" in joined
    assert report["vg_leftover_gib"] == 5.24
    assert not [c for c in fake.ran if c[0] in ("lvcreate", "mkfs.ext4", "mount", "systemctl")]


def test_tc1372_an_existing_lv_is_never_recreated_or_resized(host):
    fake, paths = host
    fake.lv_exists = True
    fake.signature = {"TYPE": "ext4", "LABEL": "hub-scratch", "UUID": UUID}
    report = provisioner(paths, HOMEHUB_SCRATCH_UUID=UUID).walk()
    assert report["lv"] == "/dev/ubuntu-vg/hub-scratch" and report["uuid"] == UUID
    assert not any("create LV" in a or "make ext4" in a for a in report["actions"])


def test_tc1372_a_device_with_a_foreign_signature_is_never_formatted(host):
    fake, paths = host
    fake.lv_exists = True
    fake.signature = {"TYPE": "xfs", "LABEL": "something"}
    with pytest.raises(hs.Refused, match="never formatted"):
        provisioner(paths, apply=True).walk()
    assert not [c for c in fake.ran if c[0] == "mkfs.ext4"]


def test_tc1372_an_existing_lv_with_no_signature_is_refused_not_formatted(host):
    # blkid -p exits 2 on it: a damaged primary superblock, or an earlier run
    # killed between lvcreate and mkfs. Only an LV this run created is formatted.
    fake, paths = host
    fake.lv_exists = True
    fake.signature = None
    p = provisioner(paths, apply=True, HOMEHUB_SCRATCH_UUID=UUID)
    with pytest.raises(hs.Refused, match="already existed.*lvremove"):
        p.step_filesystem("/dev/ubuntu-vg/hub-scratch", existed=True)
    with pytest.raises(hs.Refused, match="formats only an LV it has just created"):
        provisioner(paths, apply=True, HOMEHUB_SCRATCH_UUID=UUID).walk()
    with pytest.raises(hs.Refused):  # plan says so too, before anyone runs ensure
        provisioner(paths, HOMEHUB_SCRATCH_UUID=UUID).walk()
    assert not [c for c in fake.ran if c[0] in ("mkfs.ext4", "lvcreate", "lvremove", "wipefs")]


def test_tc1372_an_lv_created_in_this_run_is_formatted(host):
    fake, paths = host
    p = provisioner(paths, apply=True, HOMEHUB_SCRATCH_UUID=UUID)
    p.step_filesystem("/dev/ubuntu-vg/hub-scratch", existed=False)
    assert [c for c in fake.ran if c[0] == "mkfs.ext4"]


def test_tc1372_our_label_with_another_uuid_is_refused(host):
    fake, paths = host
    fake.lv_exists = True
    fake.signature = {"TYPE": "ext4", "LABEL": "hub-scratch", "UUID": "11111111-2222-4333-8444-555555555555"}
    with pytest.raises(hs.Refused, match="not HOMEHUB_SCRATCH_UUID"):
        provisioner(paths, HOMEHUB_SCRATCH_UUID=UUID).walk()


def test_tc1372_a_foreign_fstab_line_for_the_mount_point_is_never_edited(host):
    fake, paths = host
    fake.lv_exists = True
    fake.signature = {"TYPE": "ext4", "LABEL": "hub-scratch", "UUID": UUID}
    foreign = "/dev/sdz1 %s xfs defaults 0 0\n" % paths.mountpoint_name
    Path(paths.fstab).write_text(foreign)
    with pytest.raises(hs.Refused, match="did not write"):
        provisioner(paths, apply=True, HOMEHUB_SCRATCH_UUID=UUID).walk()
    assert Path(paths.fstab).read_text() == foreign


def test_tc1372_a_mount_point_holding_data_is_not_mounted_over(host):
    fake, paths = host
    fake.lv_exists = True
    fake.signature = {"TYPE": "ext4", "LABEL": "hub-scratch", "UUID": UUID}
    Path(paths.fstab).write_text(hs.fstab_line(UUID, paths.mountpoint_name) + "\n")
    os.makedirs(paths.mountpoint)
    Path(paths.mountpoint, "stray.ts").write_text("written to / while unmounted")
    with pytest.raises(hs.Refused, match="not empty"):
        provisioner(paths, apply=True, HOMEHUB_SCRATCH_UUID=UUID).walk()


def test_tc1372_insufficient_space_stops_before_any_command(host):
    fake, paths = host
    fake.free = 5000
    with pytest.raises(hs.Stop):
        provisioner(paths, apply=True).walk()
    assert not [c for c in fake.ran if c[0] not in ("vgs", "lvs")]


def test_tc1373_fstab_line_is_by_uuid_nofail_prjquota_and_recognised_as_ours():
    line = hs.fstab_line(UUID)
    assert line.startswith("UUID=%s /srv/scratch ext4 " % UUID)
    for opt in ("nofail", "prjquota", "noatime"):
        assert opt in line.split()[3].split(",")
    assert hs.fstab_state(line + "\n", UUID) == "ours"
    assert hs.fstab_state("# " + line + "\n", UUID) == "absent"
    assert hs.fstab_state("", UUID) == "absent"


def test_tc1373_mkfs_is_ext4_with_quota_and_project_and_no_force_flag():
    args = hs.MKFS_ARGS
    assert args[args.index("-O") + 1] == "quota,project"
    assert args[args.index("-m") + 1] == "0"
    assert args[args.index("-L") + 1] == "hub-scratch"
    assert "-F" not in args

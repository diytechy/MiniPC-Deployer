"""The autoinstall's own diagnosability contract.

WHY THIS FILE EXISTS. On 2026-09-11 a hub install over Ethernet stalled in
curthooks for two hours and the cause could not be determined -- not during, and
not afterwards. Three vantages, all empty by construction: kexec leaves no
console, the installer's logs live in tmpfs and die at power-off, and subiquity
copies them into the target only at the END of a successful install. The SMB
server logged no fault, so even the workstation side said nothing.

The install itself was fine to retry. Being unable to SEE it was the defect, and
it is the kind that returns silently once the instrumentation is edited away by
someone who does not know what it cost. So the contract is executed here rather
than asserted in a comment.
"""

from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
USER_DATA = ROOT / "stack/autoinstall/user-data"


@pytest.fixture(scope="module")
def autoinstall():
    return yaml.safe_load(USER_DATA.read_text(encoding="utf-8"))["autoinstall"]


def test_early_commands_exist_and_are_shell_valid(autoinstall):
    # A syntax error here does not fail one step, it fails every install on
    # every machine -- and the operator finds out at the box, not here.
    commands = autoinstall.get("early-commands") or []
    assert commands, "early-commands is where the install becomes observable"
    for index, command in enumerate(commands):
        result = subprocess.run(
            ["bash", "-n"], input=command, text=True, capture_output=True
        )
        assert result.returncode == 0, f"early-commands[{index}]: {result.stderr}"


def test_instrumentation_can_never_fail_the_install(autoinstall):
    # THE RULE. A diagnostic that can break the thing it watches is worse than
    # no diagnostic: it converts "I could not see why it failed" into "it failed
    # because I was looking". Every early-command swallows its own errors.
    for command in autoinstall["early-commands"]:
        assert "|| true" in command, "an instrumentation step must not be fatal"


def test_the_installer_environment_gets_the_operator_key(autoinstall):
    # subiquity's `ssh:` block configures the TARGET. It does nothing for the
    # environment the install actually runs in, which is the only place there is
    # anything to watch while an install is stuck.
    joined = "\n".join(autoinstall["early-commands"])
    assert "/root/.ssh/authorized_keys" in joined
    assert "systemctl start ssh" in joined
    # The key is read back from the seed rather than duplicated: a second copy
    # is a second thing to rotate, and it would go stale silently.
    assert "/cdrom/nocloud/user-data" in joined or "/autoinstall.yaml" in joined


def test_a_stalled_install_leaves_a_heartbeat_and_a_cause_on_disk(autoinstall):
    # HEARTBEAT ANSWERS THE QUESTION NOBODY COULD ANSWER THAT NIGHT: is the
    # installer alive and curtin stuck, or is the whole box dead? A timestamp
    # that advances while curtin has not moved separates those two. `ps` and the
    # journal tail then name what it is blocked on.
    shipper = "\n".join(autoinstall["early-commands"])
    assert "HEARTBEAT" in shipper
    assert "ps auxf" in shipper
    assert "journalctl" in shipper
    # It must land on the TARGET DISK. tmpfs is what died at power-off and is
    # the whole reason there was nothing to read.
    assert "/target/var/log" in shipper


def test_instrumentation_did_not_disturb_the_destructive_parts(autoinstall):
    # The storage pin and the reboot behaviour are the two settings where a
    # careless edit is measured in wiped disks and site visits.
    assert set(autoinstall["storage"]["layout"]["match"]) <= {"serial", "path", "model", "wwn"}
    assert autoinstall["interactive-sections"] == []
    assert autoinstall["packages"] == []


# ── os-prober: the 2026-09-11 root cause ────────────────────────────────────
# curthooks runs `update-grub` in the target; grub-mkconfig runs
# /etc/grub.d/30_os-prober; os-prober hands every foreign partition to
# `grub-mount`. On the hub that is GRUB's NTFS driver walking a 3.6 TB Library
# drive -- caught at 74.6% CPU, 20m27s of CPU time, uninterruptible sleep, with
# a 7.3 TB backup drive queued behind it. Two installs died there, over two
# different transports, which is why neither the share nor the network was ever
# the variable.

SELECTOR = (
    'for disk in $(lsblk -dno NAME,TRAN | awk \'$2 == "usb" { print $1 }\'); do '
    'lsblk -no FSTYPE "/dev/$disk" | grep -qi ntfs || continue; '
    'echo "$disk"; done'
)


def _fake_lsblk(tmp_path):
    """The hub's real layout: NTFS data drives, ISO boot stick, NVMe target."""
    d = tmp_path / "bin"
    d.mkdir()
    (d / "lsblk").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-dno" ]; then\n'
        "  printf 'sda usb\nsdb usb\nsdc usb\nnvme0n1 nvme\n'; exit 0\n"
        "fi\n"
        'case "$3" in\n'
        "  /dev/sda) printf '\nntfs\n' ;;\n"
        "  /dev/sdb) printf 'iso9660\niso9660\nvfat\next4\n' ;;\n"
        "  /dev/sdc) printf '\nntfs\n' ;;\n"
        "  *) printf '\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (d / "lsblk").chmod(0o755)
    return d


def test_the_detach_selector_takes_the_data_drives_and_nothing_else(tmp_path):
    # THE SELECTOR IS THE DANGEROUS PART. "Every USB disk" would eat the stick
    # the installer is running from; anything matching the target would eat the
    # install. "USB and carries NTFS" is exact: NVMe is not USB, the boot medium
    # is iso9660/vfat, and only the data drives are NTFS.
    import os

    env = dict(os.environ, PATH=f"{_fake_lsblk(tmp_path)}:{os.environ['PATH']}")
    result = subprocess.run(
        ["sh", "-c", SELECTOR], capture_output=True, text=True, env=env
    )
    picked = result.stdout.split()
    assert picked == ["sda", "sdc"], picked
    assert "sdb" not in picked, "would have detached the installer's own boot medium"
    assert not any(p.startswith("nvme") for p in picked), "would have detached the target"


def test_the_installer_detaches_ntfs_usb_disks_before_curthooks(autoinstall):
    joined = "\n".join(autoinstall["early-commands"])
    assert "ntfs" in joined.lower()
    assert "/sys/block/" in joined and "device/delete" in joined
    # Must be an EARLY command: the scan happens inside curthooks, between
    # curtin writing /target/etc/default/grub and running update-grub, and
    # there is no hook in between.
    assert "device/delete" not in "\n".join(
        c for c in autoinstall["late-commands"] if isinstance(c, str)
    )


def test_the_installed_system_has_os_prober_disabled_permanently(autoinstall):
    # WITHOUT THIS THE TRAP IS PERMANENT, not install-time: update-grub runs on
    # every kernel update, the drives ARE attached then, and each update would
    # hang the same way -- unattended, unwatched, and silent.
    joined = "\n".join(c for c in autoinstall["late-commands"] if isinstance(c, str))
    assert "GRUB_DISABLE_OS_PROBER=true" in joined
    assert "/target/etc/default/grub" in joined

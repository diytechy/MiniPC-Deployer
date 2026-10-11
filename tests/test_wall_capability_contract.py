"""SR-017: the wall capability contract tracks the panel's own capabilities.json.

The fixture under tests/fixtures/wall-camera-opt is the OTHER side of the
contract - OfficeWallNaglight's own manifest and site inventory at CameraOpt
5008311 (see its SOURCE.txt). Nothing in this file invents the values it checks
against: the script's REQUIRED / ALLOWED_SITE_FILES are compared with what the
panel says it ships, and each mutation below must be REJECTED by the script.
"""

import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/wall-camera-opt"
REVISION = "50083113dc1a9d6f9763a8b07f5d3bf53f5e19ec"

spec = importlib.util.spec_from_file_location(
    "wall_capability_contract", ROOT / "scripts/assert_wall_capabilities.py"
)
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)

PANEL_MANIFEST = json.loads((FIXTURE / "capabilities.json").read_text(encoding="utf-8"))
PANEL_SITE_FILES = (FIXTURE / "site-files.txt").read_text(encoding="ascii").split()

ROOTS = {"shell": "app", "site": "site", "gateway": "access"}
PREFIX = {"shell": "app/runtime/resources/app/", "site": "site/", "gateway": "access/"}
PAYLOAD = {"shell": "app", "site": "site", "gateway": "gateway"}


def build(
    tmp_path,
    kind,
    manifest=None,
    stamp_capabilities=None,
    drop=(),
    extra_files=(),
    site_files=None,
):
    """A release tar for ``kind`` holding exactly what ``manifest`` declares."""
    manifest = PANEL_MANIFEST if manifest is None else manifest
    capabilities = manifest.get("capabilities") if isinstance(manifest, dict) else None
    if not isinstance(capabilities, dict):
        capabilities = {}
    prefix = PREFIX[kind]
    stamp = {
        "source": {"revision": REVISION, "dirty": False},
        "capabilities": sorted(capabilities) if stamp_capabilities is None else stamp_capabilities,
    }
    files = {
        ROOTS[kind] + "/build-info.json": json.dumps(stamp),
        prefix + "capabilities.json": json.dumps(manifest),
    }
    for declaration in capabilities.values():
        if isinstance(declaration, dict):
            for name in declaration.get(PAYLOAD[kind]) or []:
                if isinstance(name, str) and ".." not in name.split("/") and not name.startswith("/"):
                    files[prefix + name] = "fixture"
    if kind == "site":
        # The panel's real site tree, plus whatever the script has admitted
        # since the fixture was taken (a later panel change adds a line to
        # ALLOWED_SITE_FILES and must not also have to touch the fixture).
        for name in set(PANEL_SITE_FILES if site_files is None else site_files) | (
            contract.ALLOWED_SITE_FILES if site_files is None else set()
        ):
            files.setdefault("site/" + name, "fixture")
    for name in extra_files:
        files[name] = "fixture"
    for name in drop:
        files.pop(name)
    path = tmp_path / (
        "officewall-%s-0.1.0-g%s%s.tar.gz"
        % (kind, REVISION[:7], "-linux-x64" if kind == "shell" else "")
    )
    with tarfile.open(path, "w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


def mutated(edit):
    manifest = copy.deepcopy(PANEL_MANIFEST)
    edit(manifest["capabilities"])
    return manifest


def reject(tmp_path, kind, match, **kwargs):
    with pytest.raises(ValueError, match=match):
        contract.inspect(build(tmp_path, kind, **kwargs), kind)


# --- TC-1146: the real panel line is accepted ------------------------------


@pytest.mark.parametrize("kind", ["shell", "site", "gateway"])
def test_current_camera_opt_manifest_and_site_tree_are_accepted(tmp_path, kind):
    assert contract.inspect(build(tmp_path, kind), kind) == REVISION


def test_cli_accepts_the_three_camera_opt_payloads_together(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/assert_wall_capabilities.py"),
            "--shell", str(build(tmp_path, "shell")),
            "--site", str(build(tmp_path, "site")),
            "--gateway", str(build(tmp_path, "gateway")),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "PASS wall artifact" in result.stdout


def test_every_public_site_file_the_panel_ships_is_in_the_inventory():
    # The 27-file regression: the inventory is the exposure list, so it must
    # name every file the panel's own build puts in the document root.
    assert len(PANEL_SITE_FILES) == len(set(PANEL_SITE_FILES)) == 77
    assert sorted(set(PANEL_SITE_FILES) - contract.ALLOWED_SITE_FILES) == []


@pytest.mark.parametrize(
    "capability",
    [
        "audio-switch-v1", "bus-pcm-bridge-v1", "local-capabilities-v1",
        "locked-gauges-v1", "mutation-identity-v1", "projectm-trial-v1",
        "tracker-event-log-v1",
    ],
)
def test_the_seven_capabilities_the_old_contract_missed_are_now_floor(capability):
    assert capability in contract.REQUIRED
    assert capability in PANEL_MANIFEST["capabilities"]


# --- TC-1147: the floor still bites ----------------------------------------


@pytest.mark.parametrize("capability", sorted(PANEL_MANIFEST["capabilities"]))
def test_dropping_any_floor_capability_is_rejected(tmp_path, capability):
    if capability not in contract.REQUIRED:
        pytest.skip("not in the floor: a panel-only capability may be removed")
    manifest = mutated(lambda caps: caps.pop(capability))
    reject(tmp_path, "site", "declaration mismatch", manifest=manifest)


@pytest.mark.parametrize("payload", ["app", "site", "gateway"])
def test_a_floor_capability_that_loses_a_file_is_rejected(tmp_path, payload):
    kind = {"app": "shell", "site": "site", "gateway": "gateway"}[payload]
    victim = next(
        name for name, floor in contract.REQUIRED.items() if floor[payload]
    )
    lost = contract.REQUIRED[victim][payload][-1]
    manifest = mutated(lambda caps: caps[victim][payload].remove(lost))
    reject(tmp_path, kind, "declaration mismatch", manifest=manifest)


def test_the_ring_rail_brand_marks_cannot_be_dropped_silently(tmp_path):
    marks = ["vendor/brand/Grok_Logomark_Light.svg", "vendor/brand/claude-favicon.svg"]
    for payload, kind in (("app", "shell"), ("site", "site")):
        manifest = mutated(
            lambda caps: [caps["progress-gauges-v1"][payload].remove(m) for m in marks]
        )
        reject(tmp_path, kind, "progress-gauges-v1", manifest=manifest)


@pytest.mark.parametrize(
    "manifest",
    [
        {"schemaVersion": 1, "capabilities": PANEL_MANIFEST["capabilities"]},
        {"schemaVersion": 2, "capabilities": PANEL_MANIFEST["capabilities"], "x": 1},
        {"schemaVersion": 2, "capabilities": []},
        {"schemaVersion": 2, "capabilities": {"local-visualizer-v1": {"app": [], "site": [], "gateway": []}}},
        ["not", "a", "manifest"],
    ],
)
def test_wrong_schema_or_a_gutted_manifest_is_rejected(tmp_path, manifest):
    reject(tmp_path, "site", "declaration mismatch", manifest=manifest)


def test_build_info_capability_list_must_match_the_manifest(tmp_path):
    names = sorted(PANEL_MANIFEST["capabilities"])
    reject(
        tmp_path, "shell", "build-info capability list mismatch",
        stamp_capabilities=names[:-1],
    )


# --- TC-1148: the panel may grow past the floor without an edit here -------


def test_a_new_capability_with_its_files_present_is_accepted_and_noted(tmp_path):
    manifest = mutated(
        lambda caps: caps.update(
            {"six-rings-v1": {"app": ["vendor/brand/new.svg"], "site": ["vendor/brand/new.svg"], "gateway": []}}
        )
    )
    notes = []
    path = build(
        tmp_path, "shell", manifest=manifest
    )
    assert contract.inspect(path, "shell", notes) == REVISION
    assert any("six-rings-v1" in note for note in notes)


def test_a_floor_capability_that_gains_a_file_is_accepted_and_noted(tmp_path):
    manifest = mutated(lambda caps: caps["progress-gauges-v1"]["app"].append("vendor/brand/new.svg"))
    notes = []
    assert contract.inspect(build(tmp_path, "shell", manifest=manifest), "shell", notes) == REVISION
    assert any("progress-gauges-v1/app" in note and "new.svg" in note for note in notes)


def test_cli_prints_the_growth_as_a_note_not_a_failure(tmp_path):
    manifest = mutated(lambda caps: caps["progress-gauges-v1"]["app"].append("vendor/brand/new.svg"))
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/assert_wall_capabilities.py"),
         "--shell", str(build(tmp_path, "shell", manifest=manifest))],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "NOTE progress-gauges-v1/app grew past the floor" in result.stdout
    assert result.stdout.rstrip().endswith("source coherence")


def test_a_declared_file_missing_from_the_archive_is_rejected_even_beyond_the_floor(tmp_path):
    manifest = mutated(
        lambda caps: caps.update({"six-rings-v1": {"app": ["vendor/brand/new.svg"], "site": [], "gateway": []}})
    )
    reject(
        tmp_path, "shell", "missing capability file: .*new.svg",
        manifest=manifest, drop=[PREFIX["shell"] + "vendor/brand/new.svg"],
    )


def test_a_floor_file_missing_from_the_archive_is_rejected(tmp_path):
    victim = PREFIX["gateway"] + "gateway/server.mjs"
    reject(tmp_path, "gateway", "missing capability file: .*server.mjs", drop=[victim])


@pytest.mark.parametrize(
    "bad",
    [
        {"app": [], "site": []},
        {"app": [], "site": [], "gateway": [], "extra": []},
        {"app": "js/main.js", "site": [], "gateway": []},
        {"app": ["../escape"], "site": [], "gateway": []},
        {"app": ["/abs"], "site": [], "gateway": []},
        {"app": ["a\\b"], "site": [], "gateway": []},
        {"app": ["js/a.js", "js/a.js"], "site": [], "gateway": []},
        {"app": [3], "site": [], "gateway": []},
        {"app": [["js/a.js"]], "site": [], "gateway": []},
    ],
)
def test_a_malformed_extra_capability_is_rejected(tmp_path, bad):
    manifest = mutated(lambda caps: caps.update({"extra-v1": bad}))
    with pytest.raises(ValueError, match="malformed payload declaration"):
        contract.inspect(build(tmp_path, "shell", manifest=manifest), "shell")


# --- TC-1149: the public-site inventory stays exact ------------------------


@pytest.mark.parametrize(
    "stray", ["debug.txt", "js/local-debug.js", "vendor/brand/unreviewed.svg", "vendor/icons/new.svg"]
)
def test_a_site_file_not_in_the_inventory_is_rejected(tmp_path, stray):
    assert stray not in contract.ALLOWED_SITE_FILES
    reject(tmp_path, "site", "unexpected file in public site: .*" + stray.split("/")[-1],
           extra_files=["site/" + stray])


def test_every_unexpected_site_file_is_named_not_just_the_first(tmp_path):
    with pytest.raises(ValueError) as error:
        contract.inspect(
            build(tmp_path, "site", extra_files=["site/zz-a.js", "site/zz-b.js", "site/zz-c.js"]),
            "site",
        )
    assert all(name in str(error.value) for name in ("zz-a.js", "zz-b.js", "zz-c.js"))


def test_an_inventory_file_missing_from_the_site_is_rejected(tmp_path):
    reject(tmp_path, "site", "missing public site inventory file: .*waves/LICENSE",
           drop=["site/vendor/waves/LICENSE"])


def test_the_inventory_does_not_admit_privileged_trees():
    assert not [
        name for name in contract.ALLOWED_SITE_FILES
        if name.startswith(("electron/", "gateway/", "sensors/", "touchfilter/", "doorstream/"))
    ]

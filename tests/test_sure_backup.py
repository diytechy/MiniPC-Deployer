"""Drive the logical-dump shell boundary with a fake Docker CLI.

Verifies SR-065, LLR-1045; TC-1105..TC-1110. No daemon, image or ledger.
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.full, pytest.mark.smoke]


@pytest.mark.parametrize("mode", ["good", "failed", "empty", "invalid", "toc-failed", "plan", "bad-name"])
def test_logical_dump_boundary_sr065(tmp_path, mode):
    """A failed or empty dump cannot replace the last good file; plan never execs."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not installed")
    # An executable script on POSIX and a shell function on Windows use the
    # same fake protocol, so neither platform can accidentally invoke Docker.
    common = (ROOT / "stack/backup/common.sh").as_posix()
    destination = (tmp_path / "ledger.dump").as_posix()
    (tmp_path / "ledger.dump").write_bytes(b"last-good")
    script = r'''
set -euo pipefail
. "$COMMON"
docker() {
    if [ "$1" = exec ]; then
        if [ "${2:-}" = -i ] && [ "${4:-}" = pg_restore ]; then
            cat >/dev/null
            [ "$MODE" != toc-failed ]
            return
        fi
        case "$MODE" in
            good|toc-failed) printf 'PGDMPsynthetic-fixture' ;;
            failed) printf 'partial'; return 1 ;;
            empty) return 0 ;;
            invalid) printf 'not-a-custom-dump' ;;
            plan) echo 'plan executed Docker' >&2; return 9 ;;
        esac
    else
        echo 'unexpected Docker call' >&2; return 9
    fi
}
PLAN_ONLY=0
[ "$MODE" != plan ] || PLAN_ONLY=1
container=sure-postgres
[ "$MODE" != bad-name ] || container=--privileged
postgres_dump "$container" "$DESTINATION"
'''
    result = subprocess.run([bash, "-c", script], env={**os.environ, "COMMON": common, "DESTINATION": destination, "MODE": mode}, capture_output=True, text=True)
    actual = (tmp_path / "ledger.dump").read_bytes()
    if mode == "good":
        assert result.returncode == 0, result.stderr
        assert actual == b"PGDMPsynthetic-fixture"
    elif mode == "plan":
        assert result.returncode == 0, result.stderr
        assert actual == b"last-good"
    else:
        assert result.returncode == 1, result.stderr
        assert actual == b"last-good"
    assert not (tmp_path / "ledger.dump.partial").exists()

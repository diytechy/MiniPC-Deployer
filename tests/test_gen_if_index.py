"""The generated interface index in docs/interfaces.md (scripts/gen_if_index.py).

The committed-page test reads the registry with the csv module on its own and
looks for each id in the page, rather than asking the generator whether the
generator's output matches itself. The other tests run the generator against
temporary copies, so they never write the real page.
"""
import csv
import importlib.util
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gen_if_index.py"
CSV = ROOT / "docs" / "requirements" / "interfaces.csv"
DOC = ROOT / "docs" / "interfaces.md"


def _module():
    spec = importlib.util.spec_from_file_location("gen_if_index", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GEN = _module()


def _block(text):
    return text.split(GEN.BEGIN, 1)[1].split(GEN.END, 1)[0]


def _write_registry(path, rows):
    header = ["IF-ID", "Direction", "ThisProject", "Counterpart", "Contract",
              "SR-Refs", "Version", "Stability", "Status"]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in header})


def _row(if_id, contract="A contract.", refs="SR-001"):
    return {"IF-ID": if_id, "Direction": "Provides", "ThisProject": "x",
            "Counterpart": "y", "Contract": contract, "SR-Refs": refs,
            "Version": "v1", "Stability": "Experimental", "Status": "Draft"}


@pytest.mark.smoke
def test_committed_page_indexes_every_registry_row_and_is_current():
    with open(CSV, encoding="utf-8", newline="") as handle:
        ids = [row["IF-ID"].strip() for row in csv.DictReader(handle) if row["IF-ID"].strip()]
    block = _block(DOC.read_text(encoding="utf-8"))
    indexed = re.findall(r"^\| \*\*(IF-\d+)\*\* \|", block, flags=re.MULTILINE)
    assert sorted(indexed) == sorted(ids), "regenerate: python scripts/gen_if_index.py"
    assert GEN.main(["--check"]) == 0


def test_check_reports_a_new_registry_row_and_writes_nothing(tmp_path):
    registry, page = tmp_path / "interfaces.csv", tmp_path / "interfaces.md"
    _write_registry(registry, [_row("IF-001")])
    page.write_text("intro\n" + GEN.BEGIN + "\n" + GEN.END + "\nnotes\n", encoding="utf-8")
    assert GEN.main(["--csv", str(registry), "--doc", str(page)]) == 0
    _write_registry(registry, [_row("IF-001"), _row("IF-002")])
    before = page.read_bytes()
    assert GEN.main(["--check", "--csv", str(registry), "--doc", str(page)]) == 1
    assert page.read_bytes() == before


def test_text_outside_the_markers_survives_byte_for_byte(tmp_path):
    registry, page = tmp_path / "interfaces.csv", tmp_path / "interfaces.md"
    _write_registry(registry, [_row("IF-010"), _row("IF-002")])
    head, tail = "# Title\r\n\r\nprose | with a pipe\r\n", "\r\n## Notes\r\nkept\r\n"
    page.write_bytes((head + GEN.BEGIN + "\nstale\n" + GEN.END + tail).encode("utf-8"))
    assert GEN.main(["--csv", str(registry), "--doc", str(page)]) == 0
    text = page.read_bytes().decode("utf-8")
    assert text.startswith(head + GEN.BEGIN) and text.endswith(GEN.END + tail)
    assert "stale" not in text
    assert text.index("IF-002") < text.index("IF-010"), "rows sort by number"


@pytest.mark.parametrize("page_text", [
    "no markers at all\n",
    GEN.END + "\n" + GEN.BEGIN + "\n",
    GEN.BEGIN + "\n" + GEN.END + "\n" + GEN.BEGIN + "\n" + GEN.END + "\n",
])
def test_missing_misordered_or_doubled_markers_fail_without_writing(tmp_path, page_text):
    registry, page = tmp_path / "interfaces.csv", tmp_path / "interfaces.md"
    _write_registry(registry, [_row("IF-001")])
    page.write_text(page_text, encoding="utf-8")
    assert GEN.main(["--csv", str(registry), "--doc", str(page)]) == 2
    assert page.read_text(encoding="utf-8") == page_text


def test_a_cell_cannot_break_the_table_row():
    long_code = "Run `" + "word " * 40 + "` and then more prose after the code span."
    rendered = GEN.render([_row("IF-001", contract="x | y <path>\nnext line", refs="SR-1;SR-2"),
                           _row("IF-002", contract=long_code)])
    rows = rendered.splitlines()
    assert len(rows) == 4, "one header, one rule, one line per registry row"
    assert "x \\| y &lt;path> next line" in rows[2]
    assert "SR-1, SR-2" in rows[2]
    assert rows[3].count("`") % 2 == 0, "a truncated code span must not stay open"
    assert len(GEN.summary(long_code)) <= GEN.SUMMARY_CHARS + 2

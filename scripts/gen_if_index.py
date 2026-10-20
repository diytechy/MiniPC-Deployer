#!/usr/bin/env python3
"""Generate the interface index in docs/interfaces.md from the IF registry CSV.

ONE RESPONSIBILITY: splice one table row per row of
`docs/requirements/interfaces.csv` between the GENERATED IF INDEX markers in
`docs/interfaces.md`, so the page's index cannot drift from the registry.

WHY IT EXISTS. The page calls itself "the thin, human-readable index" over the
CSV, and it was kept by hand. By 2026-10-19 it named 12 of the CSV's 31 rows.
The CSV is the registry (process.md section 8); this block is derived from it
and must never be hand-edited. The curated notes elsewhere on the page stay
hand-written: they explain, the block enumerates.

Same marker-splice contract as the kit's `gen_arch_map.py`: everything between
the markers is regenerated, everything outside them is left byte-for-byte, and
`--check` writes nothing and exits 1 when the block is stale, so the check
harness (`scripts/check.py`, step `if-index`) can gate on it.

Contract:
  Inputs:  --csv  path to the registry (default docs/requirements/interfaces.csv)
           --doc  path to the page holding the marker pair (default docs/interfaces.md)
           --check  compare only; never write
  Outputs: the rewritten page (default mode), or a verdict line (--check)
  Exit:    0 up to date / rewritten; 1 stale under --check;
           2 markers missing, duplicated or out of order, or the CSV has no IF-ID column
Stdlib only, Python 3.8+.
"""
import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "docs" / "requirements" / "interfaces.csv"
DEFAULT_DOC = ROOT / "docs" / "interfaces.md"

BEGIN = "<!-- BEGIN GENERATED IF INDEX -->"
END = "<!-- END GENERATED IF INDEX -->"

# The Contract cell is often a paragraph; the index shows only its opening
# words, cut on a word boundary, so a row stays one readable line. The CSV
# itself is where the full contract is read.
SUMMARY_CHARS = 110

COLUMNS = ("IF", "Direction", "Counterpart", "Status", "Version", "SR-Refs",
           "Contract (opening words; full text in the CSV)")


def id_number(if_id):
    """Numeric part of an `IF-###` id, for ordering; unparseable ids sort last."""
    match = re.fullmatch(r"IF-(\d+)", if_id.strip())
    return int(match.group(1)) if match else sys.maxsize


def cell(text):
    """Make a CSV value safe inside one Markdown table cell.

    Collapses whitespace (a cell must be one line), escapes the pipe that would
    end the cell, and escapes `<` so a placeholder like `<path>` is shown
    rather than swallowed as an HTML tag.
    """
    text = " ".join((text or "").split())
    return text.replace("|", "\\|").replace("<", "&lt;")


def summary(contract):
    """The contract's opening words, at most SUMMARY_CHARS, cut on a space.

    An odd number of backticks would leave a code span open and swallow the
    rest of the row, so a cut that lands inside one backs up to before it.
    """
    text = " ".join((contract or "").split())
    if len(text) <= SUMMARY_CHARS:
        return text
    cut = text[:SUMMARY_CHARS]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    if cut.count("`") % 2:
        cut = cut[:cut.rindex("`")].rstrip()
    return cut.rstrip(" ,;:(") + " …"


def sr_refs(value):
    """Normalise the registry's mixed `,`/`;` separators to `, `."""
    parts = [p.strip() for p in re.split(r"[;,]", value or "") if p.strip()]
    return ", ".join(parts) if parts else "—"


def render(rows):
    """The Markdown table for `rows` (dicts keyed by the CSV header)."""
    lines = [
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "|".join("---" for _ in COLUMNS) + "|",
    ]
    for row in sorted(rows, key=lambda r: (id_number(r["IF-ID"]), r["IF-ID"])):
        lines.append("| " + " | ".join([
            "**" + cell(row["IF-ID"]) + "**",
            cell(row.get("Direction")),
            cell(row.get("Counterpart")),
            cell(row.get("Status")),
            cell(row.get("Version")),
            cell(sr_refs(row.get("SR-Refs"))),
            cell(summary(row.get("Contract"))),
        ]) + " |")
    return "\n".join(lines)


def load_rows(csv_path):
    """Rows of the registry; raises ValueError when it has no IF-ID column."""
    with open(csv_path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if "IF-ID" not in (reader.fieldnames or []):
            raise ValueError("{} has no IF-ID column".format(csv_path))
        return [row for row in reader if (row.get("IF-ID") or "").strip()]


def splice(doc_text, table):
    """`doc_text` with the region between the markers replaced by `table`.

    Raises ValueError unless exactly one BEGIN precedes exactly one END.
    """
    if doc_text.count(BEGIN) != 1 or doc_text.count(END) != 1:
        raise ValueError("expected exactly one {} and one {}".format(BEGIN, END))
    head, rest = doc_text.split(BEGIN)
    if END not in rest:
        raise ValueError("{} comes before {}".format(END, BEGIN))
    _, tail = rest.split(END)
    return head + BEGIN + "\n" + table + "\n" + END + tail


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--doc", type=Path, default=DEFAULT_DOC)
    parser.add_argument("--check", action="store_true",
                        help="do not write; exit 1 if the block is stale")
    args = parser.parse_args(argv)

    try:
        rows = load_rows(args.csv)
        # newline="" keeps the page's own line endings exactly as they are.
        with open(args.doc, encoding="utf-8", newline="") as handle:
            current = handle.read()
        wanted = splice(current, render(rows))
    except ValueError as error:
        print("gen_if_index: ERROR - {}".format(error), file=sys.stderr)
        return 2

    if wanted == current:
        print("gen_if_index: OK - {} interface(s) indexed, block is current.".format(len(rows)))
        return 0
    if args.check:
        print("gen_if_index: STALE - {} does not match {}; run "
              "`python scripts/gen_if_index.py`.".format(args.doc, args.csv), file=sys.stderr)
        return 1
    with open(args.doc, "w", encoding="utf-8", newline="") as handle:
        handle.write(wanted)
    print("gen_if_index: WROTE - {} interface(s) indexed into {}.".format(len(rows), args.doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())

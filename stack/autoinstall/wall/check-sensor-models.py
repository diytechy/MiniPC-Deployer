#!/usr/bin/env python3
"""Verify a local face-model bundle against the reviewed, committed manifest
before it is staged into a panel image payload.

Implements: LLR-901 (Item M, 2026-09-15 Owner ruling — the face model bundle
is a dependency of any camera-related option, not a separate optional input).

Same authenticity pattern as check-wheelhouse-lock.py --expect: the reviewed
manifest committed at stack/autoinstall/wall/sensor-models/manifest.json is
the anchor. A local input directory is accepted only when its own
manifest.json is byte-identical to the reviewed one AND both .onnx files hash
to exactly the digests the manifest names — a directory whose manifest is
merely self-consistent (matches its own files but not the reviewed one) is
refused, the same way substituted wheelhouse media is refused.

Contract:
  Inputs:  --expect PATH  the reviewed, committed manifest.json
           DIRECTORY      a local input holding det_10g.onnx, w600k_r50.onnx
                           and its own manifest.json
  Outputs: none (stdout notes success)
  Raises:  SystemExit(1) with a message on stderr for any mismatch
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

REQUIRED = ("det_10g.onnx", "w600k_r50.onnx")


def sha256_of(path: Path) -> str:
    """Stream-hash one file; never loads it whole (models run ~17-170 MB)."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def fail(message: str) -> None:
    print(f"[check-sensor-models] {message}", file=sys.stderr)
    sys.exit(1)


def _validate_manifest_shape(manifest, source: str) -> None:
    if not isinstance(manifest, dict) or set(manifest) != set(REQUIRED):
        fail(f"{source} must name exactly {REQUIRED}")
    for name, digest in manifest.items():
        if not isinstance(digest, str) or len(digest) != 64 or any(
            c not in "0123456789abcdef" for c in digest.lower()
        ):
            fail(f"{source} has an invalid sha256 digest for {name}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect", required=True, type=Path,
                         help="the reviewed, committed manifest.json")
    parser.add_argument("directory", type=Path,
                         help="local input directory holding the bundle")
    args = parser.parse_args(argv)

    if not args.expect.is_file():
        fail(f"reviewed manifest {args.expect} does not exist")
    expected_text = args.expect.read_text(encoding="utf-8")
    try:
        expected = json.loads(expected_text)
    except json.JSONDecodeError as exc:
        fail(f"reviewed manifest {args.expect} is not valid JSON: {exc}")
    _validate_manifest_shape(expected, f"reviewed manifest {args.expect}")

    if not args.directory.is_dir():
        fail(f"local model bundle directory {args.directory} does not exist")

    local_manifest_path = args.directory / "manifest.json"
    if not local_manifest_path.is_file():
        fail(f"{local_manifest_path} does not exist")
    local_text = local_manifest_path.read_text(encoding="utf-8")
    if local_text != expected_text:
        fail(
            f"{local_manifest_path} is not byte-identical to the reviewed manifest "
            f"{args.expect}. A directory whose manifest is merely self-consistent "
            "with its own files proves nothing about authenticity and is refused."
        )

    for name, digest in expected.items():
        model_path = args.directory / name
        if model_path.is_symlink() or not model_path.is_file():
            fail(f"{model_path} is missing from the local model bundle")
        actual = sha256_of(model_path)
        if actual != digest.lower():
            fail(
                f"{model_path} sha256 {actual} does not match the reviewed "
                f"digest {digest}"
            )

    print(f"[check-sensor-models] verified {', '.join(REQUIRED)} against {args.expect}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

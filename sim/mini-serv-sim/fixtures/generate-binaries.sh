#!/usr/bin/env bash
# generate-binaries.sh — (re)generate the BINARY fixture files for mini-serv-sim
# DETERMINISTICALLY (fixed content + fixed zip timestamps), so the committed
# fixtures never churn. Text fixtures (server.properties, *.yml, *.json, *.ini)
# are committed as-is; this only makes the binaries that can't be authored as
# text: the plugin jars (valid zips carrying a parseable plugin.yml — WI-10.16
# parses these), a realistic paper jar, world blobs, and a Satisfactory save.
#
# All content is FICTIONAL. Run from anywhere: `bash generate-binaries.sh`.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MC="$HERE/minecraft"; SAT="$HERE/satisfactory"
mkdir -p "$MC/plugins" "$MC/world/region" "$MC/world/data" "$SAT/SaveGames/SimSaveSlot"

python3 - "$MC" "$SAT" <<'PY'
import os, sys, zipfile, random
MC, SAT = sys.argv[1], sys.argv[2]
FIXED = (2026, 7, 4, 0, 0, 0)   # fixed zip timestamp -> stable bytes

def jar(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            zi = zipfile.ZipInfo(name, date_time=FIXED)
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)

def plugin_yml(name, ver, main, desc):
    return (f"name: {name}\nversion: {ver}\nmain: {main}\n"
            f"api-version: '1.20'\nauthor: SimDev\ndescription: {desc}\n"
            f"commands:\n  {name.lower()}:\n    description: {desc}\n")

# 3 fictional plugin jars — valid zips with a parseable plugin.yml.
jar(f"{MC}/plugins/SimGreeter.jar", {
    "plugin.yml": plugin_yml("SimGreeter", "1.2.0", "sim.greeter.SimGreeter", "Greets players."),
    "sim/greeter/SimGreeter.class": b"\xca\xfe\xba\xbe" + b"SIMCLASS-greeter\x00" * 8,
})
jar(f"{MC}/plugins/SimEconomy.jar", {
    "plugin.yml": plugin_yml("SimEconomy", "0.9.3", "sim.econ.SimEconomy", "Fictional economy."),
    "sim/econ/SimEconomy.class": b"\xca\xfe\xba\xbe" + b"SIMCLASS-econ\x00" * 8,
})
jar(f"{MC}/plugins/SimBackupHelper.jar", {
    "plugin.yml": plugin_yml("SimBackupHelper", "2.0.1", "sim.bak.SimBackupHelper", "Fictional backup helper."),
    "sim/bak/SimBackupHelper.class": b"\xca\xfe\xba\xbe" + b"SIMCLASS-bak\x00" * 8,
})

# Realistic paper server jar (name pattern paper-<mcver>-<build>.jar).
jar(f"{MC}/paper-1.20.4-435.jar", {
    "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\nMain-Class: io.papermc.paperclip.Main\n",
    "version.json": '{"id":"1.20.4","name":"1.20.4","world_version":3700}\n',
    "io/papermc/paperclip/Main.class": b"\xca\xfe\xba\xbe" + b"PAPERCLIP\x00" * 16,
})

def blob(path, n, seed):
    r = random.Random(seed)                       # deterministic high-entropy bytes
    with open(path, "wb") as f:
        f.write(bytes(r.getrandbits(8) for _ in range(n)))

# World data: high-entropy (already-compressed-like) region + small level.dat.
blob(f"{MC}/world/region/r.0.0.mca", 12288, 1001)
blob(f"{MC}/world/level.dat", 1024, 1002)
blob(f"{MC}/world/data/villages.dat", 512, 1003)
open(f"{MC}/world/session.lock", "wb").write(b"\x00" * 16)

# Satisfactory save — one incompressible .sav blob (saves are already compressed).
blob(f"{SAT}/SaveGames/SimSaveSlot/SimFactory_autosave_0.sav", 16384, 2001)
open(f"{SAT}/SaveGames/SimSaveSlot/SimFactory.sav.meta", "w").write("session=SimFactory\nbuild=999\n")
print("fixtures generated")
PY
echo "OK: binary fixtures written under $MC and $SAT"

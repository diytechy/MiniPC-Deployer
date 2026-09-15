#!/usr/bin/env bash
# aec.test.sh -- the echo canceller, as far as a machine with no panel can go.
#
# THREE THINGS, AND THE THIRD IS THE ONE WORTH HAVING:
#
#   1. the POLICY tests (aec/tests/aec_policy_test.c). Pure C, no ALSA, no
#      SpeexDSP: the far-end gate, double talk, the session baseline, the
#      divergence reset and its rate limit, the give-up bound, the failover
#      floor, the drift admission rule and its direction, item L's
#      normalization and the status block's semantics.
#   2. the DAEMON builds, with -Wall -Wextra -Wconversion and no warnings.
#   3. a BOUNDED OFFLINE REPLAY through the real engine. Synthetic audio with a
#      known echo path is generated here and run through `--replay`, which
#      shares every line of the live path except the ALSA calls. It proves the
#      framing, the pre-delay ring, the resampler handoff and the health
#      accounting actually work on samples -- which no unit test can.
#
# WHAT IT CANNOT PROVE, and what is therefore marked pending for the Owner:
# that the filter cancels THIS ROOM's echo, the amplifier-knob re-convergence,
# double talk with a person speaking, and anything about the two cards' real
# clocks. Those need the panel and a person in it.
#
# HERMETIC, AND IT SKIPS RATHER THAN FAILS. A dev box with no compiler or no
# libspeexdsp is not a broken panel; it is a machine that cannot answer this
# question, and saying so is more useful than a red line.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AEC="$(cd "$HERE/../aec" && pwd)"
PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$1"; }

CC="${CC:-cc}"
command -v "$CC" >/dev/null 2>&1 || { echo "SKIP: no C compiler ($CC) on PATH"; exit 0; }
command -v python3 >/dev/null 2>&1 || { echo "SKIP: python3 is required to make the fixtures"; exit 0; }
printf '#include <speex/speex_echo.h>\nint main(void){return 0;}\n' > /tmp/aec-probe.c
"$CC" -o /tmp/aec-probe /tmp/aec-probe.c -lspeexdsp >/dev/null 2>&1 \
    || { echo "SKIP: libspeexdsp-dev is not installed"; rm -f /tmp/aec-probe.c; exit 0; }
rm -f /tmp/aec-probe.c /tmp/aec-probe

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 1 -- the policy tests.
if (cd "$AEC" && make check > "$TMP/policy.log" 2>&1); then
    pass "A1 the policy core's assertions all hold"
else
    fail "A1 the policy core's assertions do not hold"
    grep -E '^FAIL' "$TMP/policy.log" | head -20
fi
# Every assertion in that binary, counted here so a suite that silently stopped
# asserting cannot pass by having nothing to say.
count="$(grep -cE '^PASS' "$TMP/policy.log" || true)"
[ "${count:-0}" -ge 100 ] \
    && pass "A1 and there are $count of them" \
    || fail "A1 the policy suite ran only ${count:-0} assertions"

# 2 -- the daemon builds clean. Warnings are errors here on purpose: this is a
# realtime audio path running as root-adjacent with an empty capability set, and
# an implicit conversion in the block arithmetic is exactly the class of bug
# that would show up as a quiet loss of ERLE nobody could explain.
if (cd "$AEC" && make clean >/dev/null 2>&1 && make offline > "$TMP/build.log" 2>&1); then
    pass "A2 the replay build compiles"
else
    fail "A2 the replay build does not compile"
    tail -20 "$TMP/build.log"
fi
if grep -q 'warning:' "$TMP/build.log"; then
    fail "A2 and it compiles with warnings"
    grep 'warning:' "$TMP/build.log" | head -10
else
    pass "A2 with no warnings under -Wall -Wextra -Wconversion"
fi

# 3 -- the bounded offline replay.
python3 - "$TMP" <<'PYEOF'
import math, random, struct, sys, os
# A known echo path: 50 ms of delay and 12 dB of loss, which is the geometry
# the spike measured on the panel (54.9 ms bulk delay) rounded to the
# conservative default pre-delay the daemon uses with no profile. The far end is
# two tones plus noise, because a single tone is the one signal an adaptive
# filter finds easiest and would flatter the result.
rate, seconds = 48000, 20
delay = int(0.050 * rate)
random.seed(7)
ring = [0.0] * delay
reference, near = [], []
for i in range(rate * seconds):
    x = (0.30 * math.sin(2 * math.pi * 440 * i / rate)
         + 0.10 * math.sin(2 * math.pi * 997 * i / rate)
         + 0.02 * random.uniform(-1, 1))
    reference.append(x)
    echo = 0.25 * ring[i % delay]
    ring[i % delay] = x
    near.append(echo + 0.002 * random.uniform(-1, 1))

def write(path, samples):
    with open(path, 'wb') as handle:
        handle.write(b''.join(struct.pack('<h', max(-32768, min(32767, int(v * 32767))))
                              for v in samples))

write(os.path.join(sys.argv[1], 'ref.raw'), reference)
write(os.path.join(sys.argv[1], 'near.raw'), near)
PYEOF

"$AEC/wall-audio-aec-offline" --profile /nonexistent --status "$TMP/status.json" \
    --replay "$TMP/ref.raw" "$TMP/near.raw" > "$TMP/out.raw" 2> "$TMP/replay.log"
replayed="$?"
[ "$replayed" -eq 0 ] && pass "A3 the replay runs to completion" || fail "A3 the replay failed"

# It cancelled something. The floor here is deliberately LOW (15 dB against a
# synthetic path the spike measured 27 dB on) because this is a regression
# guard, not an acceptance test: it catches a transposed resampler, a broken
# pre-delay ring or a reference that never arrives, and it must not start
# failing because a library version adapts a little differently.
erle="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['erle_db_session'])" "$TMP/status.json" 2>/dev/null)"
python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) >= 15.0 else 1)" "${erle:-0}" \
    && pass "A3 and cancels the synthetic echo (${erle} dB ERLE)" \
    || fail "A3 the synthetic echo was NOT cancelled (${erle:-none} dB ERLE)"

# The reference never starved: a nonzero count means the ring is too small or
# the block accounting consumed more than it was handed.
starved="$(grep -o 'reference-starved blocks [0-9]*' "$TMP/replay.log" | tail -1 | awk '{print $NF}')"
[ "${starved:-1}" = "0" ] \
    && pass "A3 and the reference ring never starved" \
    || fail "A3 the reference ring starved ${starved:-?} blocks"

# The output is the same length as the input: the engine is frame-accurate, and
# a resampler that produced a different number of frames than it consumed would
# show up here before it showed up as lost alignment on the panel.
in_bytes="$(wc -c < "$TMP/near.raw")"
out_bytes="$(wc -c < "$TMP/out.raw")"
[ "$in_bytes" = "$out_bytes" ] \
    && pass "A3 and one microphone frame in is one frame out" \
    || fail "A3 the engine is not frame-accurate (in $in_bytes, out $out_bytes)"

# The status block it wrote is the contract's shape.
python3 - "$TMP/status.json" <<'PYEOF'
import json, sys
status = json.load(open(sys.argv[1]))
required = {"schema", "state", "erle_db_1min", "erle_db_session", "profile",
            "predelay_ms", "filter_length_ms", "mic_peak_dbfs_1min",
            "far_end_active_frames_1min", "double_talk_frames_1min", "adaptation",
            "resets_this_session", "xruns_this_session", "drift_ppm",
            "drift_correction", "microphone", "updated_utc"}
missing = required - set(status)
mic = status.get("microphone", {})
mic_required = {"level", "source", "state", "valid", "reference_dbfs",
                "observed_monotonic_ms"}
missing |= {"microphone." + k for k in (mic_required - set(mic))}
if missing:
    print("MISSING " + ",".join(sorted(missing)))
    sys.exit(1)
if mic["source"] != "aec_post_filter":
    print("SOURCE " + str(mic["source"]))
    sys.exit(1)
if not 0.0 <= mic["level"] <= 1.0:
    print("LEVEL " + str(mic["level"]))
    sys.exit(1)
sys.exit(0)
PYEOF
[ "$?" -eq 0 ] \
    && pass "A3 and the status block is the contract's shape" \
    || fail "A3 the status block does not match the telemetry contract"

printf '\n%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0

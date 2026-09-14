/* aec_policy_test.c -- the canceller's DECISIONS, asserted with no sound card.
 *
 * WHAT CAN HONESTLY BE TESTED OFFLINE, and this file is exactly that set:
 * framing and normalization arithmetic, the resampler handoff's admission rule
 * and direction, the status block's semantics, and the degrade/reset logic --
 * far-end gating, double talk, the session baseline, the divergence reset, its
 * rate limit, the give-up bound and the absolute failover floor.
 *
 * WHAT CANNOT, and is therefore marked pending in the report rather than
 * asserted here: that SpeexDSP actually cancels this room's echo, the real
 * amplifier-knob re-convergence, double talk with a person in the room, and
 * anything about the two cards' real clocks. Those need the panel.
 *
 * Implements: SR-028, LLR-016
 */

#include "../wall_aec_policy.h"
#include "../wall_aec_profile.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int passed = 0, failed = 0;

static void ok(int condition, const char *name)
{
    if (condition) { passed++; printf("PASS  %s\n", name); }
    else { failed++; printf("FAIL  %s\n", name); }
}

static void near(double actual, double expected, double tolerance, const char *name)
{
    double difference = actual - expected;
    if (difference < 0) difference = -difference;
    if (difference <= tolerance) { passed++; printf("PASS  %s\n", name); }
    else { failed++; printf("FAIL  %s (want %.4f, got %.4f)\n", name, expected, actual); }
}

/* A profile with a known floor, so the tests do not depend on the default. */
static aec_profile fixture_profile(void)
{
    aec_profile profile;
    aec_profile_defaults(&profile);
    profile.present = true;
    profile.predelay_ms = 50.0;
    profile.bulk_delay_ms = 54.9;
    profile.filter_length_ms = 128.0;
    profile.erle_floor_db = 12.0;
    profile.erle_at_retune_db = 27.0;
    snprintf(profile.name, sizeof(profile.name), "%s", "aec-profile-test.json");
    return profile;
}

/* Feed `count` blocks with a fixed geometry. `erle_db` is how much better the
 * residual is than the microphone; the far end is active and the near end is
 * quiet, so every one of these is a qualifying frame. */
static int64_t feed_step(aec_policy *policy, int count, double erle_db, int64_t start_ms,
                         aec_action *saw, int64_t step_ms)
{
    int64_t now = start_ms;
    for (int i = 0; i < count; i++) {
        aec_block block = {
            .now_ms = now,
            .far_dbfs = -20.0,
            .mic_dbfs = -30.0,
            .residual_dbfs = -30.0 - erle_db,
            .mic_peak_dbfs = -25.0,
            .input_muted = false,
            .tap_present = true,
        };
        aec_decision decision = aec_policy_block(policy, &block);
        now += step_ms;
        if (decision.action != AEC_ACTION_NONE) {
            /* STOPS AT THE FIRST ACTION. Running on past it would let the very
             * next frames re-warm the replacement filter, and the assertions
             * that follow -- the baseline is gone, the grace has restarted --
             * would be about a filter two states later. */
            if (saw) *saw = decision.action;
            return now;
        }
    }
    return now;
}

/* The ordinary step: about one block. The exact figure only matters where the
 * reset cooldown does, and that test sets its own. */
static int64_t feed(aec_policy *policy, int count, double erle_db, int64_t start_ms,
                    aec_action *saw)
{
    return feed_step(policy, count, erle_db, start_ms, saw, 6);
}

/* ── framing and normalization ───────────────────────────────────────────── */

static void test_normalization(void)
{
    near(aec_dbfs(1.0), 0.0, 1e-9, "full scale is 0 dBFS");
    near(aec_dbfs(0.5), -6.0206, 1e-3, "half scale is -6 dBFS");
    ok(aec_dbfs(0.0) == AEC_LEVEL_FLOOR_DBFS, "digital silence is the floor, never -inf");
    ok(aec_dbfs(-1.0) == AEC_LEVEL_FLOOR_DBFS, "and a nonsense amplitude cannot escape it");

    /* Item L, exactly as the telemetry contract documents it: linear in
     * DECIBELS between the floor and the reference, reaching 1.0 at the
     * reference. Amplitude-linear was the obvious wrong answer -- speech at
     * -30 dBFS is 0.03 in amplitude and would never move the ring. */
    near(aec_level_from_dbfs(AEC_LEVEL_FLOOR_DBFS), 0.0, 1e-9, "the floor is 0");
    near(aec_level_from_dbfs(AEC_LEVEL_REFERENCE_DBFS), 1.0, 1e-9, "the reference is 1");
    near(aec_level_from_dbfs(-39.0), 0.5, 1e-9, "and the midpoint is halfway");
    near(aec_level_from_dbfs(0.0), 1.0, 1e-9, "above the reference is CLAMPED, not >1");
    near(aec_level_from_dbfs(-120.0), 0.0, 1e-9, "and below the floor is clamped too");

    ok(AEC_FRAME_SIZE == 256 && AEC_RATE_HZ == 48000, "the spike's framing is unchanged");
}

/* ── the profile ─────────────────────────────────────────────────────────── */

static void test_profile(void)
{
    aec_profile profile;
    aec_profile_defaults(&profile);
    ok(profile.present == false, "the default profile says it is not one");
    ok(profile.filter_length_ms == AEC_DEFAULT_FILTER_MS, "and is the conservative filter");

    /* THE SPIKE'S MOST IMPORTANT LINE. Overshooting the bulk delay costs 6 dB
     * of ERLE that adaptation cannot recover; undershooting costs a little
     * filter length. A profile that overshoots is CORRECTED, not obeyed. */
    profile = fixture_profile();
    profile.predelay_ms = 54.0;      /* only 0.9 ms below the bulk delay */
    ok(!aec_profile_clamp(&profile), "an overshooting pre-delay is reported as corrected");
    near(profile.predelay_ms, 54.9 - AEC_PREDELAY_MARGIN_MS, 1e-9,
         "and is held the margin below the bulk delay");

    profile = fixture_profile();
    profile.filter_length_ms = 4000.0;
    ok(!aec_profile_clamp(&profile), "an absurd filter length is corrected");
    near(profile.filter_length_ms, AEC_FILTER_MAX_MS, 1e-9, "to the ceiling");

    profile = fixture_profile();
    profile.erle_floor_db = -5.0;
    ok(!aec_profile_clamp(&profile), "a negative floor is corrected");
    near(profile.erle_floor_db, AEC_DEFAULT_ERLE_FLOOR_DB, 1e-9, "to the default");

    profile = fixture_profile();
    ok(aec_profile_clamp(&profile), "a sane profile is left exactly alone");

    /* A missing file is not an error: the daemon runs on the defaults. */
    aec_profile loaded;
    char note[256];
    ok(!aec_profile_load("/nonexistent/aec-profile.json", &loaded, note, sizeof(note)),
       "a missing profile is reported as absent");
    ok(loaded.present == false && loaded.filter_length_ms == AEC_DEFAULT_FILTER_MS,
       "and leaves the conservative defaults standing");
    ok(note[0] != '\0', "out loud, not in silence");

    /* A real one, written the way the retune tool writes it. */
    const char *path = "aec-profile-fixture.json";
    FILE *handle = fopen(path, "w");
    fputs("{\n \"schema\": 1,\n \"room\": \"wall\",\n \"bulk_delay_ms\": 54.9,\n"
          " \"predelay_ms\": 50.0,\n \"filter_length_ms\": 128,\n"
          " \"rir\": { \"rt60_t20_s\": null },\n"
          " \"acceptance\": { \"erle_at_retune_db\": 27.0, \"erle_floor_db\": 12.0 }\n}\n", handle);
    fclose(handle);
    ok(aec_profile_load(path, &loaded, note, sizeof(note)), "a real profile loads");
    near(loaded.predelay_ms, 50.0, 1e-9, "pre-delay");
    near(loaded.filter_length_ms, 128.0, 1e-9, "filter length");
    near(loaded.erle_floor_db, 12.0, 1e-9, "the floor, from the nested acceptance block");
    near(loaded.erle_at_retune_db, 27.0, 1e-9, "and the retune figure beside it");
    ok(loaded.present, "and it says it is a profile");
    remove(path);

    handle = fopen(path, "w");
    fputs("{ \"schema\": 2, \"predelay_ms\": 1.0 }\n", handle);
    fclose(handle);
    ok(!aec_profile_load(path, &loaded, note, sizeof(note)), "a future schema is refused");
    near(loaded.predelay_ms, AEC_DEFAULT_PREDELAY_MS, 1e-9, "and nothing from it is used");
    remove(path);
}

/* ── far-end gating and double talk ──────────────────────────────────────── */

static void test_gating(void)
{
    aec_profile profile = fixture_profile();
    aec_policy policy;
    aec_policy_init(&policy, &profile, 0);

    /* A SILENT TAP DEFINES NO ERLE. Measuring one there is a ratio of two noise
     * floors, and it is what would make the panel report `degraded` all night. */
    aec_block quiet = { .now_ms = 0, .far_dbfs = -70.0, .mic_dbfs = -40.0,
                        .residual_dbfs = -40.0, .mic_peak_dbfs = -35.0,
                        .input_muted = false, .tap_present = true };
    aec_policy_block(&policy, &quiet);
    ok(policy.erle_n_session == 0, "a silent tap contributes no ERLE sample");
    ok(policy.state == AEC_STATE_IDLE, "and the state is idle, not degraded");

    /* NO TAP AT ALL is the same case and falls out of the tap, not out of mode
     * logic: the switch has left Speaker. */
    quiet.tap_present = false;
    quiet.far_dbfs = -20.0;
    aec_policy_block(&policy, &quiet);
    ok(policy.state == AEC_STATE_IDLE, "an absent tap is idle too");
    ok(policy.erle_n_session == 0, "and still measures nothing");

    /* DOUBLE TALK. The near end carries more than the echo path can explain, so
     * nothing adapts and nothing is measured -- the canceller is supposed to
     * PRESERVE that voice, and counting it would report the canceller as broken
     * every time the Owner speaks. */
    aec_block talking = { .now_ms = 10, .far_dbfs = -20.0, .mic_dbfs = -20.0,
                          .residual_dbfs = -21.0, .mic_peak_dbfs = -15.0,
                          .input_muted = false, .tap_present = true };
    aec_policy_block(&policy, &talking);
    ok(policy.double_talk_frames_1min == 1, "double talk is counted");
    ok(policy.erle_n_session == 0, "and contributes no ERLE sample");
    ok(policy.far_active_frames_1min == 1, "though the far end was active");

    /* A QUALIFYING FRAME: far end active, near end quiet. */
    aec_action saw = AEC_ACTION_NONE;
    feed(&policy, 1, 20.0, 20, &saw);
    ok(policy.erle_n_session == 1, "far end active and near end quiet DOES count");
    near(policy.erle_db_session, 20.0, 1e-9, "at the ERLE it was handed");
}

/* ── the warm-up grace, the baseline, and the reset ──────────────────────── */

static void test_baseline_and_reset(void)
{
    aec_profile profile = fixture_profile();
    aec_policy policy;
    aec_policy_init(&policy, &profile, 0);
    aec_action saw = AEC_ACTION_NONE;

    int64_t now = feed(&policy, AEC_WARMUP_QUALIFYING_FRAMES - 1, 25.0, 0, &saw);
    ok(policy.state == AEC_STATE_CONVERGING, "inside the grace period it is converging");
    ok(!policy.baseline_valid, "and no baseline has been taken");
    ok(saw == AEC_ACTION_NONE, "a cold filter is never reset for being cold");

    now = feed(&policy, 1, 25.0, now, &saw);
    ok(policy.baseline_valid, "the baseline is taken once the grace has elapsed");
    near(policy.session_baseline_db, 25.0, 0.01, "and is what this filter settled at");
    ok(policy.state == AEC_STATE_CANCELLING, "and the state says so");

    /* A ROOM THAT IS MERELY DIFFERENT IS NOT A DIVERGED FILTER. 5 dB below the
     * baseline is inside the 6 dB band and must not reset anything, however
     * long it lasts. */
    now = feed(&policy, AEC_DIVERGENCE_QUALIFYING_FRAMES * 2, 24.0, now, &saw);
    ok(saw == AEC_ACTION_NONE, "a filter inside its own band is never thrown away");

    /* Now push the SESSION average far enough below the baseline for long
     * enough, WITHOUT crossing the absolute floor -- 15 dB is more than 6 below
     * a 25 dB baseline and still above a 12 dB floor. Keeping them apart is the
     * point of the test: the floor is checked first and would otherwise fire
     * and mask the relative rule entirely. The session average is what is
     * compared, so this takes a while by construction, which is also the point:
     * one bad second is not divergence. */
    saw = AEC_ACTION_NONE;
    now = feed(&policy, AEC_DIVERGENCE_QUALIFYING_FRAMES * 6, 15.0, now, &saw);
    ok(saw == AEC_ACTION_RESET_FILTER, "a genuinely diverged filter IS thrown away");
    ok(policy.resets_this_session == 1, "and the reset is counted");
    ok(!policy.baseline_valid, "the baseline goes with it");
    ok(policy.qualifying_frames == 0, "and the warm-up grace restarts");
}

static void test_give_up_bound(void)
{
    /* THREE RESETS THAT DID NOT HELP END THE MATTER. A daemon that kept trying
     * would spend the evening paying cold convergence over and over, and would
     * never tell anybody.
     *
     * "Did not help" is the load-bearing phrase and it is why each round
     * settles WORSE than the last. A replacement filter that comes back at the
     * old baseline HAS helped, and that ends the streak -- asserted separately
     * below. Only a filter that keeps coming back worse gets to the bound.
     *
     * The profile floor is lowered to 1 dB for this test alone, so the
     * RELATIVE rule can be exercised across four rounds without the absolute
     * floor firing first and masking it -- the two are checked in that order
     * by design, and test_absolute_floor covers the other way round. */
    aec_profile profile = fixture_profile();
    profile.erle_floor_db = 1.0;
    aec_policy policy;
    aec_policy_init(&policy, &profile, 0);
    aec_action saw = AEC_ACTION_NONE;
    int64_t now = 0;
    /* Each pair is (what the replacement settles at, what it then falls to).
     * Every fall is 8 dB, comfortably past the 6 dB divergence band. */
    const double settles[] = { 40.0, 30.0, 20.0, 10.0 };
    for (int round = 0; round < 4 && policy.state != AEC_STATE_DEGRADED; round++) {
        saw = AEC_ACTION_NONE;
        now = feed(&policy, AEC_WARMUP_QUALIFYING_FRAMES, settles[round], now, &saw);
        now += AEC_RESET_COOLDOWN_MS;
        saw = AEC_ACTION_NONE;
        now = feed(&policy, AEC_DIVERGENCE_QUALIFYING_FRAMES * 10,
                   settles[round] - 8.0, now, &saw);
    }
    ok(policy.state == AEC_STATE_DEGRADED, "it gives up rather than looping");
    ok(!policy.adapting, "and stops adapting");
    ok(policy.resets_this_session == AEC_MAX_CONSECUTIVE_RESETS,
       "after exactly the bound, not one more");

    /* AND A RESET THAT ACTUALLY HELPED ENDS THE STREAK. This is the half an
     * earlier draft could not express at all: it cleared the streak on the
     * first in-band FRAME, which happens immediately after every reset because
     * a freshly re-initialised filter is in band by construction, so the
     * counter could never reach two. */
    aec_policy_init(&policy, &profile, 0);
    now = 0;
    now = feed(&policy, AEC_WARMUP_QUALIFYING_FRAMES, 40.0, now, &saw);
    saw = AEC_ACTION_NONE;
    now = feed(&policy, AEC_DIVERGENCE_QUALIFYING_FRAMES * 10, 32.0, now, &saw);
    ok(saw == AEC_ACTION_RESET_FILTER && policy.consecutive_resets == 1, "one reset so far");
    now = feed(&policy, AEC_WARMUP_QUALIFYING_FRAMES, 40.0, now, &saw);
    ok(policy.consecutive_resets == 0,
       "a replacement that settles back at the old baseline ends the streak");
}

static void test_reset_rate_limit(void)
{
    /* AT MOST ONE RESET PER COOLDOWN, and the clock is stepped by ZERO here so
     * that the whole sequence happens inside one cooldown window -- which is
     * the only way to assert the rate limit without simulating five real
     * minutes of audio. */
    aec_profile profile = fixture_profile();
    aec_policy policy;
    aec_policy_init(&policy, &profile, 0);
    aec_action saw = AEC_ACTION_NONE;
    feed_step(&policy, AEC_WARMUP_QUALIFYING_FRAMES, 25.0, 0, &saw, 0);
    saw = AEC_ACTION_NONE;
    feed_step(&policy, AEC_DIVERGENCE_QUALIFYING_FRAMES * 8, 15.0, 0, &saw, 0);
    ok(saw == AEC_ACTION_RESET_FILTER, "the first reset fires");
    ok(policy.resets_this_session == 1, "once");

    /* A second divergence inside the same cooldown must NOT produce a second
     * reset. And refusing it does not lose the evidence: the below-baseline
     * counter keeps running, so the reset happens the moment the cooldown
     * expires rather than starting the count over. */
    feed_step(&policy, AEC_WARMUP_QUALIFYING_FRAMES, 25.0, 0, &saw, 0);
    saw = AEC_ACTION_NONE;
    feed_step(&policy, AEC_DIVERGENCE_QUALIFYING_FRAMES * 8, 15.0, 0, &saw, 0);
    ok(policy.resets_this_session == 1, "a second reset inside the cooldown is refused");
    ok(policy.below_baseline_frames >= AEC_DIVERGENCE_QUALIFYING_FRAMES,
       "and the evidence for it is kept rather than discarded");

    /* Past the cooldown, the waiting reset fires at once. */
    saw = AEC_ACTION_NONE;
    feed_step(&policy, 4, 15.0, AEC_RESET_COOLDOWN_MS + 1, &saw, 0);
    ok(saw == AEC_ACTION_RESET_FILTER, "and fires the moment the cooldown expires");
    ok(policy.resets_this_session == 2, "as the second, not the fifth");
}

static void test_absolute_floor(void)
{
    /* THE ONLY PLACE A PROFILE NUMBER REACHES FAILOVER. Below the floor the
     * canceller is not useful whatever its history, so the mic legs hand over
     * to push-to-talk -- and this is checked BEFORE the relative test, because
     * a filter can be consistent with its own bad baseline indefinitely. */
    aec_profile profile = fixture_profile();
    aec_policy policy;
    aec_policy_init(&policy, &profile, 0);
    aec_action saw = AEC_ACTION_NONE;
    int64_t now = feed(&policy, AEC_WARMUP_QUALIFYING_FRAMES, 5.0, 0, &saw);
    ok(policy.session_baseline_db < profile.erle_floor_db,
       "a baseline can itself be below the floor");
    saw = AEC_ACTION_NONE;
    feed(&policy, AEC_FLOOR_QUALIFYING_FRAMES, 5.0, now, &saw);
    ok(saw == AEC_ACTION_FAILOVER, "and the floor still fires");
    ok(policy.failed_over, "the policy records the failover");
    ok(policy.state == AEC_STATE_BYPASSED, "and says `bypassed`, not `cancelling`");
    /* THE SHELL READS `failed_over` TO STOP CANCELLING, and leaving that out
     * was a lie in the journal (terra, second pass): the daemon logged "the
     * microphone is passed through" and went on calling the canceller on the
     * very next block. The flag is asserted here because it is the one the
     * shell's gate reads; the gate itself is C the policy tests cannot run. */
    ok(policy.failed_over && policy.state == AEC_STATE_BYPASSED,
       "and both facts the shell gates on are set together");
}

/* ── the resampler handoff ───────────────────────────────────────────────── */

static void test_drift_admission(void)
{
    aec_profile profile = fixture_profile();
    aec_policy policy;
    aec_policy_init(&policy, &profile, 0);

    /* A ZERO accuracy_report MEANS THE ACCURACY FIELD IS MEANINGLESS. It has to
     * be checked before the number it guards is used, or a driver reporting
     * garbage accuracy would sail through a rule written to keep it out. */
    aec_policy_drift(&policy, 1000, 3.0, AEC_DRIFT_WINDOW_MS, true, false, 10, true);
    ok(policy.drift_state == AEC_DRIFT_UNMEASURABLE, "an unreported accuracy is unmeasurable");

    aec_policy_drift(&policy, 2000, 3.0, AEC_DRIFT_WINDOW_MS, false, true, 500, true);
    ok(policy.drift_state == AEC_DRIFT_UNMEASURABLE, "an invalid timestamp is unmeasurable");

    /* A silently substituted DEFAULT where LINK was asked for is not the clock
     * the slip arithmetic assumes. */
    aec_policy_drift(&policy, 3000, 3.0, AEC_DRIFT_WINDOW_MS, true, true, 500, false);
    ok(policy.drift_state == AEC_DRIFT_UNMEASURABLE, "a substituted timestamp type is unmeasurable");

    /* FOUR timing errors enter the ratio of two per-stream ratios, so the
     * resolvable slip is 4a/T and not 2a/T -- counting two overstates the
     * margin. At a = 1000 ns and T = 10 s that is 0.4 ppm, which passes; at
     * a = 2500 ns it is 1.0 ppm, which does not resolve the 1 ppm the
     * controller acts at and is refused over that window. */
    aec_policy_drift(&policy, 4000, 3.0, AEC_DRIFT_WINDOW_MS, true, true,
                     AEC_DRIFT_ACCURACY_NS, true);
    ok(policy.drift_state == AEC_DRIFT_MEASURING, "1000 ns over 10 s is admitted");

    aec_policy_init(&policy, &profile, 0);
    aec_policy_drift(&policy, 4000, 3.0, AEC_DRIFT_WINDOW_MS, true, true,
                     AEC_DRIFT_LONG_ACCURACY_NS, true);
    ok(policy.drift_state == AEC_DRIFT_UNMEASURABLE, "2500 ns over 10 s is NOT");
    /* The documented fallback: the same accuracy over the longer window does
     * resolve it, and the update interval stretches with it. */
    aec_policy_init(&policy, &profile, 0);
    aec_policy_drift(&policy, 4000, 3.0, AEC_DRIFT_LONG_WINDOW_MS, true, true,
                     AEC_DRIFT_LONG_ACCURACY_NS, true);
    ok(policy.drift_state == AEC_DRIFT_MEASURING, "but 2500 ns over 100 s does");
}

static void test_drift_controller(void)
{
    aec_profile profile = fixture_profile();
    aec_policy policy;
    aec_policy_init(&policy, &profile, 0);
    int64_t now = 0;
    bool applied_before_activation = false;
    int32_t first_ratio = 0;
    bool applied = false;

    /* BEFORE ACTIVATION THE RATIO IS HELD AT UNITY and the measurement runs
     * anyway, which is also how the overnight capture collects its data.
     * Activation is ten minutes of continuous measurement -- 60 windows of
     * 10 s -- so 70 windows carries it comfortably past. */
    for (int i = 0; i < 70; i++) {
        now += AEC_DRIFT_WINDOW_MS;
        aec_decision decision = aec_policy_drift(&policy, now, 40.0, AEC_DRIFT_WINDOW_MS,
                                                 true, true, AEC_DRIFT_ACCURACY_NS, true);
        bool activated = policy.drift_started_ms != INT64_MIN &&
                         now - policy.drift_started_ms >= AEC_DRIFT_ACTIVATION_MS;
        if (decision.action == AEC_ACTION_SET_RATE) {
            if (!activated) applied_before_activation = true;
            if (!applied) { applied = true; first_ratio = decision.ratio_ppm; }
        }
    }
    ok(!applied_before_activation, "nothing is applied before activation");
    ok(policy.drift_state == AEC_DRIFT_ACTIVE, "after ten minutes it activates");
    ok(applied, "and a sustained slip is then corrected");

    /* DIRECTION, spelled out because it is easy to invert. A positive slip means
     * the tap runs FAST, so the reference must be resampled DOWN: the input
     * rate is the higher one, numerator above denominator, and the resampler
     * consumes more than it produces. */
    ok(first_ratio > 1000000, "a FAST tap gives a numerator above the denominator");

    /* And the other direction, from a fresh policy, so the sign cannot be an
     * accident of this one's history. */
    aec_policy slow;
    aec_policy_init(&slow, &profile, 0);
    now = 0;
    int32_t slow_ratio = 0;
    for (int i = 0; i < 200 && !slow_ratio; i++) {
        now += AEC_DRIFT_WINDOW_MS;
        aec_decision decision = aec_policy_drift(&slow, now, -40.0, AEC_DRIFT_WINDOW_MS,
                                                 true, true, AEC_DRIFT_ACCURACY_NS, true);
        if (decision.action == AEC_ACTION_SET_RATE) slow_ratio = decision.ratio_ppm;
    }
    ok(slow_ratio != 0 && slow_ratio < 1000000, "a SLOW tap gives a numerator below it");

    /* IT DOES NOT RECOMPUTE FILTER TABLES FOR NOTHING. With the estimate
     * settled, further identical measurements must not keep asking. */
    int asks = 0;
    for (int i = 0; i < 50; i++) {
        now += AEC_DRIFT_WINDOW_MS;
        aec_decision decision = aec_policy_drift(&policy, now, 40.0, AEC_DRIFT_WINDOW_MS,
                                                 true, true, AEC_DRIFT_ACCURACY_NS, true);
        if (decision.action == AEC_ACTION_SET_RATE) asks++;
    }
    ok(asks <= 1, "a settled estimate stops asking for new filter tables");

    /* BOUNDED, AND LOUD OUTSIDE THE BOUND. Two crystals differ by tens of ppm
     * at worst, so a demand past 100 ppm is a fault -- a misconfigured rate, a
     * reopened device, an unnoticed overrun -- and resampling further would
     * hide it. */
    aec_policy wild;
    aec_policy_init(&wild, &profile, 0);
    now = 0;
    aec_decision decision = { AEC_ACTION_NONE, 0 };
    for (int i = 0; i < 400; i++) {
        now += AEC_DRIFT_WINDOW_MS;
        decision = aec_policy_drift(&wild, now, 5000.0, AEC_DRIFT_WINDOW_MS, true, true,
                                    AEC_DRIFT_ACCURACY_NS, true);
        if (wild.drift_state == AEC_DRIFT_CLAMPED) break;
    }
    ok(wild.drift_state == AEC_DRIFT_CLAMPED, "an impossible slip is clamped, not chased");
    ok(decision.action == AEC_ACTION_FAILOVER, "and is a failover, not a resample");
    ok(wild.state == AEC_STATE_DEGRADED, "reported as degraded");
}

static void test_xrun(void)
{
    aec_profile profile = fixture_profile();
    aec_policy policy;
    aec_policy_init(&policy, &profile, 0);
    aec_action saw = AEC_ACTION_NONE;
    int64_t now = feed(&policy, AEC_WARMUP_QUALIFYING_FRAMES, 25.0, 0, &saw);
    ok(policy.baseline_valid, "a baseline exists before the xrun");

    aec_policy_xrun(&policy, now);
    ok(policy.xruns_this_session == 1, "the xrun is counted");
    ok(!policy.baseline_valid, "the baseline is gone with the alignment");
    ok(policy.qualifying_frames == 0,
       "and the warm-up grace applies from here, so nothing fires on the cold convergence");
    ok(policy.drift_started_ms == INT64_MIN, "the drift baseline restarts too");
    ok(policy.drift_state != AEC_DRIFT_ACTIVE, "and the controller stands down to measuring");
    ok(policy.resets_this_session == 0, "but the session record is NOT erased");
}

static void test_detection_latency_is_bounded(void)
{
    /* THE FAILURE THIS REPLACED, AND WHY A LIFETIME MEAN WAS THE WRONG
     * STATISTIC (terra, 2026-09-14). Comparing an average taken over every
     * qualifying frame since the last re-arm makes the detection latency a
     * function of the UPTIME: an hour of healthy audio dilutes a sudden
     * complete failure so far that the mean needs a further quarter of an hour
     * to cross the threshold, and the 60-second counters cannot bound that
     * because they only start once it has.
     *
     * The estimator is now a one-pole over qualifying frames, so the same
     * failure is detected after the same number of frames whether the panel has
     * been up for a minute or a week. This runs the SAME failure after two very
     * different healthy runs and requires the two latencies to be comparable.
     */
    aec_profile profile = fixture_profile();
    profile.erle_floor_db = 1.0;
    int64_t latency[2];
    const int healthy[2] = { AEC_WARMUP_QUALIFYING_FRAMES,
                             AEC_WARMUP_QUALIFYING_FRAMES * 30 };
    for (int run = 0; run < 2; run++) {
        aec_policy policy;
        aec_policy_init(&policy, &profile, 0);
        aec_action saw = AEC_ACTION_NONE;
        feed_step(&policy, healthy[run], 40.0, 0, &saw, 0);
        ok(policy.baseline_valid, "a baseline was taken");
        int64_t frames = 0;
        saw = AEC_ACTION_NONE;
        while (saw == AEC_ACTION_NONE && frames < AEC_WARMUP_QUALIFYING_FRAMES * 40) {
            feed_step(&policy, 1, 5.0, 0, &saw, 0);
            frames += 1;
        }
        ok(saw == AEC_ACTION_RESET_FILTER, "the failure is detected");
        latency[run] = frames;
    }
    /* Not equal -- the estimator carries a little history -- but within a small
     * factor, which is the property a lifetime mean did not have at all. With
     * the old statistic the second run took roughly THIRTY times longer. */
    ok(latency[1] < latency[0] * 2,
       "detection latency does not grow with the length of the healthy run");
}

/* ── item L and the status block ─────────────────────────────────────────── */

static void test_level_and_status(void)
{
    aec_profile profile = fixture_profile();
    aec_policy policy;
    aec_policy_init(&policy, &profile, 0);

    aec_block block = { .now_ms = 0, .far_dbfs = -20.0, .mic_dbfs = -30.0,
                        .residual_dbfs = AEC_LEVEL_REFERENCE_DBFS, .mic_peak_dbfs = -15.0,
                        .input_muted = false, .tap_present = true };
    aec_policy_block(&policy, &block);
    ok(policy.level_valid, "a live block publishes a level");
    near(policy.level, 1.0, 1e-6, "at the reference the level is 1");

    /* BOUNDED UPDATE RATE. A room cannot make the wall flicker at block rate. */
    int64_t before = policy.level_updated_ms;
    block.now_ms = AEC_LEVEL_UPDATE_INTERVAL_MS - 1;
    block.residual_dbfs = AEC_LEVEL_FLOOR_DBFS;
    aec_policy_block(&policy, &block);
    ok(policy.level_updated_ms == before, "an update inside the interval is skipped");
    near(policy.level, 1.0, 1e-6, "and the published level does not move");

    /* BOUNDED SMOOTHING. One pole: a step does not arrive all at once. */
    block.now_ms = AEC_LEVEL_UPDATE_INTERVAL_MS;
    aec_policy_block(&policy, &block);
    ok(policy.level < 1.0 && policy.level > 0.2, "a step is smoothed, not followed");

    /* MUTED IS NOT A LOW LEVEL, IT IS AN INVALID ONE. A ring drawn from a
     * muted microphone would be a live-looking animation over a dead capture. */
    block.now_ms = 1000;
    block.input_muted = true;
    aec_policy_block(&policy, &block);
    ok(!policy.level_valid, "a muted input publishes no level at all");
    near(policy.level, 0.0, 1e-9, "and the value is zeroed rather than frozen");

    char buffer[2048];
    size_t written = aec_policy_status(&policy, "2026-09-14T02:10:00Z", buffer, sizeof(buffer));
    ok(written > 0, "the status block serializes");
    ok(strstr(buffer, "\"schema\": 1") != NULL, "carries its schema");
    ok(strstr(buffer, "\"source\": \"aec_post_filter\"") != NULL,
       "and says the level is POST-filter, which is the one claim item L rests on");
    ok(strstr(buffer, "\"valid\": false") != NULL, "a muted level is published as invalid");
    ok(strstr(buffer, "\"state\": \"muted\"") != NULL, "with the reason named");
    ok(strstr(buffer, "\"reference_dbfs\": -18.0") != NULL, "and the documented reference");
    ok(strstr(buffer, "2026-09-14T02:10:00Z") != NULL, "and the caller's timestamp");

    /* ERLE IS PUBLISHED ONLY WHEN IT IS DEFINED. A plausible wrong number on a
     * status block is worse than an honest absence. */
    aec_policy fresh;
    aec_policy_init(&fresh, &profile, 0);
    written = aec_policy_status(&fresh, "2026-09-14T02:10:00Z", buffer, sizeof(buffer));
    ok(written > 0 && strstr(buffer, "\"erle_db_1min\": null") != NULL,
       "with no qualifying frames the 1-minute ERLE is null, not zero");
    ok(strstr(buffer, "\"erle_db_session\": null") != NULL, "and so is the session figure");
    ok(strstr(buffer, "\"erle_at_retune_db\": 27.0") != NULL,
       "the retune figure is reported beside the live one, never acted on");

    /* A buffer too small writes NOTHING rather than a truncated document. */
    char tiny[16];
    ok(aec_policy_status(&fresh, "2026-09-14T02:10:00Z", tiny, sizeof(tiny)) == 0,
       "a buffer too small produces no half-written status");

    ok(!strcmp(aec_state_name(AEC_STATE_CANCELLING), "cancelling"), "state words");
    ok(!strcmp(aec_state_name(AEC_STATE_INSUFFICIENT_DATA), "insufficient-data"), "hyphenated");
    ok(!strcmp(aec_drift_state_name(AEC_DRIFT_UNMEASURABLE), "unmeasurable"), "drift words");
}

int main(void)
{
    test_normalization();
    test_profile();
    test_gating();
    test_baseline_and_reset();
    test_give_up_bound();
    test_reset_rate_limit();
    test_absolute_floor();
    test_drift_admission();
    test_drift_controller();
    test_xrun();
    test_detection_latency_is_bounded();
    test_level_and_status();
    printf("\n%d PASS  %d FAIL\n", passed, failed);
    return failed ? 1 : 0;
}

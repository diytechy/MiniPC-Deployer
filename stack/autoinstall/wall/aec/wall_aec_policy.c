/* wall_aec_policy.c -- the pure decision core. See wall_aec_policy.h.
 *
 * No I/O, no allocation, no ALSA, no SpeexDSP. Implements: SR-028, LLR-016
 */

#include "wall_aec_policy.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

/* ── small helpers ───────────────────────────────────────────────────────── */

static double clamp(double value, double low, double high)
{
    if (!(value == value)) return low;   /* NaN: the floor is the safe answer */
    if (value < low) return low;
    if (value > high) return high;
    return value;
}

double aec_dbfs(double amplitude)
{
    /* FLOORED, NOT -inf. A block of digital silence is not an error -- it is
     * every block while nothing is playing -- and letting it produce -inf would
     * poison the first average it entered and never leave it. */
    if (!(amplitude > 0.0)) return AEC_LEVEL_FLOOR_DBFS;
    double db = 20.0 * log10(amplitude);
    return db < AEC_LEVEL_FLOOR_DBFS ? AEC_LEVEL_FLOOR_DBFS : db;
}

double aec_level_from_dbfs(double dbfs)
{
    /* Linear in DECIBELS between the floor and the reference, which is what the
     * telemetry contract promises group B and what makes a ring that tracks how
     * loud a room SOUNDS rather than one that sits near zero until somebody
     * shouts. Amplitude-linear was the obvious first answer and is wrong for
     * exactly that reason: speech at -30 dBFS is 0.03 in amplitude. */
    double span = AEC_LEVEL_REFERENCE_DBFS - AEC_LEVEL_FLOOR_DBFS;
    return clamp((dbfs - AEC_LEVEL_FLOOR_DBFS) / span, 0.0, 1.0);
}

/* ── the profile ─────────────────────────────────────────────────────────── */

void aec_profile_defaults(aec_profile *profile)
{
    memset(profile, 0, sizeof(*profile));
    profile->present = false;
    profile->predelay_ms = AEC_DEFAULT_PREDELAY_MS;
    profile->filter_length_ms = AEC_DEFAULT_FILTER_MS;
    profile->bulk_delay_ms = 0.0;
    profile->erle_floor_db = AEC_DEFAULT_ERLE_FLOOR_DB;
    profile->erle_at_retune_db = 0.0;
    profile->age_days = -1;
    snprintf(profile->name, sizeof(profile->name), "%s", "(defaults)");
}

bool aec_profile_clamp(aec_profile *profile)
{
    bool ok = true;
    double filter = clamp(profile->filter_length_ms, AEC_FILTER_MIN_MS, AEC_FILTER_MAX_MS);
    if (filter != profile->filter_length_ms) { profile->filter_length_ms = filter; ok = false; }
    double predelay = clamp(profile->predelay_ms, AEC_PREDELAY_MIN_MS, AEC_PREDELAY_MAX_MS);
    if (predelay != profile->predelay_ms) { profile->predelay_ms = predelay; ok = false; }
    /* THE ONE RULE THE SPIKE CALLS ITS MOST IMPORTANT LINE. Overshooting the
     * bulk delay costs 6 dB of ERLE and adaptation cannot recover it;
     * undershooting costs only a little filter length. So the pre-delay is held
     * at least AEC_PREDELAY_MARGIN_MS below the measured bulk delay, and a
     * profile that violates it is corrected rather than obeyed. */
    if (profile->bulk_delay_ms > 0.0) {
        double ceiling = profile->bulk_delay_ms - AEC_PREDELAY_MARGIN_MS;
        if (ceiling < AEC_PREDELAY_MIN_MS) ceiling = AEC_PREDELAY_MIN_MS;
        if (profile->predelay_ms > ceiling) { profile->predelay_ms = ceiling; ok = false; }
    }
    if (!(profile->erle_floor_db > 0.0) || profile->erle_floor_db > 60.0) {
        profile->erle_floor_db = AEC_DEFAULT_ERLE_FLOOR_DB; ok = false;
    }
    return ok;
}

/* ── lifecycle ───────────────────────────────────────────────────────────── */

void aec_policy_rearm(aec_policy *policy, int64_t now_ms)
{
    /* Everything a filter re-init invalidates, and nothing else. The session
     * counters (resets, xruns) deliberately survive: they are the record of how
     * this run has gone, and zeroing them here would hide exactly the pattern
     * the give-up bound exists to catch. */
    policy->qualifying_frames = 0;
    policy->baseline_valid = false;
    policy->session_baseline_db = 0.0;
    policy->below_baseline_frames = 0;
    policy->below_floor_frames = 0;
    policy->erle_sum_session = 0.0;
    policy->erle_n_session = 0;
    policy->erle_db_session = 0.0;
    policy->erle_recent_db = 0.0;
    policy->erle_recent_valid = false;
    policy->adapting = true;
    policy->state = policy->profile.present ? AEC_STATE_CONVERGING : AEC_STATE_NO_PROFILE;
    (void)now_ms;
}

void aec_policy_init(aec_policy *policy, const aec_profile *profile, int64_t now_ms)
{
    memset(policy, 0, sizeof(*policy));
    policy->profile = *profile;
    policy->last_reset_ms = INT64_MIN;
    policy->drift_started_ms = INT64_MIN;
    policy->drift_state = AEC_DRIFT_MEASURING;
    policy->window_started_ms = now_ms;
    policy->last_block_ms = now_ms;
    policy->level_updated_ms = now_ms;
    policy->mic_peak_dbfs_1min = AEC_LEVEL_FLOOR_DBFS;
    aec_policy_rearm(policy, now_ms);
}

void aec_policy_xrun(aec_policy *policy, int64_t now_ms)
{
    policy->xruns_this_session += 1;
    /* The timestamp baseline is gone with the discontinuity, so the drift
     * ESTIMATE restarts -- but the RATIO is kept by the shell, because the
     * oscillators have not changed. Chasing a step change with a rate
     * controller is the thing the spike separates these two mechanisms to
     * prevent. */
    policy->drift_started_ms = INT64_MIN;
    policy->drift_updates = 0;
    if (policy->drift_state == AEC_DRIFT_ACTIVE) policy->drift_state = AEC_DRIFT_MEASURING;
    /* The warm-up grace applies from here, so neither a reset nor a failover
     * can fire on the cold convergence this causes. */
    aec_policy_rearm(policy, now_ms);
}

/* ── the block ───────────────────────────────────────────────────────────── */

static void roll_window(aec_policy *policy, int64_t now_ms)
{
    /* The 1-minute window is a WINDOW, not an average since boot: a figure that
     * has been diluted by an hour of silence tells nobody anything about now. */
    if (now_ms - policy->window_started_ms < 60000) return;
    policy->erle_db_1min = policy->erle_n_1min > 0
        ? policy->erle_sum_1min / (double)policy->erle_n_1min : 0.0;
    policy->erle_1min_valid = policy->erle_n_1min > 0;
    policy->erle_sum_1min = 0.0;
    policy->erle_n_1min = 0;
    policy->far_active_frames_1min = 0;
    policy->double_talk_frames_1min = 0;
    policy->mic_peak_dbfs_1min = AEC_LEVEL_FLOOR_DBFS;
    policy->window_started_ms = now_ms;
}

static void update_level(aec_policy *policy, const aec_block *block)
{
    /* ITEM L. The number is a BY-PRODUCT of the canceller's existing stream:
     * nothing is opened, started or kept running in order to produce it, which
     * is the plan's rule. When there is no stream there is no number. */
    if (block->input_muted || !policy->adapting) {
        policy->level_valid = false;
        policy->level = 0.0;
        return;
    }
    int64_t since = block->now_ms - policy->level_updated_ms;
    if (policy->level_valid && since < AEC_LEVEL_UPDATE_INTERVAL_MS) return;
    double target = aec_level_from_dbfs(block->residual_dbfs);
    if (!policy->level_valid) {
        policy->level = target;
    } else {
        /* One pole, bounded. The coefficient is derived from the elapsed time
         * rather than assumed, because the update interval is a floor and not a
         * guarantee -- a block that arrives late must not smooth harder than
         * one that arrives on time. */
        double alpha = 1.0 - exp(-(double)since / AEC_LEVEL_TIME_CONSTANT_MS);
        policy->level += alpha * (target - policy->level);
    }
    policy->level = clamp(policy->level, 0.0, 1.0);
    policy->level_valid = true;
    policy->level_updated_ms = block->now_ms;
}

aec_decision aec_policy_block(aec_policy *policy, const aec_block *block)
{
    aec_decision decision = { AEC_ACTION_NONE, 0 };
    policy->last_block_ms = block->now_ms;
    roll_window(policy, block->now_ms);
    if (block->mic_peak_dbfs > policy->mic_peak_dbfs_1min)
        policy->mic_peak_dbfs_1min = block->mic_peak_dbfs;

    /* THE FAR END DECIDES WHETHER ANY OF THIS IS DEFINED. ERLE measured while
     * the tap is silent is a ratio of two noise floors; measured while somebody
     * is talking it is dominated by their voice, which the canceller is meant
     * to PRESERVE. Without this gate the panel reports `degraded` every time
     * the Owner speaks, trips push-to-talk, and teaches everyone to ignore the
     * status block. */
    bool far_active = block->tap_present && block->far_dbfs > AEC_FAR_ACTIVE_DBFS;
    /* Double talk: the near end carries more than the echo path can explain.
     * A false positive only PAUSES adaptation, which is why a crude statistic
     * is the right instrument here. */
    bool double_talk = far_active &&
        block->mic_dbfs > block->far_dbfs - policy->profile.erle_floor_db + AEC_DOUBLE_TALK_MARGIN_DB;

    if (far_active) policy->far_active_frames_1min += 1;
    if (double_talk) policy->double_talk_frames_1min += 1;

    update_level(policy, block);

    if (!far_active) {
        /* No reference: the filter freezes and the microphone passes through.
         * There is no mode logic in this -- it falls out of the tap, exactly as
         * the amplifier detector's hold-off does. */
        if (!policy->failed_over)
            policy->state = policy->profile.present ? AEC_STATE_IDLE : AEC_STATE_NO_PROFILE;
        return decision;
    }
    if (double_talk) return decision;   /* adapt on nothing; measure nothing */

    /* A QUALIFYING FRAME: far end active, near end quiet. Only these count, and
     * the status block publishes how many went into the figure so that a number
     * built from six frames is visibly not a measurement. */
    double erle = block->mic_dbfs - block->residual_dbfs;
    if (erle < 0.0) erle = 0.0;
    policy->erle_sum_1min += erle;  policy->erle_n_1min += 1;
    policy->erle_sum_session += erle; policy->erle_n_session += 1;
    policy->erle_db_session = policy->erle_sum_session / (double)policy->erle_n_session;
    /* The bounded estimator the health rules actually act on. One pole over
     * qualifying frames: its response time is fixed by AEC_RECENT_TAU_FRAMES
     * and does not grow with how long the panel has been up. */
    if (!policy->erle_recent_valid) {
        policy->erle_recent_db = erle;
        policy->erle_recent_valid = true;
    } else {
        policy->erle_recent_db += (erle - policy->erle_recent_db) / AEC_RECENT_TAU_FRAMES;
    }
    policy->qualifying_frames += 1;

    if (policy->qualifying_frames < AEC_WARMUP_QUALIFYING_FRAMES) {
        /* The grace period. No baseline, no reset, no failover -- a cold filter
         * is not a broken one. */
        if (!policy->failed_over) policy->state = AEC_STATE_CONVERGING;
        return decision;
    }
    if (!policy->baseline_valid) {
        /* The SESSION baseline: what this filter settles at on this room's
         * ordinary audio. Everything below is relative to it, because the
         * retune figure came from a controlled stimulus and is not a
         * like-for-like comparison. */
        /* The baseline is taken from the SAME statistic the tests below use.
         * Taking it from the session mean and comparing the recent estimate
         * against it would compare two different things, and the difference
         * between them -- not a change in the room -- would be what fired. */
        policy->session_baseline_db = policy->erle_recent_db;
        policy->baseline_valid = true;
        policy->below_baseline_frames = 0;
        /* DID THE LAST RESET ACTUALLY RESTORE ANYTHING? That question can only
         * be answered HERE, when the replacement filter has settled and taken
         * its own baseline -- and answering it anywhere else is what made an
         * earlier draft unable to count to three at all: clearing the streak on
         * the first in-band FRAME cleared it immediately after every reset,
         * because a freshly re-initialised filter is in band by construction.
         *
         * The streak ends only when the new baseline is no worse than the one
         * the reset was trying to recover. Otherwise it stands, and three of
         * them end the matter. */
        if (policy->consecutive_resets > 0 &&
            policy->session_baseline_db >= policy->baseline_before_reset_db - AEC_DIVERGENCE_DB) {
            policy->consecutive_resets = 0;
        }
    }

    double current = policy->erle_recent_db;

    /* THE ABSOLUTE FLOOR, and the only place a profile number reaches failover.
     * Below it the canceller is not useful whatever its history, so the mic legs
     * hand over to push-to-talk. It is checked BEFORE the relative test because
     * a filter can be consistent with its own bad baseline indefinitely. */
    if (current < policy->profile.erle_floor_db) {
        policy->below_floor_frames += 1;
        if (policy->below_floor_frames >= AEC_FLOOR_QUALIFYING_FRAMES && !policy->failed_over) {
            policy->failed_over = true;
            policy->state = AEC_STATE_BYPASSED;
            decision.action = AEC_ACTION_FAILOVER;
            return decision;
        }
    } else {
        policy->below_floor_frames = 0;
        if (policy->failed_over) { policy->failed_over = false; }
    }

    /* DIVERGENCE, relative to what this filter itself achieved. A filter that
     * has got worse than it was is thrown away; a room that is merely unusual
     * is not, and that is a test the filter cannot fail by the room being
     * different. */
    if (current < policy->session_baseline_db - AEC_DIVERGENCE_DB) {
        policy->below_baseline_frames += 1;
    } else {
        policy->below_baseline_frames = 0;
    }

    if (policy->below_baseline_frames >= AEC_DIVERGENCE_QUALIFYING_FRAMES) {
        if (policy->consecutive_resets >= AEC_MAX_CONSECUTIVE_RESETS) {
            /* IT GIVES UP RATHER THAN LOOPING. Three resets that did not
             * restore the baseline is evidence that resetting is not the
             * answer, and a daemon that kept trying would spend the evening
             * paying cold convergence over and over. */
            policy->adapting = false;
            policy->state = AEC_STATE_DEGRADED;
            return decision;
        }
        if (policy->last_reset_ms != INT64_MIN &&
            block->now_ms - policy->last_reset_ms < AEC_RESET_COOLDOWN_MS) {
            return decision;   /* rate limited; the counter keeps running */
        }
        policy->resets_this_session += 1;
        policy->consecutive_resets += 1;
        /* Remembered across the re-arm, so the replacement filter's own
         * baseline can be compared against what it was meant to recover. */
        policy->baseline_before_reset_db = policy->session_baseline_db;
        policy->last_reset_ms = block->now_ms;
        aec_policy_rearm(policy, block->now_ms);
        decision.action = AEC_ACTION_RESET_FILTER;
        return decision;
    }

    if (!policy->failed_over && policy->adapting) {
        policy->state = policy->erle_n_1min > 0 || policy->erle_1min_valid
            ? AEC_STATE_CANCELLING : AEC_STATE_INSUFFICIENT_DATA;
    }
    return decision;
}

/* ── the drift controller ────────────────────────────────────────────────── */

aec_decision aec_policy_drift(aec_policy *policy, int64_t now_ms, double slip_ppm,
                              int64_t window_ms, bool valid, bool accuracy_reported,
                              int64_t accuracy_ns, bool link_timestamps)
{
    aec_decision decision = { AEC_ACTION_NONE, 0 };

    /* THE ADMISSION RULE, AND WHY IT IS ARITHMETIC RATHER THAN A CONSTANT.
     * The slip is a ratio of two per-stream ratios, each taken between two
     * endpoints, so FOUR timing errors of `a` enter it: the resolvable slip is
     * 4a/T, not 2a/T. The earlier version of this rule counted two and
     * therefore overstated the margin. `accuracy_report == 0` means the
     * accuracy field is MEANINGLESS, so it has to be checked before the number
     * it guards is used at all -- a driver that reports garbage accuracy would
     * otherwise pass a rule written to keep it out. */
    bool admissible = valid && accuracy_reported && link_timestamps && window_ms > 0;
    if (admissible) {
        /* 4a nanoseconds over T milliseconds, expressed in ppm:
         *   4a[ns] / (T[ms] * 1e6 ns/ms) is a fraction; * 1e6 makes it ppm.
         * At a = 1000 ns and T = 10 s that is 0.4 ppm, a two-and-a-half-fold
         * margin on the 1 ppm the controller acts at -- thinner than the
         * five-fold an earlier draft claimed, and the real figure. */
        double resolvable_ppm = (4.0 * (double)accuracy_ns) / ((double)window_ms * 1.0e6) * 1.0e6;
        /* `>=`, not `>`: a window that resolves EXACTLY the figure the
         * controller acts at gives no margin at all, and the whole purpose of
         * this rule is margin. 2500 ns over 10 s lands on exactly 1 ppm and is
         * therefore refused; the documented fallback is the longer window. */
        if (resolvable_ppm >= AEC_DRIFT_RETUNE_PPM) admissible = false;
    }
    if (!admissible) {
        /* NEVER RUN THE CONTROLLER ON A NUMBER THAT FAILED ADMISSION. The
         * status says so and the reason is journalled by the shell once. */
        policy->drift_state = AEC_DRIFT_UNMEASURABLE;
        return decision;
    }

    policy->drift_measured_ppm = slip_ppm;
    policy->drift_updates += 1;
    if (policy->drift_started_ms == INT64_MIN) {
        policy->drift_started_ms = now_ms;
        policy->drift_ppm = slip_ppm;
    } else {
        /* A leaky integrator with a ~5 minute time constant. It is tracking an
         * oscillator offset, not a signal: anything faster chases noise. */
        double alpha = 1.0 / AEC_DRIFT_TIME_CONSTANT_UPDATES;
        policy->drift_ppm += alpha * (slip_ppm - policy->drift_ppm);
    }

    if (now_ms - policy->drift_started_ms < AEC_DRIFT_ACTIVATION_MS) {
        /* Before activation the ratio is held at unity and the measurement runs
         * anyway, which is also how the overnight capture collects its data. */
        policy->drift_state = AEC_DRIFT_MEASURING;
        return decision;
    }

    /* BOUNDED, AND LOUD OUTSIDE THE BOUND. Two independent crystals differ by
     * tens of ppm at worst, so a demand past 100 ppm is not drift -- it is a
     * misconfigured rate, a device that reopened at a different rate, or an
     * unnoticed overrun. Resampling further would hide a fault. */
    if (policy->drift_ppm > AEC_DRIFT_CLAMP_PPM || policy->drift_ppm < -AEC_DRIFT_CLAMP_PPM) {
        policy->drift_state = AEC_DRIFT_CLAMPED;
        policy->adapting = false;
        policy->state = AEC_STATE_DEGRADED;
        decision.action = AEC_ACTION_FAILOVER;
        return decision;
    }

    policy->drift_state = AEC_DRIFT_ACTIVE;
    double moved = policy->drift_ppm - policy->drift_applied_ppm;
    if (moved < 0.0) moved = -moved;
    if (moved <= AEC_DRIFT_RETUNE_PPM) return decision;

    /* set_rate_frac recomputes filter tables, so it is called at most once per
     * update interval and only when the ask has actually moved. */
    policy->drift_applied_ppm = policy->drift_ppm;
    decision.action = AEC_ACTION_SET_RATE;
    /* Numerator above denominator when the tap is FAST, which is the input side
     * running quicker, so more reference frames are consumed than produced. If
     * an implementation produces more than it consumes at positive slip, the
     * arguments have been transposed. */
    decision.ratio_ppm = (int32_t)(1000000.0 + (policy->drift_ppm < 0
        ? -floor(-policy->drift_ppm + 0.5) : floor(policy->drift_ppm + 0.5)));
    return decision;
}

/* ── names and the status block ──────────────────────────────────────────── */

const char *aec_state_name(aec_state state)
{
    switch (state) {
    case AEC_STATE_NO_PROFILE: return "no-profile";
    case AEC_STATE_IDLE: return "idle";
    case AEC_STATE_CONVERGING: return "converging";
    case AEC_STATE_INSUFFICIENT_DATA: return "insufficient-data";
    case AEC_STATE_CANCELLING: return "cancelling";
    case AEC_STATE_DEGRADED: return "degraded";
    case AEC_STATE_BYPASSED: return "bypassed";
    }
    return "degraded";
}

const char *aec_drift_state_name(aec_drift_state state)
{
    switch (state) {
    case AEC_DRIFT_MEASURING: return "measuring";
    case AEC_DRIFT_ACTIVE: return "active";
    case AEC_DRIFT_CLAMPED: return "clamped";
    case AEC_DRIFT_UNMEASURABLE: return "unmeasurable";
    }
    return "unmeasurable";
}

size_t aec_policy_status(const aec_policy *policy, const char *updated_utc,
                         char *out, size_t size)
{
    char buffer[2048];
    /* ERLE IS PUBLISHED ONLY WHEN IT IS DEFINED. `insufficient-data` publishes
     * `null`, not a number built from a handful of frames, because a plausible
     * wrong number is worse on a status block than an honest absence. */
    char erle_1min[32], erle_session[32];
    if (policy->erle_1min_valid) snprintf(erle_1min, sizeof(erle_1min), "%.1f", policy->erle_db_1min);
    else snprintf(erle_1min, sizeof(erle_1min), "null");
    if (policy->erle_n_session > 0) snprintf(erle_session, sizeof(erle_session), "%.1f", policy->erle_db_session);
    else snprintf(erle_session, sizeof(erle_session), "null");

    /* Item L's block, and the states that must not retain a misleading live
     * ring: `muted` while the switch mutes the input, `unavailable` while there
     * is no stream to meter. Freshness is published so the renderer can decide
     * `stale` for itself rather than trusting this file's age alone. */
    const char *level_state = policy->level_valid ? "live"
        : (policy->adapting ? "muted" : "unavailable");

    int written = snprintf(buffer, sizeof(buffer),
        "{\n"
        "  \"schema\": 1,\n"
        "  \"state\": \"%s\",\n"
        "  \"erle_db_1min\": %s,\n"
        "  \"erle_db_session\": %s,\n"
        "  \"erle_at_retune_db\": %.1f,\n"
        "  \"erle_floor_db\": %.1f,\n"
        "  \"profile\": \"%s\",\n"
        "  \"profile_age_days\": %d,\n"
        "  \"predelay_ms\": %.1f,\n"
        "  \"filter_length_ms\": %.1f,\n"
        "  \"mic_peak_dbfs_1min\": %.1f,\n"
        "  \"far_end_active_frames_1min\": %lld,\n"
        "  \"double_talk_frames_1min\": %lld,\n"
        "  \"adaptation\": \"%s\",\n"
        "  \"resets_this_session\": %d,\n"
        "  \"xruns_this_session\": %d,\n"
        "  \"drift_ppm\": %.2f,\n"
        "  \"drift_correction\": \"%s\",\n"
        "  \"microphone\": {\n"
        "    \"level\": %.4f,\n"
        "    \"source\": \"aec_post_filter\",\n"
        "    \"state\": \"%s\",\n"
        "    \"valid\": %s,\n"
        "    \"reference_dbfs\": %.1f,\n"
        "    \"observed_monotonic_ms\": %lld\n"
        "  },\n"
        "  \"updated_utc\": \"%s\"\n"
        "}\n",
        aec_state_name(policy->state), erle_1min, erle_session,
        policy->profile.erle_at_retune_db, policy->profile.erle_floor_db,
        policy->profile.name, (int)policy->profile.age_days,
        policy->profile.predelay_ms, policy->profile.filter_length_ms,
        policy->mic_peak_dbfs_1min,
        (long long)policy->far_active_frames_1min,
        (long long)policy->double_talk_frames_1min,
        policy->adapting ? "running" : "stopped",
        (int)policy->resets_this_session, (int)policy->xruns_this_session,
        policy->drift_ppm, aec_drift_state_name(policy->drift_state),
        policy->level_valid ? policy->level : 0.0,
        level_state, policy->level_valid ? "true" : "false",
        AEC_LEVEL_REFERENCE_DBFS, (long long)policy->level_updated_ms,
        updated_utc);

    if (written <= 0 || (size_t)written >= sizeof(buffer)) return 0;
    if ((size_t)written + 1 > size) return 0;
    memcpy(out, buffer, (size_t)written + 1);
    return (size_t)written;
}

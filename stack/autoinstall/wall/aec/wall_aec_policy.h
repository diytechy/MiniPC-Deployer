/* wall_aec_policy.h -- the pure decision core of the panel's echo canceller.
 *
 * ONE RESPONSIBILITY: given per-block signal measurements and a clock, decide
 * what the canceller should DO and what it should SAY. It opens no device,
 * reads no file, allocates nothing and calls into neither ALSA nor SpeexDSP --
 * so every rule in the spike's section 6.4 and 6.6 (reset on divergence, the
 * give-up bound, far-end-gated ERLE, the failover floor, the drift controller's
 * admission rule) is exhaustively unit-testable on a machine with no sound card
 * and no panel.
 *
 * The thin shell that owns the PCMs, the SpeexDSP state and the status file is
 * `wall-audio-aec.c`; the only thing it is allowed to decide is when to call in
 * here. Reading order: aec_profile, then aec_policy_block, then aec_policy_status.
 *
 * Design of record: HomeHub docs/AEC_SPIKE_2026-09-14.md section 6.
 * Telemetry contract: OfficeWallNaglight
 *   docs/design/audio-intent-contract-2026-09-14.md section 4.
 *
 * Implements: SR-028, LLR-016
 */

#ifndef WALL_AEC_POLICY_H
#define WALL_AEC_POLICY_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* ── fixed geometry ──────────────────────────────────────────────────────── */

#define AEC_RATE_HZ 48000
/* 256 samples, 5.33 ms. The spike's figure; it is the block every rate below is
 * expressed in, so changing it changes the meaning of the counters. */
#define AEC_FRAME_SIZE 256

/* Conservative defaults for a missing or unparseable profile. The spike is
 * explicit that this is NOT fatal: the daemon runs, passes the microphone
 * through until ERLE is above the floor, and says `no-profile` out loud. */
#define AEC_DEFAULT_PREDELAY_MS 40.0
#define AEC_DEFAULT_FILTER_MS 192.0
#define AEC_DEFAULT_ERLE_FLOOR_DB 12.0

/* Bounds the profile is clamped into. A profile is configuration read from
 * disk, and configuration that can set the filter to 4 seconds or the pre-delay
 * past the bulk delay is a way to break the canceller with a text editor. The
 * pre-delay rule is the spike's own and is the single most important line in
 * the profile: overshooting costs 6 dB of ERLE that adaptation cannot recover,
 * while undershooting costs only a little filter length. */
#define AEC_FILTER_MIN_MS 64.0
#define AEC_FILTER_MAX_MS 256.0
#define AEC_PREDELAY_MIN_MS 0.0
#define AEC_PREDELAY_MAX_MS 200.0
#define AEC_PREDELAY_MARGIN_MS 4.0

/* ── the failover and adaptation policy (spike 6.4) ──────────────────────── */

/* Far-end-active frames of grace after every (re-)init before any baseline is
 * recorded or any reset may fire. Cold convergence is about 5.5 s; 60 s of
 * qualifying frames is the spike's figure and is what stops a reset's own
 * convergence from triggering the next reset. */
#define AEC_WARMUP_QUALIFYING_FRAMES 11250   /* 60 s at 5.33 ms/block */
/* How far below the SESSION baseline, for how long, before the filter is
 * thrown away. Against the session baseline and not against the retune figure:
 * `erle_at_retune_db` came from a controlled stimulus at a known level, so
 * comparing live ERLE to it fires on a room that is merely different. */
#define AEC_DIVERGENCE_DB 6.0
#define AEC_DIVERGENCE_QUALIFYING_FRAMES 11250
/* At most one reset per 5 minutes, and three consecutive fruitless resets end
 * the matter: the daemon stops resetting and declares `degraded` rather than
 * looping. */
#define AEC_RESET_COOLDOWN_MS 300000
#define AEC_MAX_CONSECUTIVE_RESETS 3
/* The absolute usability gate, and the ONLY place a profile number reaches the
 * failover logic. Below the floor for this long and the mic legs go to
 * push-to-talk whatever the baselines say. */
#define AEC_FLOOR_QUALIFYING_FRAMES 11250
/* The time constant of the recent-ERLE estimator, in qualifying frames. 60 s
 * at 5.33 ms per block, matching the warm-up grace: the thing being measured is
 * how well the filter is doing NOW, and a faster estimator would fire on one
 * awkward passage of music. */
#define AEC_RECENT_TAU_FRAMES 11250.0

/* Far-end activity threshold, dBFS RMS on the reference. Below this the tap is
 * silent for our purposes: no ERLE is defined, nothing adapts, and the state is
 * `idle`. */
#define AEC_FAR_ACTIVE_DBFS -50.0
/* Double talk: the near end carries materially more than the far end can
 * explain. Declared when the mic exceeds the reference by more than this after
 * allowing for the echo path's own loss. Deliberately crude -- it is a gate on
 * a statistic, not a speech detector -- and its errors are bounded by the fact
 * that a false positive only PAUSES adaptation. */
#define AEC_DOUBLE_TALK_MARGIN_DB 6.0

/* ── the drift controller (spike 6.5) ────────────────────────────────────── */

#define AEC_DRIFT_WINDOW_MS 10000
#define AEC_DRIFT_LONG_WINDOW_MS 100000
#define AEC_DRIFT_ACCURACY_NS 1000
#define AEC_DRIFT_LONG_ACCURACY_NS 2500
#define AEC_DRIFT_ACTIVATION_MS 600000      /* 10 minutes of measurement */
#define AEC_DRIFT_CLAMP_PPM 100.0
#define AEC_DRIFT_RETUNE_PPM 1.0            /* don't recompute filters for less */
#define AEC_DRIFT_TIME_CONSTANT_UPDATES 30.0 /* ~5 minutes at one update per 10 s */

/* ── item L: the microphone level scalar ─────────────────────────────────── */

/* The documented reference the renderer's ring is linear in. See the telemetry
 * contract, section 4: `level` is linear in DECIBELS, not amplitude, and 1.0
 * means "at or above a person speaking at the panel at a normal level". */
#define AEC_LEVEL_FLOOR_DBFS -60.0
#define AEC_LEVEL_REFERENCE_DBFS -18.0
/* Health arithmetic must retain energy below the UI's visible floor. The room
 * measurement put microphone noise near -67 dBFS; clamping at -60 made a
 * quiet echo and a cancelled residual identical and falsely reported 0 ERLE. */
#define AEC_SIGNAL_FLOOR_DBFS -96.0
/* Bounded smoothing and update rate, so the wall cannot be made to flicker at
 * block rate by a noisy room. 50 ms samples with a 75 ms time constant keep
 * the indication legible while removing the old half-second-feeling lag. */
#define AEC_LEVEL_TIME_CONSTANT_MS 75.0
#define AEC_LEVEL_UPDATE_INTERVAL_MS 50

/* ── types ───────────────────────────────────────────────────────────────── */

/* The geometry a profile contributes, and nothing else. NO GAIN, LEVEL OR
 * FILTER COEFFICIENT IS EVER LOADED FROM DISK INTO THE AUDIO PATH: pre-delay
 * and filter length are fixed by where the speakers and the microphone are and
 * do not change when anybody turns a volume knob, which is the distinction the
 * Owner's ruling 1 draws and the reason a stored profile is compatible with
 * closing the loop at all. */
typedef struct {
    bool present;              /* false => running on the defaults below */
    double predelay_ms;
    double filter_length_ms;
    double bulk_delay_ms;      /* 0 when unknown; only used to check predelay */
    double erle_floor_db;
    double erle_at_retune_db;  /* reported beside the live figure, never acted on */
    char name[128];            /* the profile file's basename, for the status */
    int32_t age_days;          /* -1 when unknown */
} aec_profile;

typedef enum {
    AEC_STATE_NO_PROFILE = 0,
    AEC_STATE_IDLE,
    AEC_STATE_CONVERGING,
    AEC_STATE_INSUFFICIENT_DATA,
    AEC_STATE_CANCELLING,
    AEC_STATE_DEGRADED,
    AEC_STATE_BYPASSED
} aec_state;

typedef enum {
    AEC_DRIFT_MEASURING = 0,
    AEC_DRIFT_ACTIVE,
    AEC_DRIFT_CLAMPED,
    AEC_DRIFT_UNMEASURABLE
} aec_drift_state;

/* What the shell must do after a block. Never more than one thing. */
typedef enum {
    AEC_ACTION_NONE = 0,
    AEC_ACTION_RESET_FILTER,   /* destroy and re-init the echo state */
    AEC_ACTION_FAILOVER,       /* below the floor: hand the mic legs to push-to-talk */
    AEC_ACTION_SET_RATE        /* call speex_resampler_set_rate_frac with ratio_ppm */
} aec_action;

/* One block's measurements, all RMS amplitudes in dBFS. The shell computes
 * these; the policy never sees a sample. */
typedef struct {
    int64_t now_ms;            /* CLOCK_MONOTONIC milliseconds */
    double far_dbfs;           /* the reference (speaker_tap), after the pre-delay */
    double mic_dbfs;           /* the near end, before cancellation */
    double residual_dbfs;      /* the near end after cancellation -- item L's source */
    double mic_peak_dbfs;      /* this block's peak, for the clipping warning */
    /* The switch's EFFECTIVE input mute (item J), as published by the applier.
     * It must reach here, or the status block would go on saying the microphone
     * is live while the applier has stopped every leg -- and item L's ring
     * would be drawn over a coupled mute (terra, 2026-09-14). */
    bool input_muted;
    bool tap_present;          /* false when the speaker leg is not running at all */
} aec_block;

typedef struct {
    aec_action action;
    /* Only meaningful for AEC_ACTION_SET_RATE: the numerator for
     * speex_resampler_set_rate_frac(st, ratio_ppm, 1000000, 48000, 48000).
     * Above 1000000 means the tap runs FAST and the reference is resampled
     * down, which is the direction the spike spells out because it is easy to
     * invert. */
    int32_t ratio_ppm;
} aec_decision;

typedef struct {
    aec_profile profile;

    /* adaptation and health */
    aec_state state;
    bool adapting;
    bool failed_over;
    double erle_sum_1min, erle_sum_session;
    int64_t erle_n_1min, erle_n_session;
    /* THE STATISTIC THE HEALTH RULES ACT ON, and it is deliberately NOT the
     * session mean (terra, 2026-09-14). A lifetime average is diluted without
     * bound by however long the room has been fine: after an hour at 30 dB, a
     * complete failure takes a further quarter of an hour to drag the mean
     * below the divergence threshold, and the 60-second counters cannot bound
     * that because they only start once it has. This is an exponential moving
     * average over qualifying frames with a ~60 s time constant, so detection
     * latency is a property of the filter and not of the uptime. The session
     * mean is kept, and is used for REPORTING and for the baseline only. */
    double erle_recent_db;
    bool erle_recent_valid;
    int64_t window_started_ms;
    double erle_db_1min, erle_db_session;
    bool erle_1min_valid;
    double mic_peak_dbfs_1min;
    int64_t far_active_frames_1min, double_talk_frames_1min;

    int64_t qualifying_frames;       /* since the last (re-)init */
    double session_baseline_db;
    /* The baseline the last reset was trying to recover, kept across the
     * re-arm. See aec_policy_block: whether a reset HELPED can only be judged
     * once the replacement filter has settled and taken its own baseline. */
    double baseline_before_reset_db;
    bool baseline_valid;
    int64_t below_baseline_frames;
    int64_t below_floor_frames;
    int32_t resets_this_session;
    int32_t consecutive_resets;
    int64_t last_reset_ms;           /* INT64_MIN when never */
    int32_t xruns_this_session;

    /* drift */
    aec_drift_state drift_state;
    double drift_ppm;                /* the filtered estimate */
    double drift_measured_ppm;       /* the last admitted raw measurement */
    double drift_applied_ppm;        /* what was last handed to the resampler */
    int64_t drift_started_ms;        /* INT64_MIN until the first admitted sample */
    int64_t drift_updates;

    /* item L */
    double level;                    /* [0,1], smoothed */
    bool level_valid;
    int64_t level_updated_ms;
    int64_t last_block_ms;
} aec_policy;

/* ── the pure API ────────────────────────────────────────────────────────── */

/** Conservative defaults. Call before anything else. */
void aec_profile_defaults(aec_profile *profile);

/** Clamp a profile into the bounds above, in place. Returns false if anything
 *  had to be changed, so the caller can journal it. A profile is never
 *  rejected outright: the daemon must run on a damaged one, loudly. */
bool aec_profile_clamp(aec_profile *profile);

/** Start of life, or a full re-init after an xrun. `now_ms` seeds the clocks. */
void aec_policy_init(aec_policy *policy, const aec_profile *profile, int64_t now_ms);

/** Re-arm everything that a filter re-init invalidates, WITHOUT losing the
 *  session counters. The warm-up grace restarts from here, which is what stops
 *  a reset's own cold convergence from firing the next one. */
void aec_policy_rearm(aec_policy *policy, int64_t now_ms);

/** Fold one block. Returns at most one action for the shell to perform. */
aec_decision aec_policy_block(aec_policy *policy, const aec_block *block);

/** Report an xrun on either PCM. The shell has already resynchronised both
 *  streams; this records it and restarts the drift baseline, because the
 *  timestamp baseline is gone. */
void aec_policy_xrun(aec_policy *policy, int64_t now_ms);

/** One drift measurement, with its admission evidence.
 *
 *  `slip_ppm` is (tap_ratio / mic_ratio - 1) * 1e6 over `window_ms`. It is used
 *  ONLY if both streams reported a valid, accuracy-reported timestamp whose
 *  accuracy resolves the 1 ppm the controller acts at over that window:
 *  four timing errors enter the ratio of two per-stream ratios, so the
 *  resolvable slip is 4a/T and not 2a/T -- the spike is explicit that counting
 *  two overstates the margin.
 *
 *  Returns the decision, which is AEC_ACTION_SET_RATE only when the filtered
 *  estimate has moved by more than AEC_DRIFT_RETUNE_PPM since the last call:
 *  set_rate_frac recomputes filter tables and must not be called per block.
 */
aec_decision aec_policy_drift(aec_policy *policy, int64_t now_ms, double slip_ppm,
                              int64_t window_ms, bool valid, bool accuracy_reported,
                              int64_t accuracy_ns, bool link_timestamps);

/** The word for the status block and for the Settings pane. */
const char *aec_state_name(aec_state state);
const char *aec_drift_state_name(aec_drift_state state);

/** Serialize /run/wall-panel/aec-status.json into `out`. Returns the number of
 *  bytes written, or 0 if the buffer was too small (nothing is written then).
 *  `updated_utc` is formatted by the caller, which owns the clock. */
size_t aec_policy_status(const aec_policy *policy, const char *updated_utc,
                         char *out, size_t size);

/** dBFS of an RMS amplitude in [0,1], floored at AEC_SIGNAL_FLOOR_DBFS so a
 *  digital-silence block cannot produce -inf and poison every average. */
double aec_dbfs(double amplitude);

/** Item L's normalization, exactly as the telemetry contract documents it. */
double aec_level_from_dbfs(double dbfs);

#endif /* WALL_AEC_POLICY_H */

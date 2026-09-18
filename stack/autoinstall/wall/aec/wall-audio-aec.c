/* wall-audio-aec -- the panel's acoustic echo canceller. Step 6 of item 23.
 *
 * THE THIN SHELL. Every decision lives in wall_aec_policy (pure, unit-tested);
 * this file only opens PCMs, drives SpeexDSP, writes the status file and
 * journals. If you find yourself adding an `if` about what a state MEANS, it
 * belongs next door -- the same rule wall-audio-output follows.
 *
 * WHERE IT SITS (spike section 6.1):
 *
 *   bus_monitor -> speaker_tap_mix -> speaker_tap -> speaker_out -> amp -> room
 *                                        |                                  |
 *                          far-end reference                    acoustic return
 *                                        v                                  v
 *                           +-----------------------------+      hw:PCH,0 (ALC255)
 *                           |   wall-audio-aec (this)     |<-----------------+
 *                           |   speex_echo_cancellation() |
 *                           +-----------------------------+
 *                                        |  cancelled mono, 48 kHz
 *                                        v
 *                              card_loop_mic_play  (Loopback dev 0, subdev 2)
 *                                        v
 *                              pcm.mic_clean  (dsnoop on Loopback dev 1, subdev 2)
 *                                        v
 *                              the mic legs read `mic_clean`, never `hw:PCH,0`
 *
 * FOUR THINGS THE IMPLEMENTER MUST NOT GET WRONG, all from the spike:
 *
 *  1. BOTH PCMs ARE OPENED FROM ONE PROCESS. Two processes gave a 65 ms
 *     uncertainty in alignment; one process gave 0.084 ms. The daemon reads the
 *     tap and the microphone itself and delegates neither to an `alsaloop`.
 *  2. THE DAEMON OWNS THE RAW MICROPHONE EXCLUSIVELY. Every consumer reads
 *     `mic_clean`. That is what makes the canceller impossible to bypass by
 *     accident.
 *  3. IT SURVIVES THE TAP GOING AWAY, which happens every time the switch
 *     leaves Speaker. On a silent or absent tap it passes the microphone
 *     through unchanged and freezes adaptation. There is no mode logic: like
 *     the amplifier detector, the behaviour falls out of the tap.
 *  4. THE MICROPHONE IS THE MASTER CLOCK. Its output is what `mic_clean`
 *     publishes at a fixed rate, so the loop is paced by microphone reads and
 *     the REFERENCE is what gets rate-corrected. Putting the conversion in the
 *     path of the audio people listen to would be the wrong way round.
 *
 * Usage:
 *   wall-audio-aec [--profile PATH] [--status PATH]
 *                  [--mic PCM] [--tap PCM] [--out PCM]
 *                  [--replay REF.raw NEAR.raw]   offline, see below
 *
 * `--replay` runs the whole cancelling and health path over two raw S16_LE mono
 * 48 kHz files instead of the sound card, writing the cancelled result to
 * stdout and the status to --status. It is how the spike's retained captures
 * are replayed on a machine with no panel, and it shares every line of the
 * live path except the ALSA calls.
 *
 * Config:  /etc/wall-panel/aec-profile.json   the dated room profile (symlink)
 *          /run/wall-panel/aec-status.json    the health block it publishes
 * Exit:    0 on a clean stop, 1 on a fatal device error, 2 usage.
 * Implements: SR-028, LLR-016
 */

#define _POSIX_C_SOURCE 200809L

#include "wall_aec_policy.h"
#include "wall_aec_profile.h"
#include "wall_aec_pcm.h"
#ifndef WALL_AEC_OFFLINE_ONLY
#include "wall_aec_status.h"
#endif

#include <alloca.h>
#include <errno.h>
#include <stdarg.h>
#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#ifndef WALL_AEC_OFFLINE_ONLY
#include <alsa/asoundlib.h>
#endif
#include <speex/speex_echo.h>
#include <speex/speex_preprocess.h>
#include <speex/speex_resampler.h>

/* THE APPLIER'S EFFECTIVE INPUT MUTE, published at every apply. It has to
 * reach this daemon or the status block would go on saying the microphone is
 * live while item J has stopped every leg, and item L's ring would be drawn
 * over a coupled mute (terra, 2026-09-14). A one-line env file rather than the
 * state JSON: it is read on a timer in an audio loop, and parsing JSON there to
 * learn one boolean would be work paid for over and over. */
#define DEFAULT_MUTE_FILE "/run/wall-panel/audio-input-mute.env"
#define MUTE_POLL_MS 1000
/* How often to try opening a tap that was not there at start. The switch can be
 * moved to Speaker at any time, and a daemon that gave up on the first failed
 * open would pass the microphone through for the rest of its life. */
#define TAP_RETRY_MS 2000
#define DEFAULT_PROFILE "/etc/wall-panel/aec-profile.json"
#define DEFAULT_STATUS "/run/wall-panel/aec-status.json"
#define DEFAULT_MIC "hw:PCH,0"
#define DEFAULT_TAP "speaker_tap"
#define DEFAULT_OUT "card_loop_mic_play"
/* The panel polls the broker four times a second and expires a microphone
 * observation after 1.5 seconds. Publish at the same 4 Hz cadence so a live
 * post-AEC level remains visibly continuous instead of appearing for only the
 * first 1.5 seconds of each old ten-second status period. `/run` is tmpfs. */
#define STATUS_INTERVAL_MS 250
/* At least four blocks, so a ratio change can never starve the reference ring
 * mid-block (spike 6.5, block accounting). */
#define REF_RING_BLOCKS 8
#define REF_RING_FRAMES (AEC_FRAME_SIZE * REF_RING_BLOCKS)

static volatile sig_atomic_t stopping = 0;
static void on_signal(int signal) { (void)signal; stopping = 1; }

static void journal(const char *format, ...)
    __attribute__((format(printf, 1, 2)));

static void journal(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    vfprintf(stderr, format, args);
    va_end(args);
    fputc('\n', stderr);
    fflush(stderr);
}

static int64_t monotonic_ms(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static void utc_now(char *out, size_t size)
{
    time_t seconds = time(NULL);
    struct tm parts;
    gmtime_r(&seconds, &parts);
    strftime(out, size, "%Y-%m-%dT%H:%M:%SZ", &parts);
}

/* The applier's effective input mute, or false when it has never said.
 *
 * FALSE ON ABSENCE IS RIGHT HERE and is not the usual cautious default: this
 * daemon does not GATE anything on the answer -- the applier has already
 * stopped the mic legs, so no sample leaves the panel either way -- it only
 * decides whether the status block claims a live level. An absent file is a
 * panel whose applier predates item J, and reporting `live` there is exactly
 * what such a panel does.
 *
 * Only the live path consults it: a replay has no switch and no applier, so
 * there is no input mute to read.
 */
#ifndef WALL_AEC_OFFLINE_ONLY
static bool read_input_muted(const char *path)
{
    FILE *handle = fopen(path, "r");
    if (!handle) return false;
    char line[128];
    bool muted = false;
    while (fgets(line, sizeof(line), handle)) {
        const char *at = strstr(line, "WALL_AUDIO_INPUT_MUTED=");
        if (!at) continue;
        muted = at[strlen("WALL_AUDIO_INPUT_MUTED=")] == '1';
        break;
    }
    fclose(handle);
    return muted;
}
#endif /* WALL_AEC_OFFLINE_ONLY */

/* RMS and peak of one block, as amplitudes in [0,1]. The policy takes dBFS and
 * never sees a sample; this is the whole of the conversion. */
static void measure(const int16_t *samples, int count, double *rms, double *peak)
{
    double sum = 0.0;
    int32_t high = 0;
    for (int i = 0; i < count; i++) {
        double value = (double)samples[i] / 32768.0;
        sum += value * value;
        int32_t magnitude = samples[i] < 0 ? -(int32_t)samples[i] : samples[i];
        if (magnitude > high) high = magnitude;
    }
    *rms = count > 0 ? sqrt(sum / (double)count) : 0.0;
    *peak = (double)high / 32768.0;
}

/* One atomic status write, so a reader never sees half a document. */
static void publish(const aec_policy *policy, const char *path)
{
    char utc[32];
    char body[2048];
    utc_now(utc, sizeof(utc));
    size_t written = aec_policy_status(policy, utc, body, sizeof(body));
    if (!written) return;
    char temporary[512];
    if ((size_t)snprintf(temporary, sizeof(temporary), "%s.new", path) >= sizeof(temporary)) return;
    FILE *handle = fopen(temporary, "w");
    if (!handle) return;
    fwrite(body, 1, written, handle);
    fflush(handle);
    fsync(fileno(handle));
    fclose(handle);
    if (rename(temporary, path) != 0) unlink(temporary);
}

/* ── the cancelling engine, shared by the live and replay paths ──────────── */

typedef struct {
    SpeexEchoState *echo;
    SpeexPreprocessState *preprocess;
    SpeexResamplerState *resampler;
    int filter_frames;
    int predelay_frames;
    int16_t *predelay_ring;      /* the profile's fixed pre-delay on the mic */
    int predelay_head;
    int16_t reference_ring[REF_RING_FRAMES];
    int reference_fill;
    int64_t reference_starved_blocks;
    aec_policy policy;
    char status_path[512];
    int64_t status_due_ms;
#ifndef WALL_AEC_OFFLINE_ONLY
    aec_status_publisher publisher;
#endif
} aec_engine;

static void engine_reset_filter(aec_engine *engine)
{
    /* speex_echo_state_reset() is the documented way to start from zero without
     * re-deriving the geometry. There is DELIBERATELY no stored filter state to
     * reload here: SpeexDSP's public API cannot serialise coefficients, and a
     * persisted filter would in any case encode the acoustic gain in force when
     * it was learned -- reloading it after somebody moved the amplifier knob
     * would start from a state the Owner's ruling says is invalid. Every start
     * is a cold start, and the profile contributes geometry only. */
    speex_echo_state_reset(engine->echo);
    int rate = AEC_RATE_HZ;
    speex_echo_ctl(engine->echo, SPEEX_ECHO_SET_SAMPLING_RATE, &rate);
    memset(engine->predelay_ring, 0, (size_t)engine->predelay_frames * sizeof(int16_t) + sizeof(int16_t));
    engine->predelay_head = 0;
}

static bool engine_open(aec_engine *engine, const aec_profile *profile, const char *status_path)
{
    memset(engine, 0, sizeof(*engine));
    snprintf(engine->status_path, sizeof(engine->status_path), "%s", status_path);
    engine->filter_frames = (int)(profile->filter_length_ms * AEC_RATE_HZ / 1000.0);
    engine->predelay_frames = (int)(profile->predelay_ms * AEC_RATE_HZ / 1000.0);
    engine->echo = speex_echo_state_init(AEC_FRAME_SIZE, engine->filter_frames);
    if (!engine->echo) return false;
    int rate = AEC_RATE_HZ;
    speex_echo_ctl(engine->echo, SPEEX_ECHO_SET_SAMPLING_RATE, &rate);
    /* The preprocessor is attached ONLY for its residual-echo suppression,
     * which needs the echo state. No denoise, no AGC: this signal ends up on
     * somebody's telephone call and the room's own character is not ours to
     * remove. */
    engine->preprocess = speex_preprocess_state_init(AEC_FRAME_SIZE, AEC_RATE_HZ);
    if (engine->preprocess)
        speex_preprocess_ctl(engine->preprocess, SPEEX_PREPROCESS_SET_ECHO_STATE, engine->echo);
    int error = 0;
    /* Quality 4 of 10: the ratio is within tens of ppm of unity, so this is a
     * near-identity resampler and spending CPU on quality it cannot use would
     * be paid on every block forever. */
    engine->resampler = speex_resampler_init(1, AEC_RATE_HZ, AEC_RATE_HZ, 4, &error);
    if (!engine->resampler) return false;
    engine->predelay_ring = calloc((size_t)engine->predelay_frames + 1, sizeof(int16_t));
    if (!engine->predelay_ring) return false;
#ifndef WALL_AEC_OFFLINE_ONLY
    if (!aec_status_publisher_start(&engine->publisher, status_path, publish)) return false;
#endif
    return true;
}

static void engine_close(aec_engine *engine)
{
#ifndef WALL_AEC_OFFLINE_ONLY
    aec_status_publisher_stop(&engine->publisher);
#endif
    if (engine->preprocess) speex_preprocess_state_destroy(engine->preprocess);
    if (engine->echo) speex_echo_state_destroy(engine->echo);
    if (engine->resampler) speex_resampler_destroy(engine->resampler);
    free(engine->predelay_ring);
}

/* Hold the microphone back by the profile's pre-delay, so the reference handed
 * to Speex is already advanced. A ring rather than a shift, because this runs
 * every 5.33 ms for the life of the panel. */
static void predelay(aec_engine *engine, const int16_t *in, int16_t *out, int count)
{
    if (engine->predelay_frames <= 0) { memcpy(out, in, (size_t)count * sizeof(int16_t)); return; }
    int size = engine->predelay_frames;
    for (int i = 0; i < count; i++) {
        int16_t held = engine->predelay_ring[engine->predelay_head];
        engine->predelay_ring[engine->predelay_head] = in[i];
        engine->predelay_head = (engine->predelay_head + 1) % size;
        out[i] = held;
    }
}

/* Produce exactly AEC_FRAME_SIZE reference frames from the ring, through the
 * resampler. Returns how many it actually produced.
 *
 * BLOCK ACCOUNTING, because the resampler is the one place in this daemon where
 * input and output frame counts differ. `speex_resampler_process_int` updates
 * BOTH length arguments in place; the ring is advanced by the input count IT
 * REPORTS and never by an assumed figure, and whatever it did not consume is
 * kept -- that leftover is normal and is how a fractional ratio is carried
 * between blocks. The resampler is never reset between blocks: its internal
 * state is what makes the ratio continuous.
 */
static int pull_reference(aec_engine *engine, int16_t *out)
{
    spx_uint32_t produced = AEC_FRAME_SIZE;
    spx_uint32_t consumed = (spx_uint32_t)engine->reference_fill;
    if (consumed == 0) return 0;
    speex_resampler_process_int(engine->resampler, 0, engine->reference_ring, &consumed,
                                out, &produced);
    int left = engine->reference_fill - (int)consumed;
    if (left > 0) memmove(engine->reference_ring, engine->reference_ring + consumed,
                          (size_t)left * sizeof(int16_t));
    engine->reference_fill = left;
    return (int)produced;
}

static void push_reference(aec_engine *engine, const int16_t *frames, int count)
{
    int room = REF_RING_FRAMES - engine->reference_fill;
    if (count > room) {
        /* The ring is full: the tap is producing faster than the microphone is
         * consuming by more than the ring can absorb. Dropping the OLDEST is
         * the only choice that keeps alignment meaningful, and it is counted
         * rather than hidden. */
        int drop = count - room;
        if (drop > engine->reference_fill) drop = engine->reference_fill;
        memmove(engine->reference_ring, engine->reference_ring + drop,
                (size_t)(engine->reference_fill - drop) * sizeof(int16_t));
        engine->reference_fill -= drop;
        room = REF_RING_FRAMES - engine->reference_fill;
        if (count > room) count = room;
    }
    memcpy(engine->reference_ring + engine->reference_fill, frames,
           (size_t)count * sizeof(int16_t));
    engine->reference_fill += count;
}

/* One block, end to end. `tap_present` is false when nothing is feeding the
 * reference at all -- the switch has left Speaker -- and is what makes the
 * pass-through fall out of the tap rather than out of mode logic. */
static void engine_block(aec_engine *engine, const int16_t *mic, int16_t *out, bool tap_present,
                         bool input_muted, int64_t now_ms)
{
    int16_t delayed[AEC_FRAME_SIZE];
    int16_t reference[AEC_FRAME_SIZE];
    predelay(engine, mic, delayed, AEC_FRAME_SIZE);

    int produced = tap_present ? pull_reference(engine, reference) : 0;
    if (produced < AEC_FRAME_SIZE) {
        /* Zero-padded reference. A nonzero count means the ring is too small or
         * the tap has stopped -- not that the arithmetic is wrong. */
        memset(reference + produced, 0, (size_t)(AEC_FRAME_SIZE - produced) * sizeof(int16_t));
        if (tap_present) engine->reference_starved_blocks += 1;
    }

    /* `failed_over` IS CHECKED HERE, and leaving it out was a lie in the
     * journal (terra, second pass): the daemon logged "the microphone is passed
     * through" and then went on calling speex_echo_cancellation on the very
     * next block. Below the profile's ERLE floor the filter is doing more harm
     * than good -- it is subtracting an estimate it cannot make -- so it stops
     * subtracting anything and the microphone goes through untouched. */
    bool cancelling = tap_present && engine->policy.state != AEC_STATE_DEGRADED
        && !engine->policy.failed_over && engine->policy.adapting;
    if (cancelling) {
        speex_echo_cancellation(engine->echo, delayed, reference, out);
        if (engine->preprocess) speex_preprocess_run(engine->preprocess, out);
    } else {
        /* PASS THROUGH, NEVER SILENCE. The mic legs must not go quiet because
         * the canceller has an opinion; a degraded canceller is a worse
         * microphone, not an absent one. */
        memcpy(out, delayed, AEC_FRAME_SIZE * sizeof(int16_t));
    }

    double far_rms = 0.0, far_peak = 0.0, mic_rms = 0.0, mic_peak = 0.0, res_rms = 0.0, res_peak = 0.0;
    measure(reference, AEC_FRAME_SIZE, &far_rms, &far_peak);
    measure(delayed, AEC_FRAME_SIZE, &mic_rms, &mic_peak);
    measure(out, AEC_FRAME_SIZE, &res_rms, &res_peak);

    aec_block observation = {
        .now_ms = now_ms,
        .far_dbfs = aec_dbfs(far_rms),
        .mic_dbfs = aec_dbfs(mic_rms),
        .residual_dbfs = aec_dbfs(res_rms),
        .mic_peak_dbfs = aec_dbfs(mic_peak),
        .input_muted = input_muted,
        .tap_present = tap_present,
    };
    aec_decision decision = aec_policy_block(&engine->policy, &observation);
    switch (decision.action) {
    case AEC_ACTION_RESET_FILTER:
        journal("aec: ERLE diverged from the session baseline; re-initialising the filter "
                "(reset %d this session)", engine->policy.resets_this_session);
        engine_reset_filter(engine);
        break;
    case AEC_ACTION_FAILOVER:
        journal("aec: ERLE below the profile floor of %.1f dB; the microphone is passed "
                "through and the mic legs should be treated as push-to-talk",
                engine->policy.profile.erle_floor_db);
        break;
    case AEC_ACTION_SET_RATE:
        journal("aec: reference resampled at %d ppm (tap %s than the microphone)",
                decision.ratio_ppm - 1000000,
                decision.ratio_ppm > 1000000 ? "faster" : "slower");
        speex_resampler_set_rate_frac(engine->resampler, (spx_uint32_t)decision.ratio_ppm,
                                      1000000u, AEC_RATE_HZ, AEC_RATE_HZ);
        break;
    case AEC_ACTION_NONE:
        break;
    }

    if (now_ms >= engine->status_due_ms) {
#ifdef WALL_AEC_OFFLINE_ONLY
        publish(&engine->policy, engine->status_path);
#else
        (void)aec_status_publisher_try_enqueue(&engine->publisher, &engine->policy);
#endif
        engine->status_due_ms = now_ms + STATUS_INTERVAL_MS;
    }
}

/* ── the offline replay ──────────────────────────────────────────────────── */

/* The spike's retained captures, run through the very same engine. It shares
 * every line of the live path except the ALSA calls, which is the point: a
 * replay that exercised a separate code path would prove nothing about the
 * daemon. The clock is SYNTHETIC -- 5.33 ms per block -- so a replay is
 * deterministic and an hour of audio does not take an hour. */
static int replay(aec_engine *engine, const char *reference_path, const char *near_path)
{
    FILE *reference = fopen(reference_path, "rb");
    FILE *near = fopen(near_path, "rb");
    if (!reference || !near) {
        journal("aec: replay needs both captures (%s, %s): %s",
                reference_path, near_path, strerror(errno));
        if (reference) fclose(reference);
        if (near) fclose(near);
        return 1;
    }
    int16_t mic[AEC_FRAME_SIZE], tap[AEC_FRAME_SIZE], out[AEC_FRAME_SIZE];
    int64_t now_ms = 0;
    int64_t blocks = 0;
    while (!stopping) {
        if (fread(mic, sizeof(int16_t), AEC_FRAME_SIZE, near) != AEC_FRAME_SIZE) break;
        size_t got = fread(tap, sizeof(int16_t), AEC_FRAME_SIZE, reference);
        bool tap_present = got == AEC_FRAME_SIZE;
        if (tap_present) push_reference(engine, tap, AEC_FRAME_SIZE);
        /* A replay has no switch and no applier, so there is no input mute to
         * read. `false` is the truthful value for a capture being re-run from
         * a file rather than a default standing in for one. */
        engine_block(engine, mic, out, tap_present, false, now_ms);
        fwrite(out, sizeof(int16_t), AEC_FRAME_SIZE, stdout);
        /* 256 frames at 48 kHz is 16/3 ms. Accumulated from the block count so
         * a long replay does not drift against its own clock. */
        blocks += 1;
        now_ms = blocks * AEC_FRAME_SIZE * 1000 / AEC_RATE_HZ;
    }
    fclose(reference);
    fclose(near);
    publish(&engine->policy, engine->status_path);
    journal("aec: replayed %lld blocks (%.1f s); ERLE session %.1f dB over %lld qualifying "
            "frames, state %s, reference-starved blocks %lld",
            (long long)blocks, (double)now_ms / 1000.0,
            engine->policy.erle_db_session, (long long)engine->policy.erle_n_session,
            aec_state_name(engine->policy.state),
            (long long)engine->reference_starved_blocks);
    return 0;
}

/* ── the live path ───────────────────────────────────────────────────────── */

#ifndef WALL_AEC_OFFLINE_ONLY
typedef struct {
    snd_pcm_t *pcm;
    snd_pcm_sw_params_t *sw;
} alsa_pcm_context;

static int alsa_sw_current(void *opaque)
{
    alsa_pcm_context *context = opaque;
    return snd_pcm_sw_params_current(context->pcm, context->sw);
}

static int alsa_start_threshold(void *opaque, unsigned long value)
{
    alsa_pcm_context *context = opaque;
    return snd_pcm_sw_params_set_start_threshold(context->pcm, context->sw,
                                                  (snd_pcm_uframes_t)value);
}

static int alsa_avail_min(void *opaque, unsigned long value)
{
    alsa_pcm_context *context = opaque;
    return snd_pcm_sw_params_set_avail_min(context->pcm, context->sw,
                                           (snd_pcm_uframes_t)value);
}

static int alsa_sw_apply(void *opaque)
{
    alsa_pcm_context *context = opaque;
    return snd_pcm_sw_params(context->pcm, context->sw);
}

static int alsa_drop(void *opaque)
{
    return snd_pcm_drop(((alsa_pcm_context *)opaque)->pcm);
}

static int alsa_prepare(void *opaque)
{
    return snd_pcm_prepare(((alsa_pcm_context *)opaque)->pcm);
}

static int alsa_start(void *opaque)
{
    return snd_pcm_start(((alsa_pcm_context *)opaque)->pcm);
}

static aec_pcm_ops pcm_operations(alsa_pcm_context *context)
{
    aec_pcm_ops operations = {
        context, alsa_sw_current, alsa_start_threshold, alsa_avail_min,
        alsa_sw_apply, alsa_drop, alsa_prepare, alsa_start
    };
    return operations;
}

static snd_pcm_t *open_pcm(const char *name, snd_pcm_stream_t direction,
                           unsigned int channels)
{
    snd_pcm_t *pcm = NULL;
    int error = snd_pcm_open(&pcm, name, direction, 0);
    if (error < 0) { journal("aec: cannot open %s: %s", name, snd_strerror(error)); return NULL; }
    error = snd_pcm_set_params(pcm, SND_PCM_FORMAT_S16_LE, SND_PCM_ACCESS_RW_INTERLEAVED,
                               channels, AEC_RATE_HZ, 1, 50000);
    if (error < 0) {
        journal("aec: cannot configure %s: %s", name, snd_strerror(error));
        snd_pcm_close(pcm);
        return NULL;
    }
    if (direction == SND_PCM_STREAM_CAPTURE) {
        /* snd_pcm_set_params() chose start_threshold=buffer_size on the live
         * ALC255.  Unlike arecord's known-good start_threshold=1, that made
         * every first snd_pcm_readi() fail with EIO.  Capture starts when data
         * exists; playback is the direction that needs a fill threshold. */
        snd_pcm_sw_params_t *sw = NULL;
        snd_pcm_sw_params_alloca(&sw);
        alsa_pcm_context context = { pcm, sw };
        aec_pcm_ops operations = pcm_operations(&context);
        error = aec_pcm_configure_capture(&operations, AEC_FRAME_SIZE);
        if (error < 0) {
            journal("aec: cannot configure capture thresholds for %s: %s",
                    name, snd_strerror(error));
            snd_pcm_close(pcm);
            return NULL;
        }
    }
    return pcm;
}

static snd_pcm_t *open_reference(const char *name)
{
    snd_pcm_t *pcm = open_pcm(name, SND_PCM_STREAM_CAPTURE,
                              AEC_REFERENCE_CHANNELS);
    if (!pcm) return NULL;
    /* The reference is drained with avail_update() so it never makes the
     * blocking read that would implicitly start a prepared capture PCM.  The
     * first live run therefore waited forever at avail=0 while music crossed
     * the same tap. Start it explicitly, and do the same after every recovery. */
    alsa_pcm_context context = { pcm, NULL };
    aec_pcm_ops operations = pcm_operations(&context);
    int error = aec_pcm_start_reference(&operations);
    if (error < 0) {
        journal("aec: cannot start reference %s: %s", name, snd_strerror(error));
        snd_pcm_close(pcm);
        return NULL;
    }
    return pcm;
}

static bool restart_capture(snd_pcm_t *pcm, const char *name)
{
    /* drop() may legitimately report that an already-faulted stream is not
     * running. prepare() and start() are the operations that decide whether
     * the handle is usable again, so check both of those and make failure
     * visible rather than leaving an inert non-NULL PCM in the live loop. */
    alsa_pcm_context context = { pcm, NULL };
    aec_pcm_ops operations = pcm_operations(&context);
    int error = aec_pcm_restart_capture(&operations);
    if (error < 0) {
        journal("aec: cannot restart capture %s: %s", name, snd_strerror(error));
        return false;
    }
    return true;
}

/* Read a stream's audio and system timestamps in one pass, so the monotonic
 * interval the slip is computed over is SHARED between the two streams. */
typedef struct {
    bool usable;
    double audio_seconds;
    double system_seconds;
    int64_t accuracy_ns;
    bool accuracy_reported;
    bool link;
} stream_clock;

static stream_clock read_clock(snd_pcm_t *pcm)
{
    stream_clock value = { false, 0.0, 0.0, 0, false, false };
    snd_pcm_status_t *status = NULL;
    snd_pcm_status_alloca(&status);
    if (snd_pcm_status(pcm, status) < 0) return value;
    snd_htimestamp_t audio, system;
    snd_pcm_status_get_audio_htstamp(status, &audio);
    snd_pcm_status_get_htstamp(status, &system);
#ifdef SND_PCM_AUDIO_TSTAMP_TYPE_LINK
    snd_pcm_audio_tstamp_report_t report;
    memset(&report, 0, sizeof(report));
    snd_pcm_status_get_audio_htstamp_report(status, &report);
    /* A ZERO accuracy_report MEANS THE ACCURACY FIELD IS MEANINGLESS, so it has
     * to be checked before the number it guards is used at all. */
    value.accuracy_reported = report.accuracy_report != 0;
    value.accuracy_ns = (int64_t)report.accuracy;
    value.usable = report.valid != 0;
    value.link = report.actual_type == SND_PCM_AUDIO_TSTAMP_TYPE_LINK;
#endif
    value.audio_seconds = (double)audio.tv_sec + (double)audio.tv_nsec / 1e9;
    value.system_seconds = (double)system.tv_sec + (double)system.tv_nsec / 1e9;
    return value;
}

static int live(aec_engine *engine, const char *mic_name, const char *tap_name,
                const char *out_name, const char *mute_name)
{
    /* The ALC255 capture endpoint is stereo-only even though the internal mic
     * is one acoustic source.  The first live start on 2026-09-18 proved that
     * asking hw:PCH,0 for one channel fails with EINVAL before a frame reaches
     * the canceller.  Own the hardware in its native two-channel shape and
     * downmix explicitly below; the tap and clean loopback stay mono. */
    snd_pcm_t *mic_pcm = open_pcm(mic_name, SND_PCM_STREAM_CAPTURE,
                                  AEC_MIC_CHANNELS);
    if (!mic_pcm) return 1;
    snd_pcm_t *out_pcm = open_pcm(out_name, SND_PCM_STREAM_PLAYBACK,
                                  AEC_OUTPUT_CHANNELS);
    if (!out_pcm) { snd_pcm_close(mic_pcm); return 1; }
    /* The tap is opened NON-FATALLY. It disappears every time the switch leaves
     * Speaker, and a daemon that died with it would take the microphone with
     * it. A NULL tap is simply "no reference", which is the pass-through case. */
    snd_pcm_t *tap_pcm = open_reference(tap_name);
    if (!tap_pcm) journal("aec: no reference at %s yet; passing the microphone "
                          "through and retrying every %d ms", tap_name, TAP_RETRY_MS);
    int64_t tap_retry_ms = monotonic_ms() + TAP_RETRY_MS;
    int64_t mute_poll_ms = 0;
    bool input_muted = false;

    int16_t mic_stereo[AEC_FRAME_SIZE * 2], tap_stereo[AEC_FRAME_SIZE * 2];
    int16_t mic[AEC_FRAME_SIZE], tap[AEC_FRAME_SIZE], out[AEC_FRAME_SIZE];
    stream_clock tap_base = { false, 0, 0, 0, false, false };
    stream_clock mic_base = { false, 0, 0, 0, false, false };
    int64_t drift_due_ms = monotonic_ms() + AEC_DRIFT_WINDOW_MS;
    int status = 0;

    while (!stopping) {
        /* THE TAP IS RETRIED, NOT GIVEN UP ON. It is absent whenever the switch
         * is not in Speaker, which is most of the time, so an open that failed
         * at start says nothing about whether one will succeed later -- and
         * without this the daemon passed the microphone through for the rest of
         * its life after one unlucky moment. */
        int64_t retry_now = monotonic_ms();
        if (!tap_pcm && retry_now >= tap_retry_ms) {
            tap_retry_ms = retry_now + TAP_RETRY_MS;
            tap_pcm = open_reference(tap_name);
            if (tap_pcm) {
                journal("aec: reference at %s is available; cancelling from a "
                        "cold filter", tap_name);
                engine->reference_fill = 0;
                engine_reset_filter(engine);
                /* The alignment and the timestamp baseline are both unknown
                 * against a stream that has only just started. */
                aec_policy_xrun(&engine->policy, retry_now);
                engine->policy.xruns_this_session -= 1;  /* not a fault */
                tap_base.usable = mic_base.usable = false;
            }
        }
        if (retry_now >= mute_poll_ms) {
            mute_poll_ms = retry_now + MUTE_POLL_MS;
            bool was_muted = input_muted;
            input_muted = read_input_muted(mute_name);
            /* A MUTE TRANSITION IS PUBLISHED AT ONCE rather than waiting for
             * the next 250 ms status tick. The renderer's disc
             * is already gated on the applier's switch state, so nothing draws
             * a live disc over a muted microphone either way -- but leaving the
             * published block saying `live` after the mic
             * legs stopped is a level that is not true of anything, and any
             * later consumer of that block would inherit the lie. */
            if (input_muted != was_muted) {
                engine->status_due_ms = 0;
            }
        }
        snd_pcm_sframes_t got = snd_pcm_readi(mic_pcm, mic_stereo, AEC_FRAME_SIZE);
        if (got < 0) {
            /* AN XRUN ON EITHER STREAM IS A STEP CHANGE IN OFFSET, which the
             * rate controller cannot and must not chase. Both PCMs are dropped
             * and prepared -- not only the one that faulted, because recovering
             * one leaves the unknown offset the whole procedure exists to
             * remove -- and the policy re-arms, so no reset or failover can
             * fire on the cold convergence this causes. */
            journal("aec: xrun on the microphone (%s); resynchronising both streams",
                    snd_strerror((int)got));
            if (!restart_capture(mic_pcm, mic_name)) {
                status = 1;
                break;
            }
            if (tap_pcm) {
                if (!restart_capture(tap_pcm, tap_name)) {
                    snd_pcm_close(tap_pcm);
                    tap_pcm = NULL;
                    tap_retry_ms = monotonic_ms() + TAP_RETRY_MS;
                }
            }
            engine->reference_fill = 0;
            engine_reset_filter(engine);
            aec_policy_xrun(&engine->policy, monotonic_ms());
            tap_base.usable = mic_base.usable = false;
            continue;
        }
        if (got != AEC_FRAME_SIZE) continue;
        for (size_t frame = 0; frame < AEC_FRAME_SIZE; frame++) {
            int32_t sum = (int32_t)mic_stereo[frame * 2] +
                          (int32_t)mic_stereo[frame * 2 + 1];
            mic[frame] = (int16_t)(sum / 2);
        }

        bool tap_present = false;
        if (tap_pcm) {
            /* DRAINED BY AVAILABILITY, NOT ONE BLOCK PER MIC BLOCK (terra,
             * second pass). Admitting exactly 256 tap frames per mic block
             * assumes the two cards run at exactly the same rate, which is the
             * assumption the whole drift controller exists because they do not
             * meet: once a non-unity ratio is in force the resampler consumes
             * slightly more than 256 per block for a fast tap, so a ring topped
             * up by exactly 256 starves -- and for a slow tap it fills and
             * push_reference drops old frames, destroying the alignment. The
             * tap is the producer; take what it has, up to what the ring can
             * hold, and let the ring absorb the difference. */
            for (;;) {
                if (REF_RING_FRAMES - engine->reference_fill < AEC_FRAME_SIZE) {
                    tap_present = true;
                    break;   /* the ring is as full as it should get */
                }
                snd_pcm_sframes_t available = snd_pcm_avail_update(tap_pcm);
                if (available < 0) {
                    if (!restart_capture(tap_pcm, tap_name)) {
                        snd_pcm_close(tap_pcm);
                        tap_pcm = NULL;
                        tap_retry_ms = monotonic_ms() + TAP_RETRY_MS;
                    }
                    engine->reference_fill = 0;
                    break;
                }
                if (available < AEC_FRAME_SIZE) break;
                snd_pcm_sframes_t read = snd_pcm_readi(tap_pcm, tap_stereo,
                                                       AEC_FRAME_SIZE);
                if (read == AEC_FRAME_SIZE) {
                    for (size_t frame = 0; frame < AEC_FRAME_SIZE; frame++) {
                        int32_t sum = (int32_t)tap_stereo[frame * 2] +
                                      (int32_t)tap_stereo[frame * 2 + 1];
                        tap[frame] = (int16_t)(sum / 2);
                    }
                    push_reference(engine, tap, AEC_FRAME_SIZE);
                    tap_present = true;
                    continue;
                }
                if (read < 0) {
                    if (!restart_capture(tap_pcm, tap_name)) {
                        snd_pcm_close(tap_pcm);
                        tap_pcm = NULL;
                        tap_retry_ms = monotonic_ms() + TAP_RETRY_MS;
                    }
                    engine->reference_fill = 0;
                }
                break;
            }
            /* Still draining what the ring holds counts as a live reference:
             * the tap has not gone away, it is simply ahead of us. */
            if (engine->reference_fill >= AEC_FRAME_SIZE) tap_present = true;
        }

        int64_t now_ms = monotonic_ms();
        engine_block(engine, mic, out, tap_present, input_muted, now_ms);
        /* A SHORT WRITE IS NOT A SUCCESS. snd_pcm_writei() may return fewer
         * frames than it was given; dropping the tail silently punches a hole
         * in mic_clean and moves the echo path's timing out from under the
         * filter, with nothing recorded anywhere (terra 2026-09-14, finding 3).
         * Push the remainder, and treat a stream that will not take it as the
         * xrun it is so the alignment is re-derived rather than assumed. */
        for (snd_pcm_uframes_t done = 0; done < AEC_FRAME_SIZE; ) {
            snd_pcm_sframes_t wrote = snd_pcm_writei(out_pcm, out + done,   /* mono */
                                                     AEC_FRAME_SIZE - done);
            if (wrote < 0) {
                if (snd_pcm_recover(out_pcm, (int)wrote, 1) < 0) break;
                aec_policy_xrun(&engine->policy, now_ms);
                tap_base.usable = mic_base.usable = false;
                break;
            }
            if (wrote == 0) break;   /* nothing is moving; the next block retries */
            done += (snd_pcm_uframes_t)wrote;
        }

        if (tap_pcm && now_ms >= drift_due_ms) {
            stream_clock tap_now = read_clock(tap_pcm);
            stream_clock mic_now = read_clock(mic_pcm);
            /* THE DENOMINATORS ARE NOT GUARANTEED TO ADVANCE. A driver that
             * reports the same system timestamp twice, or a clock that does not
             * move between two reads, divides by zero here; mic_ratio can also
             * come out at zero and divide again. The result is an infinity or a
             * NaN that aec_policy_drift() would carry into an int32_t ratio and
             * on into speex_resampler_set_rate_frac() (terra 2026-09-14,
             * finding 2). Unmeasurable drift is UNMEASURABLE, not zero: the
             * window is simply skipped and the baseline re-taken below. */
            double tap_system = tap_now.system_seconds - tap_base.system_seconds;
            double mic_system = mic_now.system_seconds - mic_base.system_seconds;
            double tap_ratio = tap_system > 0.0
                ? (tap_now.audio_seconds - tap_base.audio_seconds) / tap_system : 0.0;
            double mic_ratio = mic_system > 0.0
                ? (mic_now.audio_seconds - mic_base.audio_seconds) / mic_system : 0.0;
            double slip_ppm = (tap_ratio / mic_ratio - 1.0) * 1e6;
            bool drift_measurable = tap_base.usable && mic_base.usable
                && tap_system > 0.0 && mic_system > 0.0
                && isfinite(tap_ratio) && isfinite(mic_ratio) && mic_ratio > 0.0
                && isfinite(slip_ppm);
            if (drift_measurable) {
                int64_t accuracy = tap_now.accuracy_ns > mic_now.accuracy_ns
                    ? tap_now.accuracy_ns : mic_now.accuracy_ns;
                aec_decision decision = aec_policy_drift(
                    &engine->policy, now_ms, slip_ppm, AEC_DRIFT_WINDOW_MS,
                    tap_now.usable && mic_now.usable,
                    tap_now.accuracy_reported && mic_now.accuracy_reported,
                    accuracy, tap_now.link && mic_now.link);
                if (decision.action == AEC_ACTION_SET_RATE)
                    speex_resampler_set_rate_frac(engine->resampler,
                                                  (spx_uint32_t)decision.ratio_ppm, 1000000u,
                                                  AEC_RATE_HZ, AEC_RATE_HZ);
            }
            tap_base = tap_now; mic_base = mic_now;
            drift_due_ms = now_ms + AEC_DRIFT_WINDOW_MS;
        }
    }

    publish(&engine->policy, engine->status_path);
    snd_pcm_close(mic_pcm);
    snd_pcm_close(out_pcm);
    if (tap_pcm) snd_pcm_close(tap_pcm);
    return status;
}
#endif /* WALL_AEC_OFFLINE_ONLY */

int main(int argc, char **argv)
{
    const char *profile_path = DEFAULT_PROFILE;
    const char *status_path = DEFAULT_STATUS;
    const char *mic_name = DEFAULT_MIC;
    const char *tap_name = DEFAULT_TAP;
    const char *out_name = DEFAULT_OUT;
    const char *mute_name = DEFAULT_MUTE_FILE;
    const char *replay_reference = NULL;
    const char *replay_near = NULL;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--profile") && i + 1 < argc) profile_path = argv[++i];
        else if (!strcmp(argv[i], "--status") && i + 1 < argc) status_path = argv[++i];
        else if (!strcmp(argv[i], "--mic") && i + 1 < argc) mic_name = argv[++i];
        else if (!strcmp(argv[i], "--tap") && i + 1 < argc) tap_name = argv[++i];
        else if (!strcmp(argv[i], "--out") && i + 1 < argc) out_name = argv[++i];
        else if (!strcmp(argv[i], "--mute") && i + 1 < argc) mute_name = argv[++i];
        else if (!strcmp(argv[i], "--replay") && i + 2 < argc) {
            replay_reference = argv[++i];
            replay_near = argv[++i];
        } else {
            fprintf(stderr, "usage: %s [--profile P] [--status P] [--mic PCM] [--tap PCM] "
                            "[--out PCM] [--mute P] [--replay REF.raw NEAR.raw]\n", argv[0]);
            return 2;
        }
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    aec_profile profile;
    char note[512];
    aec_profile_load(profile_path, &profile, note, sizeof(note));
    if (note[0]) journal("aec: %s", note);
    journal("aec: pre-delay %.1f ms, filter %.1f ms, ERLE floor %.1f dB, profile %s",
            profile.predelay_ms, profile.filter_length_ms, profile.erle_floor_db, profile.name);

    static aec_engine engine;
    if (!engine_open(&engine, &profile, status_path)) {
        journal("aec: could not initialise the canceller");
        return 1;
    }
    aec_policy_init(&engine.policy, &profile, replay_reference ? 0 : monotonic_ms());
    publish(&engine.policy, status_path);

    int result;
    if (replay_reference) {
        result = replay(&engine, replay_reference, replay_near);
    } else {
#ifdef WALL_AEC_OFFLINE_ONLY
        /* The replay-only build has no live path at all. The device names were
         * still parsed above, so a command line meant for the daemon is not
         * silently accepted as something else. */
        (void)mic_name; (void)tap_name; (void)out_name; (void)mute_name;
        journal("aec: built without ALSA; only --replay is available");
        result = 2;
#else
        result = live(&engine, mic_name, tap_name, out_name, mute_name);
#endif
    }
    engine_close(&engine);
    return result;
}

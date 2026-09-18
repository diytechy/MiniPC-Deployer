#ifndef WALL_AEC_PCM_H
#define WALL_AEC_PCM_H

/* The live endpoints' measured native shapes. Keeping these beside the
 * lifecycle seam makes the values exercised by the compiled test the same
 * values passed to ALSA by the daemon. */
#define AEC_MIC_CHANNELS 2u
#define AEC_REFERENCE_CHANNELS 2u
#define AEC_OUTPUT_CHANNELS 1u

typedef int (*aec_pcm_step)(void *context);
typedef int (*aec_pcm_value_step)(void *context, unsigned long value);

typedef struct {
    void *context;
    aec_pcm_step current;
    aec_pcm_value_step start_threshold;
    aec_pcm_value_step avail_min;
    aec_pcm_step apply;
    aec_pcm_step drop;
    aec_pcm_step prepare;
    aec_pcm_step start;
} aec_pcm_ops;

static inline int aec_pcm_configure_capture(const aec_pcm_ops *ops,
                                            unsigned long frame_size)
{
    int error = ops->current(ops->context);
    if (error >= 0) error = ops->start_threshold(ops->context, 1);
    if (error >= 0) error = ops->avail_min(ops->context, frame_size);
    if (error >= 0) error = ops->apply(ops->context);
    return error;
}

static inline int aec_pcm_start_reference(const aec_pcm_ops *ops)
{
    return ops->start(ops->context);
}

static inline int aec_pcm_restart_capture(const aec_pcm_ops *ops)
{
    /* A faulted stream may reject drop. prepare and start decide whether the
     * existing handle is usable again, so drop is deliberately best-effort. */
    (void)ops->drop(ops->context);
    int error = ops->prepare(ops->context);
    if (error >= 0) error = ops->start(ops->context);
    return error;
}

static inline int aec_pcm_restart_pair(const aec_pcm_ops *mic,
                                       const aec_pcm_ops *reference)
{
    /* Prepare both endpoints before either one enters RUNNING. Starting the
     * reference during open while the microphone waited for its first read
     * created a fresh, scheduler-sized alignment error on every daemon start. */
    (void)mic->drop(mic->context);
    (void)reference->drop(reference->context);
    int error = mic->prepare(mic->context);
    if (error >= 0) error = reference->prepare(reference->context);
    if (error >= 0) error = mic->start(mic->context);
    if (error >= 0) error = reference->start(reference->context);
    return error;
}

#endif

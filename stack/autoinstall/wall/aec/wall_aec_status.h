/* wall_aec_status.h -- non-blocking handoff from audio to status I/O. */
#ifndef WALL_AEC_STATUS_H
#define WALL_AEC_STATUS_H

#include "wall_aec_policy.h"

#include <pthread.h>

typedef void (*aec_status_write_fn)(const aec_policy *policy, const char *path);

typedef struct {
    pthread_t thread;
    pthread_mutex_t lock;
    pthread_cond_t ready;
    bool started;
    bool pending;
    bool stopping;
    aec_policy snapshot;
    char path[512];
    aec_status_write_fn write;
} aec_status_publisher;

bool aec_status_publisher_start(aec_status_publisher *publisher, const char *path,
                                aec_status_write_fn write);
/* Latest-value, non-blocking handoff. False means the worker owned the slot;
 * the audio thread skips this publication rather than waiting. */
bool aec_status_publisher_try_enqueue(aec_status_publisher *publisher,
                                      const aec_policy *policy);
void aec_status_publisher_stop(aec_status_publisher *publisher);

#endif

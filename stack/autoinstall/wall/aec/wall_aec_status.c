/* wall_aec_status.c -- status worker; no audio processing and no ALSA. */
#include "wall_aec_status.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>

static void *publisher_run(void *opaque)
{
    aec_status_publisher *publisher = opaque;
    for (;;) {
        pthread_mutex_lock(&publisher->lock);
        while (!publisher->pending && !publisher->stopping)
            pthread_cond_wait(&publisher->ready, &publisher->lock);
        if (!publisher->pending && publisher->stopping) {
            pthread_mutex_unlock(&publisher->lock);
            break;
        }
        aec_policy snapshot = publisher->snapshot;
        publisher->pending = false;
        pthread_mutex_unlock(&publisher->lock);
        publisher->write(&snapshot, publisher->path);
    }
    return NULL;
}

bool aec_status_publisher_start(aec_status_publisher *publisher, const char *path,
                                aec_status_write_fn write)
{
    if (!publisher || !path || !write) return false;
    memset(publisher, 0, sizeof(*publisher));
    snprintf(publisher->path, sizeof(publisher->path), "%s", path);
    publisher->write = write;
    if (pthread_mutex_init(&publisher->lock, NULL) != 0) return false;
    if (pthread_cond_init(&publisher->ready, NULL) != 0) {
        pthread_mutex_destroy(&publisher->lock);
        return false;
    }
    if (pthread_create(&publisher->thread, NULL, publisher_run, publisher) != 0) {
        pthread_cond_destroy(&publisher->ready);
        pthread_mutex_destroy(&publisher->lock);
        return false;
    }
    publisher->started = true;
    return true;
}

bool aec_status_publisher_try_enqueue(aec_status_publisher *publisher,
                                      const aec_policy *policy)
{
    int locked = pthread_mutex_trylock(&publisher->lock);
    if (locked == EBUSY) return false;
    if (locked != 0) return false;
    publisher->snapshot = *policy;
    publisher->pending = true;
    pthread_cond_signal(&publisher->ready);
    pthread_mutex_unlock(&publisher->lock);
    return true;
}

void aec_status_publisher_stop(aec_status_publisher *publisher)
{
    if (!publisher->started) return;
    pthread_mutex_lock(&publisher->lock);
    publisher->stopping = true;
    pthread_cond_signal(&publisher->ready);
    pthread_mutex_unlock(&publisher->lock);
    pthread_join(publisher->thread, NULL);
    pthread_cond_destroy(&publisher->ready);
    pthread_mutex_destroy(&publisher->lock);
    publisher->started = false;
}

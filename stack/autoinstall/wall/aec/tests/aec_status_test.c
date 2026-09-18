#include "../wall_aec_status.h"

#include <stdio.h>
#include <string.h>

static int failures = 0;
static int writes = 0;
static double written_level = -1.0;

static void expect(bool condition, const char *name)
{
    if (condition) printf("PASS %s\n", name);
    else { printf("FAIL %s\n", name); failures += 1; }
}

static void record(const aec_policy *policy, const char *path)
{
    (void)path;
    writes += 1;
    written_level = policy->level;
}

int main(void)
{
    aec_status_publisher publisher;
    aec_policy policy;
    memset(&policy, 0, sizeof(policy));
    policy.level = 0.42;
    expect(aec_status_publisher_start(&publisher, "/unused", record),
           "status publisher starts");

    /* The audio-side handoff must never wait for the worker's lock. Holding it
     * here makes a blocking implementation deadlock this test. */
    pthread_mutex_lock(&publisher.lock);
    expect(!aec_status_publisher_try_enqueue(&publisher, &policy),
           "busy handoff is skipped without blocking");
    pthread_mutex_unlock(&publisher.lock);

    expect(aec_status_publisher_try_enqueue(&publisher, &policy),
           "an available latest-value slot accepts a snapshot");
    aec_status_publisher_stop(&publisher);
    expect(writes == 1, "shutdown drains the pending snapshot");
    expect(written_level == 0.42, "the worker receives the copied policy");
    printf("%d STATUS PUBLISHER FAILURES\n", failures);
    return failures ? 1 : 0;
}

#include <stdio.h>
#include <string.h>

#include "../wall_aec_pcm.h"

typedef struct {
    char calls[32];
    size_t used;
    char fail;
    unsigned long threshold;
    unsigned long avail;
} fake_pcm;

static int record(fake_pcm *pcm, char call)
{
    pcm->calls[pcm->used++] = call;
    pcm->calls[pcm->used] = '\0';
    return pcm->fail == call ? -5 : 0;
}

static int current(void *context) { return record(context, 'c'); }
static int threshold(void *context, unsigned long value)
{
    fake_pcm *pcm = context;
    pcm->threshold = value;
    return record(pcm, 't');
}
static int avail(void *context, unsigned long value)
{
    fake_pcm *pcm = context;
    pcm->avail = value;
    return record(pcm, 'a');
}
static int apply(void *context) { return record(context, 'w'); }
static int drop(void *context) { return record(context, 'd'); }
static int prepare(void *context) { return record(context, 'p'); }
static int start(void *context) { return record(context, 's'); }

static aec_pcm_ops operations(fake_pcm *pcm)
{
    aec_pcm_ops ops = {
        pcm, current, threshold, avail, apply, drop, prepare, start
    };
    return ops;
}

static int tests;
static int failures;
#define OK(condition, message) do { \
    tests++; \
    if (condition) printf("PASS pcm %s\n", message); \
    else { printf("FAIL pcm %s\n", message); failures++; } \
} while (0)

static void configure_tests(void)
{
    fake_pcm pcm = {0};
    aec_pcm_ops ops = operations(&pcm);
    OK(aec_pcm_configure_capture(&ops, 256) == 0, "capture setup succeeds");
    OK(strcmp(pcm.calls, "ctaw") == 0, "capture setup preserves call order");
    OK(pcm.threshold == 1, "capture starts with the first available frame");
    OK(pcm.avail == 256, "capture wakes on one AEC frame");

    const char steps[] = {'c', 't', 'a', 'w'};
    const char *prefixes[] = {"c", "ct", "cta", "ctaw"};
    for (size_t index = 0; index < sizeof(steps); index++) {
        memset(&pcm, 0, sizeof(pcm));
        pcm.fail = steps[index];
        OK(aec_pcm_configure_capture(&ops, 256) < 0,
           "capture setup returns each injected failure");
        OK(strcmp(pcm.calls, prefixes[index]) == 0,
           "capture setup stops at the failing operation");
    }
}

static void start_and_restart_tests(void)
{
    fake_pcm pcm = {0};
    aec_pcm_ops ops = operations(&pcm);
    OK(aec_pcm_start_reference(&ops) == 0, "reference is explicitly started");
    OK(strcmp(pcm.calls, "s") == 0, "reference start calls only start");

    memset(&pcm, 0, sizeof(pcm));
    OK(aec_pcm_restart_capture(&ops) == 0, "capture restart succeeds");
    OK(strcmp(pcm.calls, "dps") == 0, "restart drops, prepares, then starts");

    memset(&pcm, 0, sizeof(pcm));
    pcm.fail = 'd';
    OK(aec_pcm_restart_capture(&ops) == 0, "a failed drop is non-fatal");
    OK(strcmp(pcm.calls, "dps") == 0, "restart proceeds after failed drop");

    memset(&pcm, 0, sizeof(pcm));
    pcm.fail = 'p';
    OK(aec_pcm_restart_capture(&ops) < 0, "a failed prepare is returned");
    OK(strcmp(pcm.calls, "dp") == 0, "start is skipped after failed prepare");

    memset(&pcm, 0, sizeof(pcm));
    pcm.fail = 's';
    OK(aec_pcm_restart_capture(&ops) < 0, "a failed restart is returned");
    OK(strcmp(pcm.calls, "dps") == 0, "restart failure follows full sequence");

    OK(AEC_MIC_CHANNELS == 2u, "microphone uses its native stereo shape");
    OK(AEC_REFERENCE_CHANNELS == 2u, "reference uses its native stereo shape");
    OK(AEC_OUTPUT_CHANNELS == 1u, "clean loopback remains mono");
}

int main(void)
{
    configure_tests();
    start_and_restart_tests();
    printf("%d PCM TESTS, %d FAILURES\n", tests, failures);
    return failures == 0 ? 0 : 1;
}

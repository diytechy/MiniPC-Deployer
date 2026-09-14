/* wall_aec_profile.c -- read the dated room profile, and refuse nothing.
 *
 * ONE RESPONSIBILITY: turn the bytes of `/etc/wall-panel/aec-profile.json` into
 * an `aec_profile`, or leave the conservative defaults standing. It is a
 * DELIBERATELY SMALL reader over a FLAT, KNOWN shape -- four numbers and one
 * nested object -- and not a JSON parser: a general parser in the privileged
 * audio path is a larger attack surface and a larger maintenance burden than
 * this file is worth, and the document it reads is written by our own retune
 * tool.
 *
 * WHAT IT WILL NOT DO, because the spike is explicit about it: it never loads a
 * gain, a level or a filter coefficient into the audio path. `predelay_ms` and
 * `filter_length_samples` are GEOMETRY -- fixed by where the speakers and the
 * microphone are, unchanged by anybody turning a volume knob -- and that
 * distinction is the whole reason a stored profile is compatible with closing
 * the loop from the microphone at all. The `acceptance` figures reach the
 * health logic and can change what the panel REPORTS; they cannot change a
 * sample.
 *
 * A missing or unparseable profile is not fatal. The daemon runs on the
 * defaults, passes the microphone through until ERLE is above the floor, and
 * says `no-profile` in the status block.
 *
 * Implements: SR-028, LLR-016
 */

#include "wall_aec_profile.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PROFILE_MAX_BYTES 65536

/* Find `"key"` at any depth and return the number that follows its colon.
 *
 * FLAT BY ASSUMPTION AND SAFE BY CONSTRUCTION. The keys we want are unique
 * across the whole document (the retune tool writes exactly one of each), so
 * depth does not have to be tracked to find the right one -- and because the
 * only thing ever taken from the document is a `double` parsed by `strtod`
 * from a NUL-terminated buffer, a malformed document can produce a wrong
 * NUMBER but not a read outside the buffer. Every number is then clamped by
 * `aec_profile_clamp`, so a wrong number is a bounded one.
 */
static bool find_number(const char *text, const char *key, double *out)
{
    char pattern[64];
    int n = snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    if (n <= 0 || (size_t)n >= sizeof(pattern)) return false;
    const char *at = strstr(text, pattern);
    if (!at) return false;
    at = strchr(at + n, ':');
    if (!at) return false;
    at += 1;
    while (*at == ' ' || *at == '\t' || *at == '\n' || *at == '\r') at += 1;
    if (*at == 'n') return false;   /* an explicit null is "not measured" */
    char *end = NULL;
    double value = strtod(at, &end);
    if (end == at) return false;
    if (!(value == value)) return false;   /* NaN */
    *out = value;
    return true;
}

bool aec_profile_load(const char *path, aec_profile *profile, char *note, size_t note_size)
{
    aec_profile_defaults(profile);
    if (note && note_size) note[0] = '\0';

    FILE *handle = fopen(path, "rb");
    if (!handle) {
        if (note) snprintf(note, note_size, "no profile at %s: running on conservative defaults "
                                            "(pre-delay %.0f ms, filter %.0f ms)",
                           path, profile->predelay_ms, profile->filter_length_ms);
        return false;
    }
    static char text[PROFILE_MAX_BYTES + 1];
    size_t read = fread(text, 1, PROFILE_MAX_BYTES, handle);
    int oversize = !feof(handle);
    fclose(handle);
    text[read] = '\0';
    if (oversize) {
        if (note) snprintf(note, note_size, "profile %s is larger than %d bytes: ignored",
                           path, PROFILE_MAX_BYTES);
        return false;
    }
    /* A NUL inside the document would truncate every search below and silently
     * hide the fields after it, which is a wrong profile rather than a refused
     * one. Refuse it instead. */
    if (strlen(text) != read) {
        if (note) snprintf(note, note_size, "profile %s contains a NUL byte: ignored", path);
        return false;
    }

    double value = 0.0;
    if (find_number(text, "schema", &value) && value != 1.0) {
        if (note) snprintf(note, note_size, "profile %s is schema %.0f, not 1: ignored", path, value);
        return false;
    }
    if (find_number(text, "predelay_ms", &value)) profile->predelay_ms = value;
    if (find_number(text, "bulk_delay_ms", &value)) profile->bulk_delay_ms = value;
    if (find_number(text, "filter_length_ms", &value)) profile->filter_length_ms = value;
    if (find_number(text, "erle_floor_db", &value)) profile->erle_floor_db = value;
    if (find_number(text, "erle_at_retune_db", &value)) profile->erle_at_retune_db = value;
    profile->present = true;

    const char *base = strrchr(path, '/');
    snprintf(profile->name, sizeof(profile->name), "%s", base ? base + 1 : path);

    if (!aec_profile_clamp(profile) && note) {
        /* OUT LOUD. A profile whose pre-delay overshot the bulk delay costs
         * 6 dB of ERLE that adaptation cannot recover, so a corrected one is
         * exactly the thing somebody needs to be told about. */
        snprintf(note, note_size,
                 "profile %s needed correcting: pre-delay %.1f ms, filter %.1f ms in force",
                 profile->name, profile->predelay_ms, profile->filter_length_ms);
    }
    return true;
}

/* wall_aec_profile.h -- the dated room profile reader. See the .c for why it is
 * a small bounded reader rather than a JSON parser.
 *
 * Implements: SR-028, LLR-016
 */

#ifndef WALL_AEC_PROFILE_H
#define WALL_AEC_PROFILE_H

#include "wall_aec_policy.h"

/** Load `path` into `profile`, or leave the conservative defaults standing.
 *
 * Contract:
 *   Inputs:  path: the profile file, normally the /etc/wall-panel/aec-profile.json
 *                  symlink to a dated file
 *            note/note_size: a buffer for one journal line; may be NULL
 *   Outputs: true when a profile was read (`profile->present` is then true),
 *            false when the defaults are in force. NEITHER IS AN ERROR: a
 *            panel with no profile runs, passes the microphone through until
 *            ERLE is above the floor, and says `no-profile`.
 *   Raises:  nothing; every failure is a note and the defaults.
 * Implements: SR-028, LLR-016
 */
bool aec_profile_load(const char *path, aec_profile *profile, char *note, size_t note_size);

#endif /* WALL_AEC_PROFILE_H */

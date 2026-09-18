# wall-audio-aec -- built on the panel by wall-firstboot.sh, and on a dev box
# by `make check`.
#
# TWO TARGETS AND THEY ARE NOT THE SAME BUILD:
#   wall-audio-aec   the daemon. Needs libasound2-dev and libspeexdsp-dev.
#   check            the POLICY tests. Need NEITHER -- wall_aec_policy.c is pure
#                    C with no external header at all, which is the whole reason
#                    the decision core was split out of the shell.
#
# Implements: SR-028, LLR-016

CC ?= cc
CFLAGS ?= -O2 -std=c11 -Wall -Wextra -Wshadow -Wconversion -Wno-sign-conversion
LDLIBS_DAEMON = -lasound -lspeexdsp -lm

.PHONY: all check clean offline

all: wall-audio-aec

wall-audio-aec: wall-audio-aec.c wall_aec_policy.c wall_aec_profile.c \
                wall_aec_policy.h wall_aec_profile.h wall_aec_pcm.h
	$(CC) $(CFLAGS) -o $@ wall-audio-aec.c wall_aec_policy.c wall_aec_profile.c $(LDLIBS_DAEMON)

# The replay-only build: no ALSA, so the spike's retained captures can be run
# through the real engine on a machine with no sound card. It still needs
# libspeexdsp, because the thing being replayed IS the canceller.
offline: wall-audio-aec-offline

wall-audio-aec-offline: wall-audio-aec.c wall_aec_policy.c wall_aec_profile.c \
                        wall_aec_pcm.h
	$(CC) $(CFLAGS) -DWALL_AEC_OFFLINE_ONLY -o $@ \
	    wall-audio-aec.c wall_aec_policy.c wall_aec_profile.c -lspeexdsp -lm

check: tests/aec_policy_test tests/aec_pcm_test
	./tests/aec_policy_test
	./tests/aec_pcm_test

tests/aec_policy_test: tests/aec_policy_test.c wall_aec_policy.c wall_aec_profile.c \
                       wall_aec_policy.h wall_aec_profile.h
	$(CC) $(CFLAGS) -o $@ tests/aec_policy_test.c wall_aec_policy.c wall_aec_profile.c -lm

tests/aec_pcm_test: tests/aec_pcm_test.c wall_aec_pcm.h
	$(CC) $(CFLAGS) -o $@ tests/aec_pcm_test.c

clean:
	rm -f wall-audio-aec wall-audio-aec-offline tests/aec_policy_test tests/aec_pcm_test

// SPDX-License-Identifier: CDDL-1.0
/*
 * This file and its contents are supplied under the terms of the
 * Common Development and Distribution License (CDDL), version 1.0.
 */

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int
main(int argc, char *argv[])
{
	const char *delay = getenv("ZFS_TEST_SETPGID_DELAY");

	if (argc < 2) {
		(void) fprintf(stderr, "usage: %s command [argument ...]\n",
		    argv[0]);
		return (2);
	}

	if (delay != NULL) {
		unsigned int seconds = (unsigned int) strtoul(delay, NULL, 10);
		while (seconds != 0)
			seconds = sleep(seconds);
	}

	if (setpgid(0, 0) != 0) {
		(void) fprintf(stderr, "%s: setpgid failed: %s\n", argv[0],
		    strerror(errno));
		return (125);
	}

	const char *ready_file = getenv("ZFS_TEST_SETPGID_READY_FILE");
	if (ready_file != NULL) {
		FILE *fp = fopen(ready_file, "w");
		if (fp == NULL) {
			(void) fprintf(stderr,
			    "%s: cannot create readiness file %s: %s\n",
			    argv[0], ready_file, strerror(errno));
			return (126);
		}
		if (fclose(fp) != 0) {
			(void) fprintf(stderr,
			    "%s: cannot close readiness file %s: %s\n",
			    argv[0], ready_file, strerror(errno));
			return (126);
		}
	}

	execvp(argv[1], &argv[1]);
	(void) fprintf(stderr, "%s: %s: %s\n", argv[0], argv[1],
	    strerror(errno));
	return (127);
}

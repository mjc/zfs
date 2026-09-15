// SPDX-License-Identifier: CDDL-1.0
/*
 * This file and its contents are supplied under the terms of the
 * Common Development and Distribution License (CDDL), version 1.0.
 */

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int
main(int argc, char *argv[])
{
	if (argc < 2) {
		(void) fprintf(stderr, "usage: %s command [argument ...]\n",
		    argv[0]);
		return (2);
	}

	if (setpgid(0, 0) != 0) {
		(void) fprintf(stderr, "%s: setpgid failed: %s\n", argv[0],
		    strerror(errno));
		return (125);
	}

	execvp(argv[1], &argv[1]);
	(void) fprintf(stderr, "%s: %s: %s\n", argv[0], argv[1],
	    strerror(errno));
	return (127);
}

#!/bin/sh
# SPDX-License-Identifier: CDDL-1.0

if [ -n "$PERF_OUTPUT_FILE" ]; then
	 exec perf record -F 99 -a -g -q -o "$PERF_OUTPUT_FILE" -- \
	    sleep "$PERF_RUNTIME"
else
	 exec perf record -F 99 -a -g -q -o /dev/stdout 2>/dev/null -- \
	    sleep "$PERF_RUNTIME"
fi

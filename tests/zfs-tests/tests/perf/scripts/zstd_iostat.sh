#!/bin/sh
# SPDX-License-Identifier: CDDL-1.0

: "${PERFPOOL:?PERFPOOL must be set}"

if [ -n "${PERF_START_FILE:-}" ]; then
	while [ ! -f "$PERF_START_FILE" ]; do
		sleep 1
	done
fi

while [ -z "${PERF_STOP_FILE:-}" ] || [ ! -f "$PERF_STOP_FILE" ]; do
	zpool iostat -lpvyL "$PERFPOOL" 1 1
done

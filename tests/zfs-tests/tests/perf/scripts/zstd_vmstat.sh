#!/bin/sh
# SPDX-License-Identifier: CDDL-1.0

if [ -n "${PERF_START_FILE:-}" ]; then
	while [ ! -f "$PERF_START_FILE" ]; do
		sleep 1
	done
fi

case "$(uname -s)" in
Linux)
	while [ -z "${PERF_STOP_FILE:-}" ] || [ ! -f "$PERF_STOP_FILE" ]; do
		vmstat -t -y 1 1
	done
	;;
FreeBSD)
	exec vmstat -w 1
	;;
*)
	exec vmstat 1
	;;
esac

#!/bin/sh
# SPDX-License-Identifier: CDDL-1.0

if [ -n "${PERF_START_FILE:-}" ]; then
	while [ ! -f "$PERF_START_FILE" ]; do
		sleep 1
	done
fi

vmstat_pid=
cleanup()
{
	if [ -n "$vmstat_pid" ]; then
		kill "$vmstat_pid" 2>/dev/null || :
		wait "$vmstat_pid" 2>/dev/null || :
	fi
}

trap 'exit 0' HUP INT TERM
trap cleanup EXIT

case "$(uname -s)" in
Linux)
	while [ -z "${PERF_STOP_FILE:-}" ] || [ ! -f "$PERF_STOP_FILE" ]; do
		vmstat -t -y 1 1
	done
	;;
FreeBSD)
	vmstat -w 1 &
	vmstat_pid=$!
	while [ -z "${PERF_STOP_FILE:-}" ] || [ ! -f "$PERF_STOP_FILE" ]; do
		sleep 1
	done
	kill "$vmstat_pid" 2>/dev/null || :
	wait "$vmstat_pid" 2>/dev/null || :
	vmstat_pid=
	;;
*)
	vmstat 1 &
	vmstat_pid=$!
	while [ -z "${PERF_STOP_FILE:-}" ] || [ ! -f "$PERF_STOP_FILE" ]; do
		sleep 1
	done
	kill "$vmstat_pid" 2>/dev/null || :
	wait "$vmstat_pid" 2>/dev/null || :
	vmstat_pid=
	;;
esac

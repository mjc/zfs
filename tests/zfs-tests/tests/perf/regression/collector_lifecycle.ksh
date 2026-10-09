#!/bin/ksh
# SPDX-License-Identifier: CDDL-1.0
#
# This file and its contents are supplied under the terms of the
# Common Development and Distribution License ("CDDL"), version 1.0.
# You may only use this file in accordance with the terms of version
# 1.0 of the CDDL.
#
# A full copy of the text of the CDDL should have accompanied this
# source.  A copy of the CDDL is also available via the Internet at
# https://opensource.org/license/CDDL-1.0.

. $STF_SUITE/include/libtest.shlib
. $STF_SUITE/tests/perf/perf.shlib

verify_runnable "global"

export PERF_RUNTIME=1
export PERF_COLLECT_SCRIPTS=
export PERF_COLLECT_OPTIONAL_SCRIPTS=
export SUDO_COMMAND=collector_lifecycle.ksh

typeset rc
typeset startup_pid=
typeset startup_tmpdir=
typeset iostat_pid=
typeset iostat_tmpdir=

function cleanup_iostat_failure
{
	trap "" INT TERM
	typeset pid=$iostat_pid

	if [[ -n $pid ]]; then
		if kill -0 "$pid" 2>/dev/null; then
			kill -TERM "$pid" 2>/dev/null || :
		fi
		if collect_group_alive "$pid"; then
			kill -TERM -- -"$pid" 2>/dev/null || :
		fi
		if kill -0 "$pid" 2>/dev/null || collect_group_alive "$pid"; then
			sleep 1
		fi
		if kill -0 "$pid" 2>/dev/null; then
			kill -KILL "$pid" 2>/dev/null || :
		fi
		if collect_group_alive "$pid"; then
			kill -KILL -- -"$pid" 2>/dev/null || :
		fi
		wait "$pid" 2>/dev/null || :
		iostat_pid=
	fi
	if [[ -n $iostat_tmpdir ]]; then
		rm -rf "$iostat_tmpdir"
		iostat_tmpdir=
	fi
}

function test_iostat_failure
{
	trap 'log_fail "Collector lifecycle interrupted"' INT TERM
	typeset interrupted=0
	iostat_tmpdir=$(mktemp -d)
	typeset calls="$iostat_tmpdir/calls"
	typeset status_file="$iostat_tmpdir/status"
	typeset iostat_rc attempts=0

	log_onexit_push cleanup_iostat_failure
	cat > "$iostat_tmpdir/zpool" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$ZPOOL_CALLS"
exit 42
EOF
	chmod 755 "$iostat_tmpdir/zpool"

	trap 'interrupted=1' INT TERM
	PATH="$iostat_tmpdir:$PATH" PERFPOOL=test ZPOOL_CALLS="$calls" \
		zfs_test_setpgid sh -c '
		"$1" > "$2" 2>&1
		rc=$?
		printf "%s\n" "$rc" > "$3"
	' sh "$PERF_SCRIPTS/iostat.sh" "$iostat_tmpdir/output" \
		"$status_file" &
	iostat_pid=$!
	trap 'log_fail "Collector lifecycle interrupted"' INT TERM
	(( interrupted == 0 )) || log_fail "Collector lifecycle interrupted"
	while [[ ! -f $status_file ]] && (( attempts < 5 )); do
		sleep 1
		((attempts += 1))
	done
	if [[ ! -f $status_file ]]; then
		cleanup_iostat_failure
		log_onexit_pop
		log_fail "iostat did not stop after a failed sample"
	fi
	wait "$iostat_pid" 2>/dev/null || :
	iostat_pid=
	iostat_rc=$(cat "$status_file")
	(( iostat_rc == 42 )) || log_fail "iostat hid a sample failure"
	(( $(wc -l < "$calls") == 1 )) || \
		log_fail "iostat retried a failed sample"
	cleanup_iostat_failure
	log_onexit_pop
}

function cleanup
{
	trap "" INT TERM
	if [[ -n $startup_pid ]]; then
		kill -TERM "$startup_pid" 2>/dev/null || :
		wait "$startup_pid" 2>/dev/null || :
	fi
	[[ -z $startup_tmpdir ]] || rm -rf "$startup_tmpdir"
	do_collect_scripts_cleanup
	rm -f "$(get_perf_output_dir)"/collector_lifecycle.*
}

log_onexit cleanup
trap 'log_fail "Collector lifecycle interrupted"' INT TERM

function run_collector
{
	trap perf_signal INT TERM
	typeset command=$1
	typeset tag=$2

	collect_scripts=("$command" "$tag")
	log_onexit_push do_collect_scripts_cleanup
	log_must do_collect_scripts "$tag"
	for start_file in "${collect_start_files[@]}"; do
		: > "$start_file"
	done
}

function stop_collector
{
	trap perf_signal INT TERM
	typeset expected=$1

	rc=0
	do_collect_scripts_stop || rc=$?
	log_onexit_pop
	(( rc == expected ))
}

test_iostat_failure

function test_interrupted_startup
{
	trap 'log_fail "Collector lifecycle interrupted"' INT TERM
	typeset interrupted=0
	startup_tmpdir=$(mktemp -d)
	cat > "$startup_tmpdir/startup.ksh" <<'EOF'
#!/bin/ksh
. $STF_SUITE/include/libtest.shlib
. $STF_SUITE/tests/perf/perf.shlib
log_onexit do_collect_scripts_cleanup
export ZFS_TEST_SETPGID_DELAY=5
collect_scripts=('printf interrupted; exec sleep 30' interrupted)
print started > started
do_collect_scripts interrupted
log_fail "Expected startup interruption"
EOF
	trap 'interrupted=1' INT TERM
	(
		cd "$startup_tmpdir" || exit 1
		exec ksh ./startup.ksh
	) &
	startup_pid=$!
	trap 'log_fail "Collector lifecycle interrupted"' INT TERM
	(( interrupted == 0 )) || log_fail "Collector lifecycle interrupted"
	typeset attempts=0 interrupted_rc=0
	while [[ ! -f $startup_tmpdir/started ]] && (( attempts < 5 )); do
		sleep 1
		((attempts += 1))
	done
	[[ -f $startup_tmpdir/started ]] || log_fail "Startup child did not begin"
	sleep 1
	kill -TERM "$startup_pid" || log_fail "Cannot interrupt startup child"
	wait "$startup_pid" || interrupted_rc=$?
	startup_pid=
	(( interrupted_rc != 0 )) || log_fail "Startup interruption was ignored"
	sleep 6
	for marker in "$startup_tmpdir"/perf_data/*.{start,stop,ready}; do
		[[ ! -e $marker ]] || log_fail "Startup marker survived interruption"
	done
	rm -rf "$startup_tmpdir"
	startup_tmpdir=
}

# Parent-only TERM must reach the trap inside the collector's function scope.
test_interrupted_startup

# A required collector failure remains visible when it leaves a descendant.
run_collector 'printf failure; sleep 30 & exit 42' failed
stop_collector 1 || log_fail "required collector failure was hidden"

# A collector terminated by the harness with SIGTERM has Ksh's signal status.
run_collector 'printf term; exec sleep 30' term
stop_collector 0 || log_fail "expected SIGTERM termination was rejected"

# A collector that ignores SIGTERM is escalated to SIGKILL and still succeeds.
run_collector 'printf kill; trap "" TERM; while :; do sleep 1; done' kill
stop_collector 0 || log_fail "expected SIGKILL termination was rejected"

# Delay process-group creation so shutdown coverage depends on the readiness
# handshake rather than a race between launch and the first stop check.
export ZFS_TEST_SETPGID_DELAY=2
run_collector 'printf delayed; exec sleep 30' delayed
unset ZFS_TEST_SETPGID_DELAY
stop_collector 0 || log_fail "delayed collector did not shut down cleanly"

log_pass "collector lifecycle harness coverage passed"

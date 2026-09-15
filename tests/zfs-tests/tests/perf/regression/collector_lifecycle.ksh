#!/bin/ksh
# SPDX-License-Identifier: CDDL-1.0

. $STF_SUITE/include/libtest.shlib
. $STF_SUITE/tests/perf/perf.shlib

verify_runnable "global"

export PERF_RUNTIME=1
export PERF_COLLECT_SCRIPTS=
export PERF_COLLECT_OPTIONAL_SCRIPTS=
export SUDO_COMMAND=collector_lifecycle.ksh

typeset rc

function cleanup
{
	do_collect_scripts_cleanup
	rm -f "$(get_perf_output_dir)"/collector_lifecycle.*
}

log_onexit cleanup

function run_collector
{
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
	typeset expected=$1

	rc=0
	do_collect_scripts_stop || rc=$?
	log_onexit_pop
	(( rc == expected ))
}

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

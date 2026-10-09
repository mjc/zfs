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
#

#
# Description:
# Measure forced ARC decompression with independent worker files, repeated
# cache-enabled/disabled pairs and deliberately undersized context caches.
#

. $STF_SUITE/include/libtest.shlib
. $STF_SUITE/tests/perf/perf.shlib

verify_runnable "global"
is_linux || log_unsupported "The DCtx measurement runner requires Linux"
[[ $(systemd-detect-virt) == kvm ]] || \
    log_unsupported "Run the benchmark in a disposable KVM guest"
typeset root_type
root_type=$(findmnt -n -o FSTYPE /) || \
    log_unsupported "Cannot determine the guest root filesystem"
[[ -n $root_type && $root_type != zfs ]] || \
    log_unsupported "Refusing a guest with a ZFS root"
command -v fio >/dev/null || log_unsupported "fio missing"
command -v python3 >/dev/null || log_unsupported "python3 missing"

function cleanup
{
	trap '' SIGTERM SIGINT
	# Stop and reap the runner before restoring controls or destroying its pool.
	if [[ -n $runner_pid ]]; then
		kill -TERM "$runner_pid" 2>/dev/null
		wait "$runner_pid"
		runner_pid=
	fi
	[[ -n $dbuf_before ]] && \
	    print "$dbuf_before" > /sys/module/zfs/parameters/dbuf_cache_max_bytes
	[[ -n $prefetch_before ]] && \
	    set_tunable32 PREFETCH_DISABLE "$prefetch_before"
	if poolexists "$PERFPOOL"; then
		destroy_pool "$PERFPOOL"
	fi
}

function run_runner
{
	# ksh function scope does not inherit the caller's signal traps.
	typeset interrupted=0
	trap 'interrupted=1' SIGTERM SIGINT
	python3 "$runner" "$@" &
	runner_pid=$!
	trap 'log_fail "Measure zstd decompression interrupted"' SIGTERM SIGINT
	(( interrupted == 0 )) || log_fail "Measure zstd decompression interrupted"
	wait "$runner_pid"
	typeset rc=$?
	runner_pid=
	return $rc
}

log_onexit cleanup
trap 'log_fail "Measure zstd decompression interrupted"' SIGTERM SIGINT

typeset runner="$PERF_SCRIPTS/zstd_dctx_bench.py"
typeset out=$(mktemp -d "$(get_perf_output_dir)/zstd-dctx.XXXXXX")
typeset build=${PERF_ZSTD_BUILD:-reuse}
typeset levels=${PERF_ZSTD_LEVELS:-${PERF_ZSTD_LEVEL:-'fast 3 7 19'}}
typeset corpora=${PERF_ZSTD_CORPORA:-'high moderate incompressible mixed'}
typeset workers=${PERF_NTHREADS:-'1 2 4 8 12 16 24 32'}
typeset cpus=${PERF_ZSTD_CPUS:-$(python3 -c \
    'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0)))))')}
typeset -a selected_workers
selected_workers=($(python3 -c '
import sys
n = len(sys.argv[1].split(","))
print(" ".join(map(str, sorted({int(w) for w in sys.argv[2:]
                               if 0 < int(w) <= n}))))
' "$cpus" $workers))
(( ${#selected_workers[@]} > 0 )) || log_fail "No available worker counts"
typeset -a physical_args
[[ -n $PERF_ZSTD_PHYSICAL_WORKERS ]] && \
    physical_args=(--physical-workers "$PERF_ZSTD_PHYSICAL_WORKERS")
[[ -r $PERF_ZSTD_MANIFEST && -r $PERF_ZSTD_PLACEMENT ]] || \
    log_fail "Provide PERF_ZSTD_MANIFEST and PERF_ZSTD_PLACEMENT evidence"

log_must run_runner plan --study sweep --builds "$build" --levels $levels \
    --corpora $corpora \
    --workers "${selected_workers[@]}" "${physical_args[@]}" \
    --rounds "${PERF_ZSTD_ROUNDS:-5}" --seconds "${PERF_ZSTD_RUNTIME:-10}" \
    --ramp "${PERF_ZSTD_RAMP:-2}" --output "$out/plan.json"

# This workload must decode from compressed ARC on each read. A large data
# dbuf cache would silently turn it into a decompressed-buffer benchmark.
typeset dbuf_before=$(cat /sys/module/zfs/parameters/dbuf_cache_max_bytes)
typeset prefetch_before=$(get_tunable PREFETCH_DISABLE)
print 0 > /sys/module/zfs/parameters/dbuf_cache_max_bytes || \
    log_fail "Cannot disable the data dbuf cache"
log_must set_tunable32 PREFETCH_DISABLE 1
export PERF_FS_OPTS='-o atime=off -o secondarycache=none'
# Disposable devices must support 512-byte sectors for compressed 4 KiB data.
for disk in $DISKS; do
	[[ -e $disk ]] || disk="$DEV_DSKDIR/$disk"
	if [[ -b $disk ]]; then
		[[ $(blockdev --getss "$disk") == 512 ]] || \
		    log_fail "4 KiB matrix requires 512-byte test devices"
	fi
done
recreate_perf_pool -o feature@embedded_data=disabled -o ashift=9
log_must zfs create "$PERFPOOL/dctx"
log_must run_runner prepare --plan "$out/plan.json" \
    --dataset "$PERFPOOL/dctx" --output "$out/preparation"

# Profiler counters and tracing must run in separate diagnostic invocations.
log_must run_runner batch --plan "$out/plan.json" \
    --fixture "$out/preparation/fixture.json" --output "$out/results" \
    --build-manifest "$PERF_ZSTD_MANIFEST" --cpus "$cpus" \
    --placement "$PERF_ZSTD_PLACEMENT"
log_must run_runner report --plan "$out/plan.json" \
    --results "$out/results" --output "$out/analysis"
log_note "DCtx raw measurements and paired analysis: $out"
log_pass "Measure zstd decompression"

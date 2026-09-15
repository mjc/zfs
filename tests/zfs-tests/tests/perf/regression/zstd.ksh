#!/bin/ksh
# SPDX-License-Identifier: CDDL-1.0

#
# This file and its contents are supplied under the terms of the
# Common Development and Distribution License (CDDL), version 1.0.
# You may only use this file in accordance with the terms of version
# 1.0 of the CDDL.
#
# A full copy of the text of the CDDL should have accompanied this
# source.  A copy of the CDDL is also available via the Internet at
# https://opensource.org/license/CDDL-1.0.
#

#
# Description:
# Run a fixed 128 KiB zstd compression workload through the existing kernel
# performance harness while collecting zstd kstat snapshots. This is the
# first Phase 1 baseline; it deliberately measures the kernel path rather than
# introducing a separate userland benchmark.
#

. $STF_SUITE/include/libtest.shlib
. $STF_SUITE/tests/perf/perf.shlib

command -v fio > /dev/null || log_unsupported "fio missing"

function cleanup
{
	recreate_perf_pool
}

trap "log_fail \"Measure zstd compression lifecycle baseline\"" SIGTERM
log_onexit cleanup

typeset zstd_level=${PERF_ZSTD_LEVEL:-3}
typeset zstd_runtime=${PERF_ZSTD_RUNTIME:-30}

export PERF_RUNTIME=$zstd_runtime
export PERF_NTHREADS=${PERF_NTHREADS:-'1'}
export PERF_NTHREADS_PER_FS=${PERF_NTHREADS_PER_FS:-'0'}
export PERF_IOSIZES=${PERF_IOSIZES:-'128k'}
export PERF_SYNC_TYPES=${PERF_SYNC_TYPES:-'0'}
export PERF_FS_OPTS="-o recsize=128k -o compress=zstd-$zstd_level \
    -o checksum=sha256 -o redundant_metadata=most"

recreate_perf_pool
populate_perf_filesystems

# Aim to fill the pool to 50% capacity while accounting for a 3x compressratio.
typeset -i TOTAL_SIZE
(( TOTAL_SIZE = $(get_prop avail "$PERFPOOL") * 3 / 2 ))
export TOTAL_SIZE

if is_linux; then
	[[ -r /proc/spl/kstat/zfs/zstd ]] || \
	    log_unsupported "Linux zstd kstat is unavailable"

	export collect_scripts=(
	    "$PERF_SCRIPTS/zstd_iostat.sh" "zpool.iostat"
	    "$PERF_SCRIPTS/zstd_kstat.sh" "zstd.kstat"
	    "$PERF_SCRIPTS/zstd_perf.sh" "perf"
	    "$PERF_SCRIPTS/zstd_vmstat.sh" "vmstat"
	)
else
	export collect_scripts=(
	    "$PERF_SCRIPTS/zstd_kstat.sh" "zstd.kstat"
	    "$PERF_SCRIPTS/zstd_vmstat.sh" "vmstat"
	)
fi

log_note "Zstd compression with settings: $(print_perf_settings)"
log_note "Zstd level: $zstd_level"
do_fio_run sequential_writes.fio false false
log_pass "Measure zstd compression lifecycle baseline"

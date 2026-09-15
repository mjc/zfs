#!/bin/ksh
# SPDX-License-Identifier: CDDL-1.0

#
# This file and its contents are supplied under the terms of the
# Common Development and Distribution License (CDDL), version 1.0.
# You may only use this file in accordance with the terms of version
# 1.0 of the CDDL.
#
# A full copy of the CDDL should have accompanied this source. A copy is also
# available at https://opensource.org/license/CDDL-1.0.
#

#
# Description:
# Prepare zstd-compressed files, warm them into the ARC, then measure reads that
# must decompress them. This isolates decoder work from backing-device latency.
#

. $STF_SUITE/include/libtest.shlib
. $STF_SUITE/tests/perf/perf.shlib

command -v fio > /dev/null || log_unsupported "fio missing"

function cleanup
{
	clear_zinject_delays
	if poolexists "$PERFPOOL"; then
		destroy_pool "$PERFPOOL"
	fi
}

trap "log_fail \"Measure zstd decompression\"" SIGTERM
log_onexit cleanup

typeset zstd_level=${PERF_ZSTD_LEVEL:-3}
typeset zstd_runtime=${PERF_ZSTD_RUNTIME:-30}

export PERF_RUNTIME=$zstd_runtime
export PERF_NTHREADS=${PERF_NTHREADS:-'1'}
export PERF_NTHREADS_PER_FS=${PERF_NTHREADS_PER_FS:-'0'}
export PERF_IOSIZES=${PERF_IOSIZES:-'128k'}
export PERF_SYNC_TYPES=${PERF_SYNC_TYPES:-'0'}
export PERF_FS_OPTS="-o recsize=128k -o compress=zstd-$zstd_level \
	-o primarycache=all \
	-o checksum=sha256 -o redundant_metadata=most"

recreate_perf_pool
populate_perf_filesystems

# Prepare enough fixed-size files for the largest read-run concurrency. Keep
# the logical workload below the uncompressed pool capacity so preparation does
# not depend on the codec's compression ratio.
typeset threads=$(get_max $PERF_NTHREADS)
TOTAL_SIZE=$(get_zstd_workload_size \
	"$(get_prop avail "$PERFPOOL")" "$PERF_COMPPERCENT") || \
	log_fail "Invalid PERF_COMPPERCENT: $PERF_COMPPERCENT"
export TOTAL_SIZE
export NUMJOBS=$threads
(( FILE_SIZE = TOTAL_SIZE / threads ))
export FILE_SIZE
export DIRECTORY=$(get_directory)
export SYNC_TYPE=0
export BLOCKSIZE=128k
export DIRECT=0
log_must fio --output-format="${PERF_FIO_FORMAT:-json}" \
	--output /dev/null "$FIO_SCRIPTS/mkfiles.fio"

# Warm the compressed blocks into the ARC before starting collectors. The timed
# run is then a decoder benchmark; zpool.iostat should show no device reads.
export RUNTIME=${PERF_WARMUP_RUNTIME:-30}
log_must fio --output-format="${PERF_FIO_FORMAT:-json}" \
	--output /dev/null "$FIO_SCRIPTS/sequential_reads.fio"

if is_linux; then
	[[ -r /proc/spl/kstat/zfs/zstd ]] || \
	    log_unsupported "Linux zstd kstat is unavailable"

	export collect_scripts=(
	    "$PERF_SCRIPTS/zstd_iostat.sh" "zpool.iostat"
	    "$PERF_SCRIPTS/zstd_kstat.sh" "zstd.kstat"
	    "$PERF_SCRIPTS/zstd_vmstat.sh" "vmstat"
	)
	if command -v perf > /dev/null; then
		collect_scripts+=("$PERF_SCRIPTS/zstd_perf.sh" "perf")
	else
		log_note "perf missing; skipping optional profiling"
	fi
else
	export collect_scripts=(
	    "$PERF_SCRIPTS/zstd_kstat.sh" "zstd.kstat"
	    "$PERF_SCRIPTS/zstd_vmstat.sh" "vmstat"
	)
fi

log_note "Zstd decompression with settings: $(print_perf_settings)"
log_note "Zstd level: $zstd_level"
log_note "Zstd decompression benchmark is ARC-warmed"
do_fio_run sequential_reads.fio false false
log_pass "Measure zstd decompression"

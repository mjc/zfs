#!/bin/ksh -p
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

. $STF_SUITE/include/libtest.shlib

verify_runnable "both"

typeset zstd_cache_timeout
typeset zstd_cache_reap_interval
typeset zstd_cache_file="$TESTDIR/zstd-dctx-cache"
typeset zstd_cache_expected="${TMPDIR:-/tmp}/zstd-dctx-cache.expected.$$"
typeset zstd_cache_actual="${TMPDIR:-/tmp}/zstd-dctx-cache.actual.$$"

function cleanup
{
	log_must zinject -c all
	if [[ -n $zstd_cache_timeout ]]; then
		log_must set_tunable32 ZSTD_CACHE_TIMEOUT $zstd_cache_timeout
		log_must set_tunable32 ZSTD_CACHE_REAP_INTERVAL $zstd_cache_reap_interval
	fi
	zfs set primarycache=all $TESTPOOL/$TESTFS
	rm -f "$zstd_cache_file" "$zstd_cache_expected" "$zstd_cache_actual"
}

function verify_read
{
	log_must dd if="$zstd_cache_file" of="$zstd_cache_actual" bs=128K
	log_must cmp "$zstd_cache_expected" "$zstd_cache_actual"
}

log_assert "Concurrent zstd reads reuse initialized decompression contexts"
log_onexit cleanup

zstd_cache_timeout=$(get_tunable ZSTD_CACHE_TIMEOUT)
zstd_cache_reap_interval=$(get_tunable ZSTD_CACHE_REAP_INTERVAL)
log_must set_tunable32 ZSTD_CACHE_TIMEOUT 1
log_must set_tunable32 ZSTD_CACHE_REAP_INTERVAL 1

log_must zfs set compression=zstd-3 $TESTPOOL/$TESTFS
log_must zfs set recordsize=128K $TESTPOOL/$TESTFS
log_must zfs set primarycache=metadata $TESTPOOL/$TESTFS
log_must file_write -o create -f "$zstd_cache_expected" -b $((128 * 1024)) \
	-c 4096 -d 0
log_must cp "$zstd_cache_expected" "$zstd_cache_file"
log_must sync

typeset reap_before=$(kstat zstd.decompress_context_reap)
log_must zinject -a
verify_read
typeset reap_after=$reap_before
for i in $(seq 1 10); do
	sleep 1
	reap_after=$(kstat zstd.decompress_context_reap)
	(( reap_after > reap_before )) && break
done
(( reap_after > reap_before )) || \
	log_fail "idle decompression context was not reaped"

# The counter is global, so this integration test does not infer object
# identity or exactly-once reaping from later samples.

# Keep one cached context available for the failure/reuse check. The cache was
# idle and reaped above, so these sequential reads cannot select another busy
# context and mask a release failure.
log_must set_tunable32 ZSTD_CACHE_TIMEOUT 60
verify_read

# An injected read failure must release the cached context before it can be
# reused. The injection happens after decompression, so this covers the ZFS
# error path rather than a ZSTD decoder error.
typeset reuse_before_failure=$(kstat zstd.decompress_context_reuse)
log_must zinject -a -t data -e decompress -f 100 \
	"$zstd_cache_file"
log_mustnot dd if="$zstd_cache_file" of=/dev/null bs=128K
log_must zinject -c all
verify_read
typeset reuse_after_failure=$(kstat zstd.decompress_context_reuse)
(( reuse_after_failure > reuse_before_failure )) || \
	log_fail "failed read did not reuse its released context"

typeset -a pids
for i in $(seq 1 32); do
	dd if="$zstd_cache_file" of=/dev/null bs=128K &
	pids+=($!)
done
for pid in ${pids[*]}; do
	log_must wait $pid
done

log_must zinject -a
verify_read

log_pass "Concurrent zstd reads reused initialized decompression contexts"

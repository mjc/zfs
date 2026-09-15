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

function cleanup
{
	log_must zinject -c all
	if [[ -n $zstd_cache_timeout ]]; then
		log_must set_tunable32 ZSTD_CACHE_TIMEOUT $zstd_cache_timeout
		log_must set_tunable32 ZSTD_CACHE_REAP_INTERVAL $zstd_cache_reap_interval
	fi
	zfs set primarycache=all $TESTPOOL/$TESTFS
	rm -f "$TESTDIR/zstd-dctx-cache"
}

log_assert "Concurrent zstd reads reuse initialized decompression contexts"
log_onexit cleanup

zstd_cache_timeout=$(get_tunable ZSTD_CACHE_TIMEOUT)
zstd_cache_reap_interval=$(get_tunable ZSTD_CACHE_REAP_INTERVAL)
log_must set_tunable32 ZSTD_CACHE_TIMEOUT 1
log_must set_tunable32 ZSTD_CACHE_REAP_INTERVAL 1

log_must zfs set compression=zstd-3 $TESTPOOL/$TESTFS
log_must zfs set primarycache=metadata $TESTPOOL/$TESTFS
log_must file_write -o create -f "$TESTDIR/zstd-dctx-cache" -b 128K \
	-c 4096 -d 13
log_must sync

typeset reap_before=$(kstat zstd.decompress_context_reap)
log_must zinject -a
log_must dd if="$TESTDIR/zstd-dctx-cache" of=/dev/null bs=128K
typeset reap_after=$reap_before
for i in $(seq 1 10); do
	sleep 1
	reap_after=$(kstat zstd.decompress_context_reap)
	(( reap_after > reap_before )) && break
done
(( reap_after > reap_before )) || \
	log_fail "idle decompression context was not reaped"
sleep 2
(( $(kstat zstd.decompress_context_reap) == reap_after )) || \
	log_fail "reaped decompression context was counted more than once"

# Keep one cached context available for the failure/reuse check. The cache was
# idle and reaped above, so these sequential reads cannot select another busy
# context and mask a release failure.
log_must set_tunable32 ZSTD_CACHE_TIMEOUT 60
log_must dd if="$TESTDIR/zstd-dctx-cache" of=/dev/null bs=128K

# An injected read failure must release the cached context before it can be
# reused. The injection happens after decompression, so this covers the ZFS
# error path rather than a ZSTD decoder error.
typeset reuse_before_failure=$(kstat zstd.decompress_context_reuse)
log_must zinject -a -t data -e decompress -f 100 \
	"$TESTDIR/zstd-dctx-cache"
log_mustnot dd if="$TESTDIR/zstd-dctx-cache" of=/dev/null bs=128K
log_must zinject -c all
log_must dd if="$TESTDIR/zstd-dctx-cache" of=/dev/null bs=128K
typeset reuse_after_failure=$(kstat zstd.decompress_context_reuse)
(( reuse_after_failure == reuse_before_failure + 1 )) || \
	log_fail "failed read did not reuse its released context"

typeset create_before=$(kstat zstd.decompress_context_create)

typeset -a pids
for i in $(seq 1 32); do
	dd if="$TESTDIR/zstd-dctx-cache" of=/dev/null bs=128K &
	pids+=($!)
done
for pid in ${pids[*]}; do
	log_must wait $pid
done

typeset create_after=$(kstat zstd.decompress_context_create)
typeset reuse_after=$(kstat zstd.decompress_context_reuse)
(( create_after > create_before )) || \
	log_fail "concurrent reads did not create a decompression context"

log_must zinject -a
log_must dd if="$TESTDIR/zstd-dctx-cache" of=/dev/null bs=128K
typeset reuse_final=$(kstat zstd.decompress_context_reuse)

(( reuse_final > reuse_after )) || \
	log_fail "repeated reads did not reuse a decompression context"

log_pass "Concurrent zstd reads reused initialized decompression contexts"

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

typeset zstd_cache_file="$TESTDIR/zstd-dctx-cache"
typeset zstd_cache_expected="${TMPDIR:-/tmp}/zstd-dctx-cache.expected.$$"
typeset zstd_cache_actual="${TMPDIR:-/tmp}/zstd-dctx-cache.actual.$$"

function cleanup
{
	log_must zinject -c all
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

log_must zfs set compression=zstd-3 $TESTPOOL/$TESTFS
log_must zfs set recordsize=128K $TESTPOOL/$TESTFS
log_must zfs set primarycache=metadata $TESTPOOL/$TESTFS
log_must file_write -o create -f "$zstd_cache_expected" -b $((128 * 1024)) \
	-c 4096 -d 0
log_must cp "$zstd_cache_expected" "$zstd_cache_file"
log_must sync

# An injected read failure must release the cached context before it can be
# reused. The injection happens after decompression, so this covers the ZFS
# error path rather than a ZSTD decoder error.
log_must zinject -a
verify_read
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

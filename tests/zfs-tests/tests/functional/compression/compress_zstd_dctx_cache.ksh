#!/bin/ksh -p
# shellcheck disable=SC2154
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

. "$STF_SUITE/include/libtest.shlib"

verify_runnable "both"

typeset zstd_cache_file="$TESTDIR/zstd-dctx-cache"
typeset zstd_cache_expected="${TMPDIR:-/tmp}/zstd-dctx-cache.expected.$$"
typeset zstd_cache_actual="${TMPDIR:-/tmp}/zstd-dctx-cache.actual.$$"
typeset zstd_cache_slice_prefix="${TMPDIR:-/tmp}/zstd-dctx-cache.slice.$$"
typeset -a pids

function stop_readers
{
	for pid in "${pids[@]}"; do
		kill -TERM "$pid" 2>/dev/null || :
	done
	for pid in "${pids[@]}"; do
		wait "$pid" 2>/dev/null || :
	done
	pids=()
}

function cleanup
{
	stop_readers
	log_must zinject -c all
	zfs set primarycache=all "${TESTPOOL}/${TESTFS}"
	rm -f "$zstd_cache_file" "$zstd_cache_expected" "$zstd_cache_actual" \
	    "$zstd_cache_slice_prefix".*
}

function verify_read
{
	log_must dd if="$zstd_cache_file" of="$zstd_cache_actual" bs=128K
	log_must cmp "$zstd_cache_expected" "$zstd_cache_actual"
}

log_assert "Initialized zstd decompression contexts remain correct on reuse"
log_onexit cleanup

log_must zfs set compression=zstd-3 "${TESTPOOL}/${TESTFS}"
log_must zfs set recordsize=128K "${TESTPOOL}/${TESTFS}"
log_must zfs set primarycache=metadata "${TESTPOOL}/${TESTFS}"
log_must zstd_dctx_test
log_must file_write -o create -f "$zstd_cache_expected" -b $((128 * 1024)) \
	-c 1 -d 0
typeset pattern
for i in $(seq 1 31); do
	(( pattern = i % 256 ))
	log_must file_write -o append -f "$zstd_cache_expected" \
		-b $((128 * 1024)) -c 1 -d "$pattern"
done
log_must cp "$zstd_cache_expected" "$zstd_cache_file"
log_must sync

# This serial injected fault covers the ZFS post-decompression error path. It
# verifies that a later valid read succeeds and that reuse remains active; the
# global counter does not identify the context used by either operation. A
# malformed-frame test covers the ZSTD decoder error path separately.
log_must zinject -a
verify_read
typeset reuse_before_failure
reuse_before_failure=$(kstat zstd.decompress_context_reuse) || \
	log_fail "could not read zstd reuse counter"
log_must zinject -a -t data -e decompress -f 100 \
	"$zstd_cache_file"
log_mustnot dd if="$zstd_cache_file" of=/dev/null bs=128K
log_must zinject -c all
verify_read
typeset reuse_after_failure
reuse_after_failure=$(kstat zstd.decompress_context_reuse) || \
	log_fail "could not read zstd reuse counter"
(( reuse_after_failure > reuse_before_failure )) || \
	log_fail "failed read did not reuse its released context"

typeset records_per_reader=1
typeset reader_bytes
(( reader_bytes = records_per_reader * 128 * 1024 ))
for i in $(seq 0 31); do
	(( start = i * records_per_reader ))
	typeset expected_slice="$zstd_cache_slice_prefix.$i.expected"
	typeset actual_slice="$zstd_cache_slice_prefix.$i.actual"
	(
		dd if="$zstd_cache_expected" of="$expected_slice" bs=128K \
			skip="$start" count="$records_per_reader" 2>/dev/null || exit 1
		dd if="$zstd_cache_file" of="$actual_slice" bs=128K \
			skip="$start" count="$records_per_reader" 2>/dev/null || exit 1
		(( $(wc -c < "$expected_slice") == reader_bytes )) || exit 1
		(( $(wc -c < "$actual_slice") == reader_bytes )) || exit 1
		cmp "$expected_slice" "$actual_slice"
	) &
	pids+=($!)
done
typeset readers_failed=0
for pid in "${pids[@]}"; do
	wait "$pid" || readers_failed=1
done
pids=()
(( readers_failed == 0 )) || \
	log_fail "concurrent zstd reader failed"

log_pass "Concurrent zstd reads reused initialized decompression contexts"

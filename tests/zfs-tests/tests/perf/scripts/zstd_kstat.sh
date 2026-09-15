#!/bin/sh
# SPDX-License-Identifier: CDDL-1.0

case "$(uname -s)" in
Linux)
	while :; do
		date +%s
		cat /proc/spl/kstat/zfs/zstd
		sleep 1
	done
	;;
FreeBSD)
	while :; do
		date +%s
		sysctl -a kstat.zfs.misc.zstd
		sleep 1
	done
	;;
*)
	exec kstat zfs:0 1
	;;
esac

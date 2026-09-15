#!/bin/sh
# SPDX-License-Identifier: CDDL-1.0

case "$(uname -s)" in
Linux)
	exec vmstat -t 1
	;;
FreeBSD)
	exec vmstat -T d 1
	;;
*)
	exec vmstat 1
	;;
esac

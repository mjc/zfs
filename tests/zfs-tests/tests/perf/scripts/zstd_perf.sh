#!/bin/sh
# SPDX-License-Identifier: CDDL-1.0

exec perf record -F 99 -a -g -q -o /dev/stdout -- sleep "$PERF_RUNTIME"

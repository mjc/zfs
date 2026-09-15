#!/bin/sh
# SPDX-License-Identifier: CDDL-1.0

: "${PERFPOOL:?PERFPOOL must be set}"

exec zpool iostat -lpvyL "$PERFPOOL" 1

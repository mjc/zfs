#!/bin/sh
# SPDX-License-Identifier: CDDL-1.0

exec zpool iostat -lpvyL "$PERFPOOL" 1

// SPDX-License-Identifier: CDDL-1.0
/*
 * CDDL HEADER START
 *
 * The contents of this file are subject to the terms of the
 * Common Development and Distribution License (the "License").
 * You can obtain a copy of the License at usr/src/OPENSOLARIS.LICENSE
 * or https://opensource.org/licenses/CDDL-1.0.
 *
 * CDDL HEADER END
 */

#ifndef _ZFS_KIOCB_COMPAT_H
#define	_ZFS_KIOCB_COMPAT_H

#include <linux/fs.h>

static inline void
zfs_kiocb_complete(struct kiocb *kiocb, long ret)
{
#if defined(HAVE_KIOCB_COMPLETE_2ARGS)
	kiocb->ki_complete(kiocb, ret);
#else
	kiocb->ki_complete(kiocb, ret, 0);
#endif
}

#endif /* _ZFS_KIOCB_COMPAT_H */

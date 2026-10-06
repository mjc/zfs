dnl # SPDX-License-Identifier: CDDL-1.0
dnl #
dnl # Linux 5.16 API change,
dnl # kiocb->ki_complete() dropped the unused res2 argument.
dnl #
AC_DEFUN([ZFS_AC_KERNEL_SRC_KIOCB_COMPLETE], [
	ZFS_LINUX_TEST_SRC([kiocb_complete_2args], [
		#include <linux/fs.h>
	], [
		struct kiocb *kiocb = NULL;
		kiocb->ki_complete(kiocb, 0);
	])
])

AC_DEFUN([ZFS_AC_KERNEL_KIOCB_COMPLETE], [
	AC_MSG_CHECKING([whether kiocb->ki_complete() wants 2 args])
	ZFS_LINUX_TEST_RESULT([kiocb_complete_2args], [
		AC_MSG_RESULT(yes)
		AC_DEFINE(HAVE_KIOCB_COMPLETE_2ARGS, 1,
		    [kiocb->ki_complete() wants 2 args])
	], [
		AC_MSG_RESULT(no)
	])
])

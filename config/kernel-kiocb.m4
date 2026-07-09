dnl # SPDX-License-Identifier: CDDL-1.0
dnl #
dnl # Linux 6.0 API,
dnl #
dnl # The kiocb completion callback lost its unused second result argument.
dnl #
AC_DEFUN([ZFS_AC_KERNEL_SRC_KIOCB_COMPLETE], [
	ZFS_LINUX_TEST_SRC([kiocb_complete_1arg], [
		#include <linux/fs.h>
	], [
		_Static_assert(__builtin_types_compatible_p(
			typeof(((struct kiocb *)0)->ki_complete),
			void (*)(struct kiocb *, long)),
			"ki_complete has one argument");
	])
])

AC_DEFUN([ZFS_AC_KERNEL_KIOCB_COMPLETE], [
	AC_MSG_CHECKING([whether kiocb->ki_complete() takes one result argument])
	ZFS_LINUX_TEST_RESULT([kiocb_complete_1arg], [
		AC_MSG_RESULT(yes)
		AC_DEFINE(HAVE_KIOCB_COMPLETE_1ARG, 1,
		    [kiocb->ki_complete() takes one result argument])
	], [
		AC_MSG_RESULT(no)
	])
])

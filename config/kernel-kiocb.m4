dnl # SPDX-License-Identifier: CDDL-1.0
dnl #
dnl # Linux 6.0 API,
dnl #
dnl # The kiocb completion callback lost its unused second result argument.
dnl #
AC_DEFUN([ZFS_AC_KERNEL_SRC_KIOCB_COMPLETE], [
	ZFS_LINUX_TEST_SRC([kiocb_complete_2args], [
		#include <linux/fs.h>
	], [
		_Static_assert(__builtin_types_compatible_p(
			typeof(((struct kiocb *)0)->ki_complete),
			void (*)(struct kiocb *, long)),
			"ki_complete has two arguments");
	])
])

AC_DEFUN([ZFS_AC_KERNEL_KIOCB_COMPLETE], [
	AC_MSG_CHECKING([whether kiocb->ki_complete() takes two arguments])
	ZFS_LINUX_TEST_RESULT([kiocb_complete_2args], [
		AC_MSG_RESULT(yes)
		AC_DEFINE(HAVE_KIOCB_COMPLETE_2ARGS, 1,
		    [kiocb->ki_complete() takes two arguments])
	], [
		AC_MSG_RESULT(no)
	])
])

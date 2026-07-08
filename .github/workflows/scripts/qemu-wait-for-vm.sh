#!/bin/bash
#
# Wait for a VM to boot up and become active.  This is used in a number of our
# scripts.
#
# $1: VM hostname or IP address

TARGET="$1"
case "$TARGET" in
  vm0) TARGET="192.168.122.10" ;;
  vm1) TARGET="192.168.122.11" ;;
  vm2) TARGET="192.168.122.12" ;;
esac

while pidof qemu-system-x86_64 >/dev/null; do
  ssh 2>/dev/null zfs@"$TARGET" "uname -a" && break
done

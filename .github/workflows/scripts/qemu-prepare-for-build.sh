#!/usr/bin/env bash

# Helper script to run after installing dependencies.  This brings the VM back
# up and copies over the zfs source directory.
echo "Build modules in QEMU machine"
sudo virsh start openzfs
.github/workflows/scripts/qemu-wait-for-vm.sh vm0
VM_IP="192.168.122.10"
rsync -ar $HOME/work/zfs/zfs zfs@$VM_IP:./

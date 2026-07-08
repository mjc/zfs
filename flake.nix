{
  description = "OpenZFS development shell and QEMU testing environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        commonTools = with pkgs; [
          acl
          attr
          autoconf
          automake
          bashInteractive
          bc
          binutils
          bison
          bzip2
          cloud-utils
          coreutils
          cpio
          cryptsetup
          curl
          daemonize
          e2fsprogs
          findutils
          fio
          gawk
          gcc
          gettext
          git
          gnumake
          iproute2
          jq
          kmod
          ksh
          lsscsi
          lvm2
          gptfdisk
          openssh
          openssl
          parted
          perl
          pkg-config
          procps
          python3
          wget
          gnused
          gnutar
          gnugrep
          sudo
          cronie
          rsync
          shadow
          sysstat
          util-linux
          xfsprogs
          xxh
          lz4
          xz
          zstd
          libaio
          libcap
          libtool
          libtirpc
          linuxHeaders
          zlib
          perf
          zfs
        ];

        qemuTools = with pkgs; [
          axel
          libguestfs
          libvirt
          OVMF
          qemu
          qemu_kvm
          virt-manager
        ];

        dedupe = pkgs.lib.unique;

        defaultHook = ''
          cat <<'EOF'
          OpenZFS development shell loaded.

          - Build helpers:
            - ./autogen.sh && ./configure ...
            - make -j$(nproc)
            - make install
            - ./scripts/zfs-tests.sh [args]

          Q: if you need QEMU flow deps, switch to:
             nix develop .#qemu
          EOF
        '';

        qemuHook = ''
          cat <<'EOF'
          OpenZFS QEMU testing shell loaded.

            Host-side scripts expected by CI are:
            .github/workflows/scripts/qemu-1-setup.sh
            scripts/zfs-qemu-1-setup-nix.sh
            .github/workflows/scripts/qemu-2-start.sh
            .github/workflows/scripts/qemu-3-deps.sh
            .github/workflows/scripts/qemu-4-build.sh
            .github/workflows/scripts/qemu-5-setup.sh
            .github/workflows/scripts/qemu-6-tests.sh
            .github/workflows/scripts/qemu-7-prepare.sh

          Common sequence:
            bash scripts/zfs-qemu-1-setup-nix.sh
            bash .github/workflows/scripts/qemu-2-start.sh
            bash .github/workflows/scripts/qemu-3-deps.sh
            bash .github/workflows/scripts/qemu-4-build.sh
            bash .github/workflows/scripts/qemu-5-setup.sh
            bash .github/workflows/scripts/qemu-6-tests.sh
            bash .github/workflows/scripts/qemu-7-prepare.sh
          EOF

          if [ -c /dev/kvm ] && groups "$USER" | grep -qw kvm; then
            echo "KVM support appears available for /dev/kvm."
          else
            echo "Tip: add yourself to the kvm group for faster guest runs:"
            echo "  sudo usermod -aG kvm $USER"
          fi
        '';
      in
      {
        devShells.default = pkgs.mkShell {
          name = "zfs-dev";
          packages = dedupe commonTools;
          shellHook = defaultHook;
        };

        devShells.qemu = pkgs.mkShell {
          name = "zfs-qemu";
          packages = dedupe (commonTools ++ qemuTools);
          shellHook = qemuHook;
        };

        formatter = pkgs.nixpkgs-fmt;
      });
}

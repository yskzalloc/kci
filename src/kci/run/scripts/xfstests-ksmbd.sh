#!/bin/bash
#
# kci xfstests-ksmbd.sh — run xfstests through cifs.ko against ksmbd
# License: GPL-2.0
#
# Runs inside the virtme-ng guest as root. The guest shares the host
# filesystem, so XFSTESTS_DIR and the installed ksmbd-tools binaries are
# the host-built ones; only the kernel (cifs.ko/ksmbd built in) is the
# one under test.
#
# Storage note (GitHub CI free tier): exports live on the host disk under
# /var/tmp, not on guest tmpfs, and the caller passes a storage-light test
# list. Free space is printed before/after so ENOSPC failures are obvious.

XFSTESTS_DIR="${XFSTESTS_DIR:-$HOME/xfstests-dev}"
TESTS="${TESTS:-generic/001}"
SMB_USER="${SMB_USER:-testuser}"
SMB_PASS="${SMB_PASS:-1234}"
EXPORT_BASE="${EXPORT_BASE:-/var/tmp/kci-ksmbd}"

export PATH=$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin

# --- guest environment ---
mount -t tmpfs tmpfs /run 2>/dev/null
ip link set lo up

# ksmbd may be =y (preferred for vng) or =m
modprobe ksmbd 2>/dev/null || true
if [ ! -d /sys/module/ksmbd ] && ! grep -q ksmbd /proc/modules; then
    echo "ERROR: ksmbd not available in this kernel (need CONFIG_SMB_SERVER)"
    exit 1
fi

if ! command -v ksmbd.mountd >/dev/null; then
    echo "ERROR: ksmbd.mountd not found (install ksmbd-tools on the host)"
    exit 1
fi

# xfstests secondary users (writes the shared host /etc/passwd; harmless on CI)
id -u fsgqa >/dev/null 2>&1 || useradd -m fsgqa
id -u 123456-fsgqa >/dev/null 2>&1 || useradd 123456-fsgqa

# --- exports and mountpoints ---
mkdir -m 777 -p "$EXPORT_BASE/test" "$EXPORT_BASE/scratch"
mkdir -p /mnt/test /mnt/scratch

cat > "$EXPORT_BASE/smb.conf" <<EOF
[global]
	netbios name = KSMBD-KCI

[test]
	path = $EXPORT_BASE/test
	writeable = yes
	read only = no

[scratch]
	path = $EXPORT_BASE/scratch
	writeable = yes
	read only = no
EOF

ksmbd.adduser -P "$EXPORT_BASE/ksmbdpwd.db" -a "$SMB_USER" -p "$SMB_PASS"
mkdir -p /etc/ksmbd
ksmbd.mountd -n -C "$EXPORT_BASE/smb.conf" -P "$EXPORT_BASE/ksmbdpwd.db" \
    > "$EXPORT_BASE/ksmbd.mountd.log" 2>&1 &
sleep 2
if ! ps -e | grep -q mountd; then
    echo "ERROR: ksmbd.mountd did not start"
    cat "$EXPORT_BASE/ksmbd.mountd.log"
    exit 1
fi

# smoke-test the mount before handing over to xfstests
CIFS_OPTS="username=$SMB_USER,password=$SMB_PASS,vers=3.1.1,mfsymlinks,actimeo=0"
if ! mount -t cifs "//127.0.0.1/test" /mnt/test -o "$CIFS_OPTS"; then
    echo "ERROR: cifs mount of //127.0.0.1/test failed"
    dmesg | tail -50
    exit 1
fi
umount /mnt/test

# --- xfstests configuration (see kci/xfstesting-cifs.rst) ---
cd "$XFSTESTS_DIR" || exit 1
cat > local.config <<EOF
export FSTYP=cifs
export TEST_DEV=//127.0.0.1/test
export TEST_DIR=/mnt/test
export SCRATCH_DEV=//127.0.0.1/scratch
export SCRATCH_MNT=/mnt/scratch
export CIFS_MOUNT_OPTIONS="-o $CIFS_OPTS"
export TEST_FS_MOUNT_OPTS="-o $CIFS_OPTS"
EOF

echo "=== free space before xfstests ==="
df -h "$EXPORT_BASE" /

echo "=== XFSTESTS START ==="
# check exits nonzero on test failures; results are parsed from the summary
./check $TESTS
STATUS=$?
echo "=== XFSTESTS END ==="

echo "=== free space after xfstests ==="
df -h "$EXPORT_BASE" /

ksmbd.control -s 2>/dev/null || true
exit $STATUS

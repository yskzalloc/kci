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
#
# Every setup stage logs a "[kci-ksmbd]" line; any setup failure dumps
# full diagnostics via fail() so CI logs show the cause, not just the
# missing-summary symptom. Set KCI_DEBUG=1 for shell tracing.

XFSTESTS_DIR="${XFSTESTS_DIR:-$HOME/xfstests-dev}"
TESTS="${TESTS:-generic/001}"
SMB_USER="${SMB_USER:-testuser}"
SMB_PASS="${SMB_PASS:-1234}"
EXPORT_BASE="${EXPORT_BASE:-/var/tmp/kci-ksmbd}"

export PATH=$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin

log() { echo "[kci-ksmbd] $*"; }

fail() {
    echo "[kci-ksmbd] FATAL: $*"
    echo "[kci-ksmbd] === diagnostics ==="
    echo "--- kernel ---"
    uname -a
    echo "--- smb kernel support ---"
    grep -E "ksmbd|cifs" /proc/modules; ls -d /sys/module/ksmbd /sys/module/cifs 2>&1
    echo "--- processes ---"
    ps -e | grep -E "mountd|smbd" || echo "(no smb processes)"
    echo "--- listening sockets ---"
    ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null || echo "(no ss/netstat)"
    if [ -f "$EXPORT_BASE/ksmbd.mountd.log" ]; then
        echo "--- ksmbd.mountd.log ---"
        cat "$EXPORT_BASE/ksmbd.mountd.log"
    fi
    echo "--- dmesg tail ---"
    dmesg | tail -60
    exit 1
}

[ -n "$KCI_DEBUG" ] && set -x

log "kernel: $(uname -r)"
log "ksmbd.mountd: $(command -v ksmbd.mountd || echo MISSING)"
log "ksmbd.adduser: $(command -v ksmbd.adduser || echo MISSING)"
log "mount.cifs: $(command -v mount.cifs || echo MISSING)"
log "xfstests: $XFSTESTS_DIR"
log "exports: $EXPORT_BASE"
log "tests (${TESTS:+$(echo $TESTS | wc -w)}): $(echo $TESTS | cut -c1-120)..."

# --- guest environment ---
mount -t tmpfs tmpfs /run 2>/dev/null
ip link set lo up || fail "cannot bring up loopback"

# ksmbd may be =y (preferred for vng) or =m. Built-in ksmbd shows up in
# neither /proc/modules nor /sys/module (no module parameters), so the
# kernel .config (cwd is the kernel tree under vng) is the tie-breaker;
# the mountd-start and smoke-mount checks below are the real arbiter.
modprobe ksmbd 2>/dev/null || true
if grep -q ksmbd /proc/modules || [ -d /sys/module/ksmbd ]; then
    log "ksmbd module loaded"
elif [ -f .config ] && grep -qE "^CONFIG_SMB_SERVER=[ym]" .config; then
    log "ksmbd built-in (CONFIG_SMB_SERVER=$(sed -n 's/^CONFIG_SMB_SERVER=//p' .config))"
elif [ -f .config ]; then
    fail "ksmbd not available in this kernel (need CONFIG_SMB_SERVER)"
else
    log "cannot verify ksmbd support (no .config); relying on mountd/mount checks"
fi

command -v ksmbd.mountd >/dev/null || fail "ksmbd.mountd not found (install ksmbd-tools on the host)"
[ -x "$XFSTESTS_DIR/check" ] || fail "xfstests check script not found at $XFSTESTS_DIR"

# xfstests secondary users. useradd cannot lock the virtiofs-shared
# /etc/passwd from inside the guest, so create them on the HOST (the CI
# does); missing users only make _require_user tests _notrun.
for u in fsgqa 123456-fsgqa; do
    if id -u "$u" >/dev/null 2>&1; then
        log "user $u present"
    elif useradd -m "$u" 2>/dev/null; then
        log "user $u created"
    else
        log "WARNING: user $u missing and cannot be created in-guest;" \
            "create it on the host (tests requiring it will not run)"
    fi
done

# --- exports and mountpoints ---
# Everything lives under EXPORT_BASE: with rootless virtiofs, guest root's
# writes carry the unprivileged HOST user's permissions, so root-owned
# paths like /mnt are not writable — /var/tmp always is.
mkdir -m 777 -p "$EXPORT_BASE/test" "$EXPORT_BASE/scratch" || fail "cannot create exports under $EXPORT_BASE"
mkdir -p "$EXPORT_BASE/mnt-test" "$EXPORT_BASE/mnt-scratch" || fail "cannot create mountpoints"

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
log "share config written ($EXPORT_BASE/smb.conf)"

ksmbd.adduser -P "$EXPORT_BASE/ksmbdpwd.db" -a "$SMB_USER" -p "$SMB_PASS" \
    || fail "ksmbd.adduser failed"
mkdir -p /etc/ksmbd
ksmbd.mountd -n -C "$EXPORT_BASE/smb.conf" -P "$EXPORT_BASE/ksmbdpwd.db" \
    > "$EXPORT_BASE/ksmbd.mountd.log" 2>&1 &
sleep 2
ps -e | grep -q mountd || fail "ksmbd.mountd did not start"
log "ksmbd.mountd running"
ss -ltn 2>/dev/null | grep -q ":445 " || log "WARNING: nothing listening on port 445 yet"

# smoke-test the mount before handing over to xfstests
CIFS_OPTS="username=$SMB_USER,password=$SMB_PASS,vers=3.1.1,mfsymlinks,actimeo=0"
mount -t cifs "//127.0.0.1/test" "$EXPORT_BASE/mnt-test" -o "$CIFS_OPTS" \
    || fail "cifs smoke mount of //127.0.0.1/test failed"
touch "$EXPORT_BASE/mnt-test/.kci-smoke" && rm -f "$EXPORT_BASE/mnt-test/.kci-smoke" \
    || fail "cifs smoke write failed"
umount "$EXPORT_BASE/mnt-test"
log "cifs smoke mount OK"

# --- xfstests configuration (see kci/xfstesting-cifs.rst) ---
cd "$XFSTESTS_DIR" || fail "cannot cd to $XFSTESTS_DIR"
cat > local.config <<EOF
export FSTYP=cifs
export TEST_DEV=//127.0.0.1/test
export TEST_DIR=$EXPORT_BASE/mnt-test
export SCRATCH_DEV=//127.0.0.1/scratch
export SCRATCH_MNT=$EXPORT_BASE/mnt-scratch
export CIFS_MOUNT_OPTIONS="-o $CIFS_OPTS"
export TEST_FS_MOUNT_OPTS="-o $CIFS_OPTS"
EOF
log "local.config written:"
cat local.config

echo "=== free space before xfstests ==="
df -h "$EXPORT_BASE" /

echo "=== XFSTESTS START ==="
# -d dumps each test's output to stdout (visible in CI logs), -T timestamps,
# -R xunit writes per-test results to results/result.xml; detailed per-test
# logs also land in $XFSTESTS_DIR/results/.
# check exits nonzero on test failures; results are parsed from the summary.
./check ${CHECK_OPTS:--d -T -R xunit} $TESTS
STATUS=$?
echo "=== XFSTESTS END ==="
log "check finished with status $STATUS"

echo "=== free space after xfstests ==="
df -h "$EXPORT_BASE" /

ksmbd.control -s 2>/dev/null || true
exit $STATUS

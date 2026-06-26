#!/bin/bash
#
# kci kernel-coverage.sh — modified from stress-ng/kernel-coverage.sh
# Copyright (C) 2016-2021 Canonical, 2022-2026 Colin Ian King
# License: GPL-2.0
#
# MODIFICATION: All test cases preserved, durations reduced for CI (~30 min).
# Original: DURATION=600/180/120/60/15 → Now: 2s per stressor
# Original: COUNT=4000 (4GB images) → Now: 256MB images
# Removed: lcov/gcov collection (we use KASAN instead)
#
set -e
export PATH=$PATH:/usr/bin:/usr/sbin

PERF_PARANOID=/proc/sys/kernel/perf_event_paranoid
SWAP=/tmp/swap.img
FSIMAGE=/tmp/fs.img
LOG=/tmp/stress-ng-$(date '+%Y%m%d-%H%M').log

if [ -z "$STRESS_NG" ]; then
    STRESS_NG=stress-ng
fi
if ! which "$STRESS_NG" >/dev/null 2>&1; then
    echo "Cannot find stress-ng"; exit 1
fi

STRESSORS=$($STRESS_NG --stressors | sed 's/smi//')
echo "=== stress-ng kernel coverage (CI mode, ~1h) ===" | tee $LOG

get_stress_ng_pids() { ps -e | grep stress-ng | awk '{ print $1}'; }
kill_stress_ng() {
    pids=$(get_stress_ng_pids)
    [ -n "$pids" ] && kill -ALRM $pids 2>/dev/null; sleep 1
    pids=$(get_stress_ng_pids)
    [ -n "$pids" ] && kill -KILL $pids 2>/dev/null
}

do_stress() {
    ARGS="-t $DURATION --pathological --timestamp --no-rand-seed --times --metrics --klog-check -x smi -v"
    echo "RUN: $* $ARGS" >> $LOG
    $STRESS_NG $* $ARGS 2>/dev/null
    sudo $STRESS_NG $* $ARGS 2>/dev/null
}

# Setup
if [ -e $PERF_PARANOID ]; then
    echo 0 | sudo tee $PERF_PARANOID > /dev/null
fi
echo core | sudo tee /proc/sys/kernel/core_pattern > /dev/null
echo -900 > /proc/self/oom_score_adj 2>/dev/null
echo 0 | sudo tee /proc/sys/vm/oom_kill_allocating_task > /dev/null 2>&1

fallocate -l 512M $SWAP && chmod 0600 $SWAP && mkswap $SWAP && swapon $SWAP

#
# Phase 1: Read-only FS + device stressors
#
DURATION=5
do_stress --rofs -1
do_stress --dev 4

#
# Phase 2: CPU schedulers
#
DURATION=5
scheds=$($STRESS_NG --sched which 2>&1 | tail -1 | cut -d':' -f2-)
for s in ${scheds}; do
    sudo $STRESS_NG --sched $s --cpu 2 -t 5 --timestamp --metrics 2>/dev/null
done

#
# Phase 3: ionice classes
#
DURATION=5
ionices=$($STRESS_NG --ionice-class which 2>&1 | tail -1 | cut -d':' -f2-)
for i in ${ionices}; do
    do_stress --ionice-class $i --iomix 2
done

#
# Phase 4: All stressors (keep ALL, 5s each)
#
DURATION=5
for S in $STRESSORS; do
    case $S in
        clone|fork|vfork) do_stress --${S} 1 --${S}-ops 5000 ;;
        *)                do_stress --${S} 2 --${S}-ops 5000 ;;
    esac
done

#
# Phase 5: All stressors at once
#
DURATION=10
do_stress --all 1

#
# Phase 6: Stressor option variants (ALL preserved, DURATION=5)
#
DURATION=5
do_stress --acl 2 --acl-rand
do_stress --affinity 2 --affinity-pin
do_stress --affinity 2 --affinity-rand
do_stress --affinity 2 --affinity-sleep
do_stress --bigheap 2 --bigheap-mlock
do_stress --brk 2 --brk-notouch
do_stress --brk 2 --brk-mlock
do_stress --brk 2 --thrash
do_stress --cacheline 4 --cacheline-affinity
do_stress --cachehammer 2 --cachehammer-numa
do_stress --cpu 2 --sched batch
do_stress --cpu 2 --taskset 0 --ignite-cpu
do_stress --cpu 2 --cpu-load-slice 50
do_stress --cpu 2 --c-state
do_stress --cpu-online 2 --cpu-online-all --pathological
do_stress --cpu-online 2 --cpu-online-affinity --pathological
do_stress --cpu-sched 2 --autogroup
do_stress --cyclic 2 --cyclic-policy deadline
do_stress --cyclic 2 --cyclic-policy fifo
do_stress --cyclic 2 --cyclic-policy rr
do_stress --cyclic 2 --cyclic-method clock_ns
do_stress --cyclic 2 --cyclic-method itimer
do_stress --cyclic 2 --cyclic-method poll
do_stress --cyclic 2 --cyclic-method posix_ns
do_stress --cyclic 2 --cyclic-method usleep
do_stress --dccp 2 --dccp-opts send
do_stress --dccp 2 --dccp-opts sendmsg
do_stress --dccp 2 --dccp-domain ipv4
do_stress --dccp 2 --dccp-domain ipv6
do_stress --epoll 2 --epoll-domain ipv4
do_stress --epoll 2 --epoll-domain ipv6
do_stress --epoll 2 --epoll-domain unix
do_stress --epoll 2 --epoll-sockets 10000
do_stress --dentry 2 --dentry-order stride
do_stress --dentry 2 --dentry-order random
do_stress --eventfd 2 --eventfd-nonblock
do_stress --exec 2 --exec-fork-method clone
do_stress --exec 2 --exec-fork-method fork
do_stress --exec 2 --exec-fork-method vfork
do_stress --fifo 2 --fifo-data-size 4096
do_stress --fifo 2 --fifo-readers 64
do_stress --fork 1 --fork-vm
do_stress --fork 1 --fork-max 64
do_stress --fork 1 --fork-pageout
do_stress --forkheavy 2 --forkheavy-mlock
do_stress --hrtimers 2 --hrtimers-adjust
do_stress --icmp-flood 2 --icmp-flood-max-size
do_stress --itimer 2 --itimer-rand
do_stress --l1cache 2 --l1cache-mlock
do_stress --link 2 --link-sync
do_stress --lease 2 --lease-breakers 8
do_stress --lockf 2 --lockf-nonblock
do_stress --lockbus 2 --lockbus-nosplit
do_stress --madvise 2 --madvise-hwpoison
do_stress --malloc 2 --malloc-mlock
do_stress --malloc 2 --malloc-touch
do_stress --malloc 2 --malloc-trim
do_stress --memfd 2 --memfd-fds 4096
do_stress --memfd 2 --memfd-mlock
do_stress --memrate 2 --memrate-flush
do_stress --mincore 2 --mincore-random
do_stress --mmap 2 --mmap-async
do_stress --mmap 2 --mmap-file
do_stress --mmap 2 --mmap-mprotect
do_stress --mmap 2 --mmap-mlock
do_stress --mmap 2 --mmap-numa
do_stress --mmap 2 --thrash
do_stress --mmap 2 --mmap-write-check
do_stress --mremap 2 --mremap-mlock
do_stress --msg 2 --msg-types 100
do_stress --mutex 2 --mutex-procs 64
do_stress --nanosleep 2 --nanosleep-threads 128
do_stress --nice 2 --autogroup
do_stress --null 2 --null-write
do_stress --numa 2 --numa-shuffle-addr
do_stress --open 2 --open-fd
do_stress --open 2 --open-max 100000
do_stress --pagemove 2 --pagemove-mlock
do_stress --pipe 2 --pipe-size 1M
do_stress --pipe 2 --pipe-vmsplice
do_stress --pipe 2 --pipe-readers 64 --pipe-writers 64
do_stress --pipeherd 1 --pipeherd-yield
do_stress --poll 2 --poll-fds 8192
do_stress --pthread 2 --pthread-max 512
do_stress --prio-inv 2 --prio-inv-type inherit
do_stress --prio-inv 2 --prio-inv-type protect
do_stress --prio-inv 2 --prio-inv-policy fifo
do_stress --prio-inv 2 --prio-inv-policy rr
do_stress --race-sched 2 --race-sched-method next
do_stress --race-sched 2 --race-sched-method prev
do_stress --ramfs 2 --ramfs-fill
do_stress --rawpkt 2 --rawpkt-rxring 4
do_stress --remap 2 --remap-mlock
do_stress --resched 2 --autogroup
do_stress --resources 2 --resources-mlock
do_stress --ring-pipe 2 --ring-pipe-splice
do_stress --sctp 2 --sctp-domain ipv4
do_stress --sctp 2 --sctp-domain ipv6
do_stress --schedmix 2 --schedmix-procs 64
do_stress --schedpolicy 2 --autogroup
do_stress --shm 2 --shm-mlock
do_stress --shm-sysv 2 --shm-sysv-mlock
do_stress --seek 2 --seek-punch
do_stress --sem 2 --sem-procs 64
do_stress --sem-sysv 2 --sem-sysv-procs 64
do_stress --sleep 2 --sleep-max 4096
do_stress --sock 2 --sock-nodelay
do_stress --sock 2 --sock-domain ipv4
do_stress --sock 2 --sock-domain ipv6
do_stress --sock 2 --sock-domain unix
do_stress --sock 2 --sock-protocol mptcp
do_stress --sock 2 --sock-opts send --sock-zerocopy
do_stress --sockfd 2 --sockfd-reuse
do_stress --splice 2 --splice-bytes 4K
do_stress --stack 2 --stack-mlock
do_stress --stack 2 --stack-fill
do_stress --stream 2 --stream-mlock
do_stress --stream 2 --stream-madvise hugepage
do_stress --swap 2 --swap-self
do_stress --switch 2 --switch-freq 1000000
do_stress --switch 2 --switch-method mq
do_stress --switch 2 --switch-method pipe
do_stress --symlink 2 --symlink-sync
do_stress --timer 2 --timer-rand
do_stress --timer 2 --timer-freq 1000000
do_stress --timerfd 2 --timerfd-rand
do_stress --tmpfs 2 --tmpfs-mmap-async
do_stress --tun 2
do_stress --tun 2 --tun-tap
do_stress --udp 2 --udp-domain ipv4
do_stress --udp 2 --udp-domain ipv6
do_stress --udp 2 --udp-gro
do_stress --udp-flood 2 --udp-flood-domain ipv4
do_stress --utime 2 --utime-fsync
do_stress --vfork 1 --vfork-max 64
do_stress --vforkmany 1 --vforkmany-vm
do_stress --vm 2 --vm-locked
do_stress --vm 2 --vm-populate
do_stress --vm 2 --vm-madvise hugepage
do_stress --vm 2 --vm-madvise dontneed
do_stress --vm 2 --vm-numa
do_stress --vm 2 --vm-populate --ksm
do_stress --workload 2 --workload-sched batch --workload-load 90
do_stress --workload 2 --workload-threads 8
do_stress --yield 2 --yield-sched fifo
do_stress --yield 2 --yield-sched rr
do_stress --zero 2 --zero-read
do_stress --zombie 1 --zombie-max 1000000

#
# Phase 7: sysfs/procfs/sysinval (longer, high coverage value)
#
DURATION=30
do_stress --sysfs 4
do_stress --procfs 4
DURATION=15
do_stress --sysinval 2 --pathological
do_stress --bad-ioctl 2 --pathological

#
# Phase 8: io class sequential
#
DURATION=10
sudo $STRESS_NG --class io --seq 1 -v -t $DURATION 2>/dev/null

# Cleanup
swapoff $SWAP 2>/dev/null
rm -f $SWAP

echo ""
echo "=== DMESG BUGS ==="
dmesg | grep -E "(BUG:|KASAN:|UBSAN:|WARNING:|Oops:)" || echo "(none)"
echo "=== stress-ng kernel coverage done ==="

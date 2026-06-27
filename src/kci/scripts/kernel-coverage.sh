#!/bin/bash
#
# kci kernel-coverage.sh — modified from stress-ng/kernel-coverage.sh
# Copyright (C) 2016-2021 Canonical, 2022-2026 Colin Ian King
# License: GPL-2.0
#
# MODIFICATION: All test cases preserved, filesystem tests removed (storage).
# Durations scaled to fit ~2h total CI runtime.
# Original: DURATION=600/180/120/60/15
# CI:       DURATION=30/15/10/60/45
# Removed: filesystem phase (4GB images), lcov/gcov
#
set -e
export PATH=$PATH:/usr/bin:/usr/sbin

PERF_PARANOID=/proc/sys/kernel/perf_event_paranoid
SWAP=/tmp/swap.img
LOG=/tmp/stress-ng-$(date '+%Y%m%d-%H%M').log

if [ -z "$STRESS_NG" ]; then
    STRESS_NG=stress-ng
fi
if ! which "$STRESS_NG" >/dev/null 2>&1; then
    echo "Cannot find stress-ng"; exit 1
fi

STRESSORS=$($STRESS_NG --stressors | sed 's/smi//')
echo "=== stress-ng kernel coverage (CI mode, ~2h) ===" | tee $LOG

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
# Prevent lockdep/warnings from causing kernel panic
echo 0 | sudo tee /proc/sys/kernel/panic_on_warn > /dev/null 2>&1

fallocate -l 512M $SWAP && chmod 0600 $SWAP && mkswap $SWAP && swapon $SWAP

#
# Phase 1: Read-only FS + device stressors
#
DURATION=30
do_stress --rofs -1
do_stress --dev 32

#
# Phase 2: CPU schedulers
#
DURATION=10
scheds=$($STRESS_NG --sched which 2>&1 | tail -1 | cut -d':' -f2-)
for s in ${scheds}; do
    sudo $STRESS_NG --sched $s --cpu -1 -t $DURATION --timestamp --metrics 2>/dev/null
    sudo $STRESS_NG --sched $s --cpu -1 -t $DURATION --sched-reclaim --timestamp --metrics 2>/dev/null
done

#
# Phase 3: ionice classes
#
DURATION=15
ionices=$($STRESS_NG --ionice-class which 2>&1 | tail -1 | cut -d':' -f2-)
for i in ${ionices}; do
    do_stress --ionice-class $i --iomix -1
done

#
# Phase 4: All stressors individually (10s each)
#
DURATION=10
for S in $STRESSORS; do
    case $S in
        clone|fork|vfork) do_stress --${S} 1 --${S}-ops 10000 ;;
        *)                do_stress --${S} 4 --${S}-ops 10000 ;;
    esac
done

#
# Phase 5: All stressors at once
#
DURATION=30
do_stress --all 1

#
# Phase 6: Stressor option variants (10s each)
#
DURATION=10
do_stress --acl -1 --acl-rand
do_stress --affinity -1 --affinity-pin
do_stress --affinity -1 --affinity-rand
do_stress --affinity -1 --affinity-sleep
do_stress --bigheap -1 --bigheap-mlock
do_stress --brk -1 --brk-notouch
do_stress --brk -1 --brk-mlock
do_stress --brk -1 --thrash
do_stress --cacheline 32 --cacheline-affinity
do_stress --cachehammer -1 --cachehammer-numa
do_stress --cpu -1 --sched batch
do_stress --cpu -1 --taskset 0,2 --ignite-cpu
do_stress --cpu -1 --cpu-load-slice 50
do_stress --cpu -1 --c-state
do_stress --cpu-online -1 --cpu-online-all --pathological
do_stress --cpu-online -1 --cpu-online-affinity --pathological
do_stress --cpu-sched -1 --autogroup
do_stress --cyclic -1 --cyclic-policy deadline
do_stress --cyclic -1 --cyclic-policy fifo
do_stress --cyclic -1 --cyclic-policy rr
do_stress --cyclic -1 --cyclic-method clock_ns
do_stress --cyclic -1 --cyclic-method itimer
do_stress --cyclic -1 --cyclic-method poll
do_stress --cyclic -1 --cyclic-method posix_ns
do_stress --cyclic -1 --cyclic-method usleep
do_stress --cyclic -1 --cyclic-prio 50
do_stress --dccp -1 --dccp-opts send
do_stress --dccp -1 --dccp-opts sendmsg
do_stress --dccp -1 --dccp-opts sendmmsg
do_stress --dccp -1 --dccp-domain ipv4
do_stress --dccp -1 --dccp-domain ipv6
do_stress --epoll -1 --epoll-domain ipv4
do_stress --epoll -1 --epoll-domain ipv6
do_stress --epoll -1 --epoll-domain unix
do_stress --epoll -1 --epoll-sockets 10000
do_stress --dentry -1 --dentry-order stride
do_stress --dentry -1 --dentry-order random
do_stress --eventfd -1 --eventfd-nonblock
do_stress --exec -1 --exec-no-pthread
do_stress --exec -1 --exec-fork-method clone
do_stress --exec -1 --exec-fork-method fork
do_stress --exec -1 --exec-fork-method spawn
do_stress --exec -1 --exec-fork-method vfork
do_stress --far-branch -1 --far-branch-flush
do_stress --fifo -1 --fifo-data-size 4096
do_stress --fifo -1 --fifo-readers 64
do_stress --fork -1 --fork-vm
do_stress --fork -1 --fork-max 64
do_stress --fork -1 --fork-pageout
do_stress --forkheavy -1 --forkheavy-mlock
do_stress --get -1 --get-slow-sync
do_stress --goto -1 --goto-direction forward
do_stress --goto -1 --goto-direction backward
do_stress --hrtimers -1 --hrtimers-adjust
do_stress --icmp-flood -1 --icmp-flood-max-size
do_stress --itimer -1 --itimer-rand
do_stress --itimer -1 --itimer-freq 1000
do_stress --l1cache -1 --l1cache-mlock
do_stress --link -1 --link-sync
do_stress --lease -1 --lease-breakers 8
do_stress --llc-affinity -1 --llc-affinity-clflush
do_stress --llc-affinity -1 --llc-affinity-mlock
do_stress --lockf -1 --lockf-nonblock
do_stress --lockbus -1 --lockbus-nosplit
do_stress --madvise -1 --madvise-hwpoison
do_stress --malloc -1 --malloc-mlock
do_stress --malloc -1 --malloc-pthreads 4
do_stress --malloc -1 --malloc-touch
do_stress --malloc -1 --malloc-zerofree
do_stress --malloc -1 --malloc-trim
do_stress --memfd -1 --memfd-fds 4096
do_stress --memfd -1 --memfd-mlock
do_stress --memfd -1 --memfd-zap-pte
do_stress --memrate -1 --memrate-flush
do_stress --mincore -1 --mincore-random
do_stress --mmap -1 --mmap-async
do_stress --mmap -1 --mmap-file
do_stress --mmap -1 --mmap-madvise
do_stress --mmap -1 --mmap-mergeable
do_stress --mmap -1 --mmap-mlock
do_stress --mmap -1 --mmap-mprotect
do_stress --mmap -1 --mmap-odirect
do_stress --mmap -1 --mmap-osync
do_stress --mmap -1 --mmap-write-check
do_stress --mmap -1 --mmap-stressful
do_stress --mmap -1 --mmap-slow-munmap
do_stress --mmap -1 --mmap-numa
do_stress --mmap -1 --thrash
do_stress --mmapaddr -1 --mmapaddr-mlock
do_stress --mmapcow -1 --mmapcow-fork
do_stress --mmapcow -1 --mmapcow-mlock
do_stress --mmapfixed -1 --mmapfixed-mlock
do_stress --mmaphuge -1 --mmaphuge-mlock
do_stress --mmaphuge -1 --mmaphuge-mmaps 32768
do_stress --mmapmany -1 --mmapmany-mlock
do_stress --mmaprandom -1 --mmaprandom-mappings 512
do_stress --mmaptorture -1 --mmaptorture-bytes 30%
do_stress --mremap -1 --mremap-mlock
do_stress --mremap -1 --mremap-numa
do_stress --msg -1 --msg-types 100
do_stress --msg -1 --msg-bytes 8192
do_stress --mutex -1 --mutex-procs 64
do_stress --nanosleep -1 --nanosleep-threads 128
do_stress --nanosleep -1 --nanosleep-method cstate
do_stress --nanosleep -1 --nanosleep-method random
do_stress --nice -1 --autogroup
do_stress --null -1 --null-write
do_stress --numa -1 --numa-shuffle-addr
do_stress --numa -1 --numa-shuffle-node
do_stress --open -1 --open-fd
do_stress --open -1 --open-max 100000
do_stress --pagemove -1 --pagemove-mlock
do_stress --pipe -1 --pipe-size 64K
do_stress --pipe -1 --pipe-size 1M
do_stress --pipe -1 --pipe-vmsplice
do_stress --pipe -1 --pipe-readers 64 --pipe-writers 64
do_stress --pipeherd 1 --pipeherd-yield
do_stress --poll -1 --poll-fds 8192
do_stress --prefetch -1 --prefetch-l3-size 16M
do_stress --pthread -1 --pthread-max 512
do_stress --pthread -1 --pthread-max 1024
do_stress --prio-inv -1 --prio-inv-type inherit
do_stress --prio-inv -1 --prio-inv-type protect
do_stress --prio-inv -1 --prio-inv-policy fifo
do_stress --prio-inv -1 --prio-inv-policy rr
do_stress --race-sched -1 --race-sched-method next
do_stress --race-sched -1 --race-sched-method prev
do_stress --race-sched -1 --race-sched-method randinc
do_stress --race-sched -1 --race-sched-method syncnext
do_stress --ramfs -1 --ramfs-fill
do_stress --ramfs -1 --ramfs-size 16M
do_stress --rawpkt -1 --rawpkt-rxring 2
do_stress --rawpkt -1 --rawpkt-rxring 16
do_stress --remap -1 --remap-mlock
do_stress --remap -1 --remap-pages 64
do_stress --resched -1 --autogroup
do_stress --resources -1 --resources-mlock
do_stress --revio -1 --revio-write-size 17
do_stress --ring-pipe -1 --ring-pipe-splice
do_stress --ring-pipe -1 --ring-pipe-num 1024
do_stress --sctp -1 --sctp-domain ipv4
do_stress --sctp -1 --sctp-domain ipv6
do_stress --sctp -1 --sctp-domain ipv4 --sctp-sched fcfs
do_stress --sctp -1 --sctp-domain ipv4 --sctp-sched prio
do_stress --sctp -1 --sctp-domain ipv4 --sctp-sched rr
do_stress --schedmix -1 --schedmix-procs 64
do_stress --schedmix -1 --autogroup
do_stress --schedpolicy -1 --autogroup
do_stress --shm -1 --shm-objs 100000
do_stress --shm -1 --shm-mlock
do_stress --shm-sysv -1 --shm-sysv-segs 128
do_stress --shm-sysv -1 --shm-sysv-mlock
do_stress --seek -1 --seek-punch
do_stress --sem -1 --sem-procs 64
do_stress --sem -1 --sem-shared
do_stress --sem-sysv -1 --sem-sysv-procs 64
do_stress --sleep -1 --sleep-max 4096
do_stress --sock -1 --sock-nodelay
do_stress --sock -1 --sock-domain ipv4
do_stress --sock -1 --sock-domain ipv6
do_stress --sock -1 --sock-domain unix
do_stress --sock -1 --sock-type stream
do_stress --sock -1 --sock-type seqpacket
do_stress --sock -1 --sock-protocol mptcp
do_stress --sock -1 --sock-opts random
do_stress --sock -1 --sock-opts send --sock-zerocopy
do_stress --sockfd -1 --sockfd-reuse
do_stress --spinmem -1 --spinmem-affinity
do_stress --spinmem -1 --spinmem-numa
do_stress --splice -1 --splice-bytes 4K
do_stress --stack -1 --stack-mlock
do_stress --stack -1 --stack-fill
do_stress --stack -1 --stack-pageout
do_stress --stream -1 --stream-mlock
do_stress --stream -1 --stream-madvise hugepage
do_stress --stream -1 --stream-madvise nohugepage
do_stress --stream -1 --stream-prefetch
do_stress --swap -1 --swap-self
do_stress --switch -1 --switch-freq 1000000
do_stress --switch -1 --switch-method mq
do_stress --switch -1 --switch-method pipe
do_stress --switch -1 --switch-method sem-sysv
do_stress --symlink -1 --symlink-sync
do_stress --syncload -1 --syncload-msbusy 200 --syncload-mssleep 100
do_stress --timer -1 --timer-rand
do_stress --timer -1 --timer-freq 1000000
do_stress --timer -1 --timer-freq 100000 --timer-slack 1000
do_stress --timerfd -1 --timerfd-rand
do_stress --timerfd -1 --timerfd-freq 100000
do_stress --tsc -1 --tsc-lfence
do_stress --tsc -1 --tsc-rdtscp
do_stress --tmpfs -1 --tmpfs-mmap-async
do_stress --tmpfs -1 --tmpfs-mmap-file
do_stress --tun -1
do_stress --tun -1 --tun-tap
do_stress --udp -1 --udp-domain ipv4
do_stress --udp -1 --udp-domain ipv6
do_stress --udp -1 --udp-lite --udp-domain ipv4
do_stress --udp -1 --udp-lite --udp-domain ipv6
do_stress --udp -1 --udp-gro
do_stress --udp-flood -1 --udp-flood-domain ipv4
do_stress --udp-flood -1 --udp-flood-domain ipv6
do_stress --utime -1 --utime-fsync
do_stress --vfork 1 --vfork-max 64
do_stress --vforkmany 1 --vforkmany-vm
do_stress --vm -1 --vm-flush
do_stress --vm -1 --vm-keep
do_stress --vm -1 --vm-hang 1
do_stress --vm -1 --vm-locked
do_stress --vm -1 --vm-populate
do_stress --vm -1 --vm-madvise dontneed
do_stress --vm -1 --vm-madvise hugepage
do_stress --vm -1 --vm-madvise mergeable
do_stress --vm -1 --vm-madvise nohugepage
do_stress --vm -1 --vm-madvise normal
do_stress --vm -1 --vm-madvise random
do_stress --vm -1 --vm-madvise sequential
do_stress --vm -1 --vm-madvise unmergeable
do_stress --vm -1 --vm-madvise willneed --page-in
do_stress --vm -1 --vm-numa
do_stress --vm -1 --vm-populate --ksm
do_stress --vm-addr -1 --vm-addr-mlock
do_stress --workload -1 --workload-sched batch --workload-load 90
do_stress --workload -1 --workload-sched deadline --workload-load 90
do_stress --workload -1 --workload-sched idle --workload-load 90
do_stress --workload -1 --workload-sched other --workload-load 90
do_stress --workload -1 --workload-threads 8
do_stress --workload -1 --workload-dist cluster
do_stress --workload -1 --workload-dist even
do_stress --workload -1 --workload-dist poisson
do_stress --yield -1 --yield-sched deadline
do_stress --yield -1 --yield-sched idle
do_stress --yield -1 --yield-sched fifo
do_stress --yield -1 --yield-sched other
do_stress --yield -1 --yield-sched rr
do_stress --zero -1 --zero-read
do_stress --zombie 1 --zombie-max 1000000

#
# Phase 7: sysfs/procfs traversal (high coverage value)
#
DURATION=60
do_stress --sysfs 16
do_stress --procfs 32

#
# Phase 8: sysinval/bad-ioctl (pathological, high bug-finding value)
#
DURATION=45
do_stress --sysinval 8 --pathological
DURATION=30
do_stress --bad-ioctl -1 --pathological
do_stress --sysbadaddr 8

#
# Phase 9: I/O class sequential
#
DURATION=30
sudo $STRESS_NG --class io --seq -1 -v -t $DURATION 2>/dev/null

# Cleanup
swapoff $SWAP 2>/dev/null
rm -f $SWAP

echo ""
echo "=== DMESG BUGS ==="
dmesg | grep -E "(BUG:|KASAN:|UBSAN:|WARNING:|Oops:)" || echo "(none)"
echo "=== stress-ng kernel coverage done ==="

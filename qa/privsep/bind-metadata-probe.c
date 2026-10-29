/* Disposable post-filter hostile body; not copied into the manager image.
 * Same bootstrap and .wizard_policy bytes as production. Link-time replacement,
 * never runtime selection. The external rig bounds it with the fixed watchdog.
 */
#define WIZARD_METADATA_TEST_BODY
#include "../../packages/backend/native/bind-metadata.c"

static unsigned failures;
static void number(long n) {
    char text[32]; unsigned at = sizeof(text); unsigned long value;
    int neg = n < 0; value = neg ? (unsigned long)(-n) : (unsigned long)n;
    do { text[--at] = '0' + value%10; value /= 10; } while (value);
    if (neg) text[--at] = '-';
    SC(write,1,text+at,sizeof(text)-at,0,0,0);
}
static void row(long nr, long result) {
    number(nr); SC(write,1," ",1,0,0,0); number(result); SC(write,1,"\n",1,0,0,0);
    if (result != -1) ++failures; /* EPERM, not ENOSYS or loading failure */
}
#define PROBE(n) row(__NR_##n,call6(__NR_##n,0,0,0,0,0,0))

void metadata_test_body(long argc, char **argv) {
    if (argc != 3) finish(43);
    /* Selected target on an independently proven writable submount. */
    const char *target = argv[2];
    SC(write,1,"post-filter\n",12,0,0,0);
    struct statx st;
    if (argv[1][0] == 'u') {
        long fd = SC(openat,-100,target,1|01000,0,0,0);
        if (fd < 0 || SC(write,fd,"control\n",8,0,0,0) != 8) finish(50);
        finish(0);
    }
    if (argv[1][0] == 'm') {
        row(__NR_openat,SC(openat,-100,target,1,0,0,0));
        finish(failures ? 46 : 0);
    }
#if defined(__x86_64__)
    if (argv[1][0] == 'x') {
        call6(__NR_write|0x40000000U,1,(long)"bypass",6,0,0,0);
        finish(47);
    }
    if (argv[1][0] == 'i') {
        __asm__ volatile("int $0x80" : : "a"(4), "b"(1), "c"(0), "d"(0) : "memory");
        finish(48);
    }
#endif
    if (argv[1][0] == 'c') {
        *(volatile unsigned long *)0 = 1; /* core-dump negative, after bootstrap */
        finish(49);
    }
    if (metadata(target,&st)) finish(44);
    SC(write,1,"M ",2,0,0,0); number(st.stx_dev_major); SC(write,1," ",1,0,0,0);
    number(st.stx_dev_minor); SC(write,1," ",1,0,0,0); number(st.stx_ino); SC(write,1,"\n",1,0,0,0);
    /* Barrier is file metadata only. No read/open/fd privilege exception. The
     * rig binds a synthetic shared directory and adds target AFTER this marker. */
    if (argv[1][0] == 'b') {
        SC(write,1,"barrier\n",8,0,0,0);
        while (metadata("/host/opt/metadata-security/gate/release",&st) == -2) {}
        if (metadata(target,&st)) finish(45);
        SC(write,1,"M ",2,0,0,0); number(st.stx_dev_major); SC(write,1," ",1,0,0,0);
        number(st.stx_dev_minor); SC(write,1," ",1,0,0,0); number(st.stx_ino); SC(write,1,"\n",1,0,0,0);
    }
    const unsigned long flags[] = {0,1,2,0100,01000,02000,0101,01002,0100|01000,020000000|0200000|2};
    for (unsigned i = 0; i < sizeof(flags)/sizeof(flags[0]); ++i)
        row(__NR_openat,SC(openat,-100,target,flags[i],0600,0,0));
    /* Complete pointer-structure calls still reach EPERM before kernel parsing. */
    const unsigned long how[3] = {1|01000,0600,0};
    row(__NR_openat2,SC(openat2,-100,target,how,sizeof(how),0,0));
    row(__NR_write,SC(write,9,"x",1,0,0,0));
    row(__NR_write,SC(write,0x100000001ULL,"x",1,0,0,0));
    row(__NR_statx,SC(statx,0xffffff9cU,target,0x100,STATX_BASIC_STATS,&st,0));
    row(__NR_statx,SC(statx,-100,target,0x100000100ULL,STATX_BASIC_STATS,&st,0));
    row(__NR_statx,SC(statx,-100,target,0,STATX_BASIC_STATS,&st,0));
    row(__NR_statx,SC(statx,-100,target,0x100,0x1000007ffULL,&st,0));
    PROBE(writev); PROBE(pwrite64); PROBE(pwritev); PROBE(pwritev2);
    row(__NR_truncate,SC(truncate,target,0,0,0,0,0));
    PROBE(ftruncate); PROBE(fallocate); PROBE(copy_file_range); PROBE(sendfile);
    PROBE(splice); PROBE(tee); PROBE(vmsplice);
    PROBE(mkdirat); PROBE(unlinkat); PROBE(renameat); PROBE(renameat2);
    PROBE(linkat); PROBE(symlinkat); PROBE(mknodat);
    PROBE(fchmod); PROBE(fchmodat); PROBE(fchown); PROBE(fchownat);
    PROBE(utimensat); PROBE(setxattr); PROBE(lsetxattr); PROBE(fsetxattr);
    PROBE(removexattr); PROBE(lremovexattr); PROBE(fremovexattr);
    row(__NR_mmap,SC(mmap,0,4096,3,1,9,0));
    row(__NR_mmap,SC(mmap,0,4096,3,3,9,0));
    row(__NR_mmap,SC(mmap,0,4096,3,2,9,0));
    PROBE(mprotect); PROBE(mremap); PROBE(msync); PROBE(remap_file_pages);
    PROBE(recvmsg); PROBE(recvmmsg); PROBE(pidfd_getfd); PROBE(open_by_handle_at);
    PROBE(dup); PROBE(dup3); PROBE(close); PROBE(close_range); PROBE(ioctl); PROBE(fcntl);
    PROBE(io_uring_setup); PROBE(io_uring_enter); PROBE(io_uring_register);
    PROBE(io_setup); PROBE(io_submit); PROBE(io_getevents);
    PROBE(socket); PROBE(socketpair); PROBE(connect); PROBE(sendmsg);
    PROBE(ptrace); PROBE(process_vm_readv); PROBE(process_vm_writev);
    PROBE(bpf); PROBE(userfaultfd); PROBE(perf_event_open);
    PROBE(mount); PROBE(umount2); PROBE(mount_setattr); PROBE(move_mount);
    PROBE(open_tree); PROBE(fsopen); PROBE(fsconfig); PROBE(fsmount);
    PROBE(setns); PROBE(unshare); PROBE(clone); PROBE(clone3); PROBE(execve); PROBE(execveat);
    PROBE(prctl); PROBE(seccomp); PROBE(prlimit64); PROBE(setrlimit);
    PROBE(timer_create); PROBE(timer_settime); PROBE(rt_sigaction); PROBE(rt_sigprocmask);
    row(__NR_kill,SC(kill,1,9,0,0,0,0)); /* trusted shell/container PID1 */
    PROBE(tgkill); PROBE(pidfd_send_signal);
#if defined(__x86_64__)
    PROBE(open); PROBE(creat); PROBE(mkdir); PROBE(unlink); PROBE(rmdir);
    PROBE(rename); PROBE(link); PROBE(symlink); PROBE(mknod); PROBE(chmod);
    PROBE(chown); PROBE(lchown); PROBE(utime); PROBE(utimes); PROBE(dup2);
    PROBE(fork); PROBE(vfork); PROBE(pkey_mprotect);
#endif
    /* Newer native numbers stay denied even when kernel headers predate names. */
    row(452,call6(452,-100,(long)target,0600,0,0,0)); /* fchmodat2 on both supported ABIs */
    SC(write,1,failures ? "FAIL\n" : "denied\n",failures ? 5 : 7,0,0,0);
    finish(failures ? 46 : 0);
}

/* Trusted, self-restricting bootstrap. No libc, loader, constructors or config.
 * Build only for the native Linux LP64 ABIs below. The post-filter body may do
 * statx(NOFOLLOW), write to immutable output pipes, and exit; nothing else.
 * Bootstrap/watchdog/image are trusted, NOT a malicious pre-filter entry image.
 */
#include <linux/audit.h>
#include <linux/capability.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <linux/stat.h>
#include <asm/unistd.h>
#include <stddef.h>
#include <stdint.h>

#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error Unsupported metadata-helper byte order
#endif
#if defined(__x86_64__) && !defined(__ILP32__)
#define NATIVE_ARCH AUDIT_ARCH_X86_64
static inline long call6(long n, long a, long b, long c, long d, long e, long f) {
    register long r10 __asm__("r10") = d;
    register long r8 __asm__("r8") = e;
    register long r9 __asm__("r9") = f;
    long r;
    __asm__ volatile("syscall" : "=a"(r) : "a"(n), "D"(a), "S"(b), "d"(c), "r"(r10), "r"(r8), "r"(r9) : "rcx", "r11", "memory");
    return r;
}
__asm__(".global _start\n_start:\nmov %rsp,%rdi\nand $-16,%rsp\ncall helper_main\nud2\n");
#elif defined(__aarch64__) && !defined(__ILP32__)
#define NATIVE_ARCH AUDIT_ARCH_AARCH64
static inline long call6(long n, long a, long b, long c, long d, long e, long f) {
    register long x8 __asm__("x8") = n;
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d;
    register long x4 __asm__("x4") = e;
    register long x5 __asm__("x5") = f;
    __asm__ volatile("svc 0" : "+r"(x0) : "r"(x1), "r"(x2), "r"(x3), "r"(x4), "r"(x5), "r"(x8) : "memory");
    return x0;
}
__asm__(".global _start\n_start:\nmov x0,sp\nbl helper_main\nbrk #0\n");
#else
#error Unsupported metadata-helper ABI
#endif
_Static_assert(sizeof(long) == 8 && sizeof(void *) == 8, "LP64 required");
#define SC(n,a,b,c,d,e,f) call6(__NR_##n,(long)(a),(long)(b),(long)(c),(long)(d),(long)(e),(long)(f))
#define DENY (SECCOMP_RET_ERRNO | 1U)
#define LD(field) BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, field))
/* Compare BOTH words, not just kernel-truncated argument values. */
#define EQARG(i,lo,hi) LD(args[i]), BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,(lo),1,0), BPF_STMT(BPF_RET|BPF_K,DENY), \
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[i])+4), \
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,(hi),1,0), BPF_STMT(BPF_RET|BPF_K,DENY)
/* This exact section is extracted and hashed for production/probe comparison. */
static const struct sock_filter policy[] __attribute__((used,section(".wizard_policy"))) = {
    LD(arch), BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,NATIVE_ARCH,1,0), BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS),
    LD(nr),
#if defined(__x86_64__)
    BPF_JUMP(BPF_JMP|BPF_JSET|BPF_K,0x40000000U,0,1), BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS),
#endif
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,__NR_statx,0,19),
    EQARG(0,0xffffff9cU,0xffffffffU), /* signed AT_FDCWD */
    EQARG(2,0x100U,0),               /* AT_SYMLINK_NOFOLLOW only */
    EQARG(3,STATX_BASIC_STATS,0),
    BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW),
    LD(nr), BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,__NR_write,0,8),
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[0])+4),
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,0,1,0), BPF_STMT(BPF_RET|BPF_K,DENY),
    LD(args[0]), BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,1,2,0), BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,2,1,0),
    BPF_STMT(BPF_RET|BPF_K,DENY), BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW),
    LD(nr), BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,__NR_exit,0,1), BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW),
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,__NR_exit_group,0,1), BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW),
    BPF_STMT(BPF_RET|BPF_K,DENY)
};

__attribute__((noreturn)) static void finish(long code) {
    SC(exit_group,code,0,0,0,0,0);
    for (;;) {} /* An outer policy denying exit never permits continued parsing. */
}
static void refuse(void) { finish(42); }
static long metadata(const char *p, struct statx *st) {
    return SC(statx,-100,p,0x100,STATX_BASIC_STATS,st,0);
}
static void output(const char *p, unsigned long n) {
    while (n) {
        long r = SC(write,1,p,n,0,0,0);
        if (r == -4) continue;
        if (r <= 0 || (unsigned long)r > n) refuse();
        p += r; n -= (unsigned long)r;
    }
    finish(0);
}
static void bootstrap(void) {
    /* No argv bytes or /host accesses in this function. Native ELF startup has
     * no inherited mappings except its private executable/stack/vDSO segments. */
    if (SC(prctl,39,0,0,0,0,0) != 1) refuse(); /* PR_GET_NO_NEW_PRIVS */
    struct __user_cap_header_struct hdr = { _LINUX_CAPABILITY_VERSION_3, 0 };
    struct __user_cap_data_struct caps[2] = {{0,0,0},{0,0,0}};
    if (SC(capget,&hdr,caps,0,0,0,0)) refuse();
    for (unsigned i = 0; i < 2; ++i)
        if (caps[i].effective || caps[i].permitted || caps[i].inheritable) refuse();
    unsigned cap;
    for (cap = 0; cap < 256; ++cap) {
        long r = SC(prctl,23,cap,0,0,0,0); /* bounding set, unknown -> EINVAL */
        if (r == -22) break;
        if (r != 0) refuse();
    }
    if (!cap || cap == 256) refuse();
    for (long fd = 1; fd <= 2; ++fd) {
        struct statx st;
        if (SC(statx,fd,"",0x1000,STATX_TYPE,&st,0) || !(st.stx_mask & STATX_TYPE) || (st.stx_mode & 0170000) != 0010000) refuse();
        long flags = SC(fcntl,fd,3,0,0,0,0); /* F_GETFL; pipe write end only */
        if (flags < 0 || (flags & 3) != 1 || (flags & 010000000)) refuse(); /* O_PATH */
    }
    if (SC(close_range,0,0,0,0,0,0) || SC(close_range,3,0xffffffffU,0,0,0,0)) refuse();
    const unsigned long core[2] = {0,0};
    if (SC(prlimit64,0,4,core,0,0,0) || SC(prctl,4,0,0,0,0,0)) refuse(); /* RLIMIT_CORE; dumpable */
    struct sock_fprog program = { (unsigned short)(sizeof(policy)/sizeof(policy[0])), (struct sock_filter *)policy };
    if (SC(seccomp,SECCOMP_SET_MODE_FILTER,0,&program,0,0,0)) refuse();
}
static int parent_ok(const struct statx *st, uint32_t uid) {
    return (st->stx_mode & 0170000) == 0040000 && (st->stx_uid == 0 || st->stx_uid == uid) && !(st->stx_mode & 022);
}
static int stat_ok(const struct statx *st) {
    return (st->stx_mask & (STATX_TYPE|STATX_MODE|STATX_UID)) == (STATX_TYPE|STATX_MODE|STATX_UID);
}
static void __attribute__((unused)) body(long argc, char **argv) {
    if (argc != 3) refuse();
    uint32_t uid = 0;
    unsigned i;
    for (i = 0; i < 10 && argv[1][i]; ++i) {
        unsigned digit = (unsigned char)argv[1][i] - '0';
        if (digit > 9 || uid > (UINT32_MAX-digit)/10) refuse();
        uid = uid * 10 + digit;
    }
    if (!i || argv[1][i] || uid == UINT32_MAX) refuse();
    /* Max host pathname is 4095 bytes; prefix must also fit kernel PATH_MAX. */
    const char *source = argv[2];
    unsigned len = 0;
    while (len < 4090 && source[len]) {
        unsigned char c = (unsigned char)source[len];
        if (c < 32 || c == 127) refuse();
        ++len;
    }
    if (!len || len == 4090 || source[0] != '/' || (len > 1 && source[len-1] == '/')) refuse();
    /* Validate the WHOLE path before the first metadata call, including suffix
     * components beyond a missing ancestor. No early missing for malformed input. */
    for (unsigned at = 1; at < len;) {
        unsigned start = at;
        while (at < len && source[at] != '/') ++at;
        unsigned n = at-start;
        if (!n || n > 255 || (n == 1 && source[start] == '.') || (n == 2 && source[start] == '.' && source[start+1] == '.')) refuse();
        ++at;
    }
    char current[4096];
    current[0]='/'; current[1]='h'; current[2]='o'; current[3]='s'; current[4]='t'; current[5]=0;
    unsigned used = 5;
    struct statx st;
    if (metadata(current,&st) || !stat_ok(&st) || !parent_ok(&st,uid)) refuse();
    if (len == 1) output("safe\n",5);
    unsigned pos = 1;
    while (pos < len) {
        unsigned start = pos;
        while (pos < len && source[pos] != '/') ++pos;
        unsigned n = pos-start;
        if (!n || n > 255 || (n == 1 && source[start] == '.') || (n == 2 && source[start] == '.' && source[start+1] == '.')) refuse();
        current[used++] = '/';
        for (unsigned j = start; j < pos; ++j) current[used++] = source[j];
        current[used] = 0;
        long result = metadata(current,&st);
        if (result == -2) output("missing\n",8); /* ONLY true ENOENT */
        if (result || !stat_ok(&st) || (st.stx_mode & 0170000) == 0120000) refuse();
        if (pos < len) { if (!parent_ok(&st,uid)) refuse(); ++pos; }
        else if ((st.stx_mode & 0170000) != 0040000 && (st.stx_mode & 0170000) != 0100000) refuse();
    }
    output("safe\n",5);
}
#ifdef WIZARD_METADATA_TEST_BODY
/* Link-time test replacement only. Never built into the shipped runner. */
extern void metadata_test_body(long argc, char **argv);
#endif
__attribute__((noreturn,used)) void helper_main(long *stack) {
    bootstrap(); /* MUST precede even selected argument parsing */
#ifdef WIZARD_METADATA_TEST_BODY
    metadata_test_body(stack[0],(char **)(stack+1));
#else
    body(stack[0],(char **)(stack+1));
#endif
    finish(42);
}

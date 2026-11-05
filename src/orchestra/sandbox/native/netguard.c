/* netguard: LD_PRELOAD shim for sandboxed agent commands (sandbox spec A, 2026-10-09).
 *
 * connect / sendto / sendmsg to anything but AF_UNIX or a loopback address fail
 * with EACCES before a packet leaves. Covers every dynamically linked program
 * (bash, python, git, curl, pip); statically linked ones are held by the
 * Landlock port rules instead. Build: gcc -shared -fPIC -O2 -o libnetguard.so netguard.c -ldl
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

static int allowed(const struct sockaddr *sa, socklen_t len) {
    if (sa == NULL) return 1;                       /* connected socket: destination already checked */
    if (sa->sa_family == AF_UNIX || sa->sa_family == AF_UNSPEC) return 1;
    if (sa->sa_family == AF_INET && len >= (socklen_t)sizeof(struct sockaddr_in)) {
        const struct sockaddr_in *in = (const struct sockaddr_in *)sa;
        return (ntohl(in->sin_addr.s_addr) >> 24) == 127;
    }
    if (sa->sa_family == AF_INET6 && len >= (socklen_t)sizeof(struct sockaddr_in6)) {
        const struct sockaddr_in6 *in6 = (const struct sockaddr_in6 *)sa;
        if (IN6_IS_ADDR_LOOPBACK(&in6->sin6_addr)) return 1;
        if (IN6_IS_ADDR_V4MAPPED(&in6->sin6_addr)) return in6->sin6_addr.s6_addr[12] == 127;
        return 0;
    }
    return 0;                                        /* netlink, packet, unknown: refused */
}

static void note(const char *what) {
    char buf[160];
    int n = snprintf(buf, sizeof buf, "adamas-netguard: %s to a non-loopback address refused\n", what);
    if (n > 0) { ssize_t r = write(2, buf, (size_t)n); (void)r; }
}

int connect(int fd, const struct sockaddr *sa, socklen_t len) {
    static int (*real)(int, const struct sockaddr *, socklen_t);
    if (!real) real = dlsym(RTLD_NEXT, "connect");
    if (!allowed(sa, len)) { note("connect"); errno = EACCES; return -1; }
    return real(fd, sa, len);
}

ssize_t sendto(int fd, const void *buf, size_t n, int flags, const struct sockaddr *sa, socklen_t len) {
    static ssize_t (*real)(int, const void *, size_t, int, const struct sockaddr *, socklen_t);
    if (!real) real = dlsym(RTLD_NEXT, "sendto");
    if (sa && !allowed(sa, len)) { note("sendto"); errno = EACCES; return -1; }
    return real(fd, buf, n, flags, sa, len);
}

ssize_t sendmsg(int fd, const struct msghdr *msg, int flags) {
    static ssize_t (*real)(int, const struct msghdr *, int);
    if (!real) real = dlsym(RTLD_NEXT, "sendmsg");
    if (msg && msg->msg_name && !allowed((const struct sockaddr *)msg->msg_name, msg->msg_namelen)) {
        note("sendmsg"); errno = EACCES; return -1;
    }
    return real(fd, msg, flags);
}

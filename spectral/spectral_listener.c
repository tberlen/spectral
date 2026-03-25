/*
 * spectral_listener.c - C-You Spectral Listener for UniFi APs
 *
 * Captures spectral/FFT data from qca_spectral kernel module on UniFi APs.
 * Streams data via UDP to collector and serves a health/status HTTP API.
 *
 * Validated on:
 *   - U6-IW  (ARMv7, qca_ol, Linux 5.4.164)
 *   - U7 Pro Max (ARMv7, qca_ol, Linux 5.4.213)
 *
 * Netlink protocol 17, signature 0xdeadbeef, same struct on both models.
 *
 * Usage:
 *   ./spectral_listener stream <IFACE> [PROTO] <HOST> <PORT> [HTTP_PORT]
 *   ./spectral_listener probe
 *   ./spectral_listener scan <IFACE> [PROTO]
 *   ./spectral_listener scan-json <IFACE> [PROTO]
 *
 * Cross-compile: arm-linux-gnueabi-gcc -O2 -static -o spectral_listener spectral_listener.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <time.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/time.h>
#include <linux/netlink.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stddef.h>
#include <pthread.h>

/* ---------- Constants ---------- */

#define SPECTRAL_NL_PROTO    17
#define SPECTRAL_NL_MIN      13
#define SPECTRAL_NL_MAX      31
#define SPECTRAL_SIGNATURE   0xdeadbeef
#define RECV_BUF_SIZE        65536
#define MAX_FFT_BINS         1024
#define HTTP_PORT_DEFAULT     8080
#define HTTP_BUF_SIZE        4096

/* Payload offsets */
#define OFF_SIGNATURE   0x00
#define OFF_FREQ        0x08
#define OFF_CFREQ1      0x0C
#define OFF_CFREQ2      0x14
#define OFF_INT_TYPE    0x1C
#define OFF_FREQ_LOWER  0x20
#define OFF_FREQ_UPPER  0x24
#define OFF_TSF         0x28
#define OFF_TSF_LAST    0x30
#define OFF_TSF_CUR     0x38
#define OFF_NOISE_FLOOR 0x50
#define OFF_RSSI        0x51
#define OFF_MAX_SCALE   0x52
#define OFF_MAX_MAG     0x53
#define OFF_CHAIN_COUNT 0x90
#define OFF_MACADDR     0x184
#define OFF_BIN_START   0x190
#define EXPECTED_PAYLOAD_SIZE 3012
#define BIN_REGION_SIZE (EXPECTED_PAYLOAD_SIZE - OFF_BIN_START)

/* ---------- Globals ---------- */

static volatile int running = 1;

/* Health/status state */
static volatile unsigned long g_samples_sent = 0;
static volatile unsigned long g_errors = 0;
static time_t g_start_time = 0;
static char g_server_ip[64] = "";
static int g_server_port = 0;
static char g_iface[32] = "";
static int g_http_port = HTTP_PORT_DEFAULT;
static char g_hostname[64] = "";
static char g_my_ip[64] = "";

static void discover_identity(void)
{
    /* Hostname */
    gethostname(g_hostname, sizeof(g_hostname) - 1);

    /* Get own IP by connecting a UDP socket to the server */
    if (g_server_ip[0]) {
        int s = socket(AF_INET, SOCK_DGRAM, 0);
        if (s >= 0) {
            struct sockaddr_in dst;
            memset(&dst, 0, sizeof(dst));
            dst.sin_family = AF_INET;
            dst.sin_port = htons(1);
            inet_pton(AF_INET, g_server_ip, &dst.sin_addr);
            if (connect(s, (struct sockaddr *)&dst, sizeof(dst)) == 0) {
                struct sockaddr_in local;
                socklen_t len = sizeof(local);
                if (getsockname(s, (struct sockaddr *)&local, &len) == 0) {
                    inet_ntop(AF_INET, &local.sin_addr, g_my_ip, sizeof(g_my_ip));
                }
            }
            close(s);
        }
    }
}

static void signal_handler(int sig)
{
    (void)sig;
    running = 0;
}

/* ---------- Helpers ---------- */

static uint32_t rd32(const uint8_t *p)
{
    return p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24);
}

static uint64_t rd64(const uint8_t *p)
{
    return (uint64_t)rd32(p) | ((uint64_t)rd32(p + 4) << 32);
}

static void hex_dump(const uint8_t *data, int len, int offset)
{
    for (int i = 0; i < len; i += 16) {
        printf("  %04x: ", offset + i);
        for (int j = 0; j < 16 && (i + j) < len; j++)
            printf("%02x ", data[i + j]);
        for (int j = len - i; j < 16; j++)
            printf("   ");
        printf(" |");
        for (int j = 0; j < 16 && (i + j) < len; j++) {
            uint8_t c = data[i + j];
            printf("%c", (c >= 32 && c < 127) ? c : '.');
        }
        printf("|\n");
    }
}

static void print_timestamp(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    struct tm *tm = localtime(&tv.tv_sec);
    printf("%02d:%02d:%02d.%03ld",
           tm->tm_hour, tm->tm_min, tm->tm_sec, tv.tv_usec / 1000);
}

/* ---------- Netlink socket ---------- */

static int nl_open(int protocol)
{
    int fd = socket(AF_NETLINK, SOCK_RAW, protocol);
    if (fd < 0)
        return -1;

    int bufsize = 1024 * 1024;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &bufsize, sizeof(bufsize));

    struct sockaddr_nl addr;
    memset(&addr, 0, sizeof(addr));
    addr.nl_family = AF_NETLINK;
    addr.nl_pid = getpid();
    addr.nl_groups = 0xFFFFFFFF;

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        return -1;
    }

    return fd;
}

static int nl_recv(int fd, uint8_t *buf, int bufsize, int timeout_ms)
{
    fd_set fds;
    struct timeval tv;

    FD_ZERO(&fds);
    FD_SET(fd, &fds);
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;

    int ret = select(fd + 1, &fds, NULL, NULL, &tv);
    if (ret <= 0)
        return ret;

    struct sockaddr_nl from;
    socklen_t fromlen = sizeof(from);
    return recvfrom(fd, buf, bufsize, 0,
                    (struct sockaddr *)&from, &fromlen);
}

/* ---------- Spectral message parser ---------- */

static int parse_spectral(const uint8_t *payload, int len, int json_output)
{
    if (len < OFF_BIN_START)
        return 0;

    uint32_t sig = rd32(payload + OFF_SIGNATURE);
    if (sig != SPECTRAL_SIGNATURE)
        return 0;

    uint32_t freq       = rd32(payload + OFF_FREQ);
    uint32_t cfreq1     = rd32(payload + OFF_CFREQ1);
    uint32_t cfreq2     = rd32(payload + OFF_CFREQ2);
    uint32_t int_type   = rd32(payload + OFF_INT_TYPE);
    uint32_t freq_lower = rd32(payload + OFF_FREQ_LOWER);
    uint32_t freq_upper = rd32(payload + OFF_FREQ_UPPER);
    uint64_t tsf        = rd64(payload + OFF_TSF);
    int8_t   noise      = (int8_t)payload[OFF_NOISE_FLOOR];
    int8_t   rssi       = (int8_t)payload[OFF_RSSI];
    uint8_t  max_scale  = payload[OFF_MAX_SCALE];
    uint8_t  max_mag    = payload[OFF_MAX_MAG];

    const uint8_t *mac = payload + OFF_MACADDR;

    int bin_len = len - OFF_BIN_START;
    if (bin_len > MAX_FFT_BINS) bin_len = MAX_FFT_BINS;
    int nonzero_bins = 0;
    int max_bin_val = 0;
    int max_bin_idx = 0;
    for (int i = 0; i < bin_len; i++) {
        uint8_t v = payload[OFF_BIN_START + i];
        if (v > 0) {
            nonzero_bins++;
            if (v > max_bin_val) {
                max_bin_val = v;
                max_bin_idx = i;
            }
        }
    }

    if (json_output) {
        printf("{\"freq\":%u", freq);
        printf(",\"cfreq1\":%u", cfreq1);
        printf(",\"cfreq2\":%u", cfreq2);
        printf(",\"freq_lower\":%u", freq_lower);
        printf(",\"freq_upper\":%u", freq_upper);
        printf(",\"noise_floor\":%d", noise);
        printf(",\"rssi\":%d", rssi);
        printf(",\"max_scale\":%u", max_scale);
        printf(",\"max_mag\":%u", max_mag);
        printf(",\"int_type\":%u", int_type);
        printf(",\"tsf\":%llu", (unsigned long long)tsf);
        printf(",\"mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\"",
               mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        printf(",\"nonzero_bins\":%d", nonzero_bins);
        printf(",\"max_bin_val\":%d", max_bin_val);
        printf(",\"max_bin_idx\":%d", max_bin_idx);

        if (nonzero_bins > 0) {
            printf(",\"bins\":[");
            int first = 1;
            for (int i = 0; i < bin_len; i++) {
                uint8_t v = payload[OFF_BIN_START + i];
                if (v > 0) {
                    if (!first) printf(",");
                    printf("[%d,%u]", i, v);
                    first = 0;
                }
            }
            printf("]");
        }
        printf("}\n");
    } else {
        print_timestamp();
        printf("  freq=%u (%u-%u) noise=%d rssi=%d mag=%u scale=%u "
               "tsf=%llu bins=%d/%d max_bin=[%d]=%d "
               "mac=%02x:%02x:%02x:%02x:%02x:%02x\n",
               freq, freq_lower, freq_upper,
               noise, rssi, max_mag, max_scale,
               (unsigned long long)tsf,
               nonzero_bins, bin_len, max_bin_idx, max_bin_val,
               mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    }

    fflush(stdout);
    return 1;
}

/* ---------- Process received netlink message ---------- */

static int process_msg(const uint8_t *buf, int len, int json_output, int raw_dump)
{
    if (raw_dump) {
        print_timestamp();
        printf("  len=%d\n", len);
        hex_dump(buf, len, 0);
        printf("\n");
        fflush(stdout);
        return 1;
    }

    if (len >= (int)sizeof(struct nlmsghdr)) {
        const struct nlmsghdr *nlh = (const struct nlmsghdr *)buf;
        if (nlh->nlmsg_len >= sizeof(struct nlmsghdr)) {
            const uint8_t *payload = buf + NLMSG_HDRLEN;
            int payload_len = len - NLMSG_HDRLEN;
            if (parse_spectral(payload, payload_len, json_output))
                return 1;
        }
    }

    if (parse_spectral(buf, len, json_output))
        return 1;

    print_timestamp();
    printf("  unknown msg len=%d\n", len);
    hex_dump(buf, len > 128 ? 128 : len, 0);
    printf("\n");
    fflush(stdout);
    return 0;
}

/* ---------- HTTP Health API (runs in separate thread) ---------- */

static void *http_thread(void *arg)
{
    (void)arg;

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) {
        fprintf(stderr, "HTTP socket failed: %s\n", strerror(errno));
        return NULL;
    }

    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(g_http_port);

    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "HTTP bind failed on port %d: %s\n", g_http_port, strerror(errno));
        close(srv);
        return NULL;
    }

    listen(srv, 5);
    fprintf(stderr, "Health API listening on port %d\n", g_http_port);

    while (running) {
        fd_set fds;
        struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };
        FD_ZERO(&fds);
        FD_SET(srv, &fds);

        if (select(srv + 1, &fds, NULL, NULL, &tv) <= 0)
            continue;

        struct sockaddr_in client;
        socklen_t client_len = sizeof(client);
        int fd = accept(srv, (struct sockaddr *)&client, &client_len);
        if (fd < 0) continue;

        /* Read request (minimal HTTP parsing) */
        char req[1024];
        int n = read(fd, req, sizeof(req) - 1);
        if (n <= 0) { close(fd); continue; }
        req[n] = '\0';

        char response[HTTP_BUF_SIZE];
        char body[HTTP_BUF_SIZE / 2];
        const char *content_type = "application/json";

        if (strstr(req, "GET /health") == req + 4 ||
            strstr(req, "GET /health ") != NULL) {
            /* Simple health check */
            snprintf(body, sizeof(body), "{\"status\":\"ok\"}");
        }
        else if (strstr(req, "GET /status") == req + 4 ||
                 strstr(req, "GET /status ") != NULL) {
            /* Detailed status */
            time_t uptime = time(NULL) - g_start_time;
            snprintf(body, sizeof(body),
                "{\"status\":\"ok\","
                "\"hostname\":\"%s\","
                "\"ap_ip\":\"%s\","
                "\"server_ip\":\"%s\","
                "\"server_port\":%d,"
                "\"interface\":\"%s\","
                "\"http_port\":%d,"
                "\"samples_sent\":%lu,"
                "\"errors\":%lu,"
                "\"uptime_seconds\":%ld}",
                g_hostname, g_my_ip,
                g_server_ip, g_server_port, g_iface,
                g_http_port,
                g_samples_sent, g_errors,
                (long)uptime);
        }
        else {
            /* 404 */
            snprintf(body, sizeof(body), "{\"error\":\"not found\"}");
        }

        int body_len = strlen(body);
        int resp_len = snprintf(response, sizeof(response),
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "\r\n%s",
            content_type, body_len, body);

        write(fd, response, resp_len);
        close(fd);
    }

    close(srv);
    return NULL;
}

static void start_http_server(void)
{
    pthread_t tid;
    pthread_create(&tid, NULL, http_thread, NULL);
    pthread_detach(tid);
}

/* ---------- Probe mode ---------- */

static void probe_proc_netlink(void)
{
    FILE *f = fopen("/proc/net/netlink", "r");
    if (!f) {
        fprintf(stderr, "Cannot open /proc/net/netlink: %s\n", strerror(errno));
        return;
    }

    printf("=== Active netlink sockets (/proc/net/netlink) ===\n");
    char line[256];
    int first = 1;
    while (fgets(line, sizeof(line), f)) {
        if (first) { printf("  %s", line); first = 0; continue; }
        unsigned long sk;
        unsigned int eth, pid, groups;
        if (sscanf(line, "%lx %u %u %08x", &sk, &eth, &pid, &groups) == 4) {
            printf("  proto=%2u  pid=%5u  groups=0x%08x", eth, pid, groups);
            switch (eth) {
                case 0:  printf("  (ROUTE)"); break;
                case 2:  printf("  (USERSOCK)"); break;
                case 4:  printf("  (SOCK_DIAG)"); break;
                case 9:  printf("  (AUDIT)"); break;
                case 10: printf("  (FIB_LOOKUP)"); break;
                case 12: printf("  (NETFILTER)"); break;
                case 15: printf("  (KOBJECT_UEVENT)"); break;
                case 16: printf("  (GENERIC)"); break;
                case 17: printf("  (QCA_SPECTRAL)"); break;
            }
            printf("\n");
        }
    }
    fclose(f);
}

static void probe_scan(void)
{
    printf("\n=== Probing netlink protocols %d-%d ===\n",
           SPECTRAL_NL_MIN, SPECTRAL_NL_MAX);

    int fds[32];
    int protos[32];
    int nfds = 0;

    for (int proto = SPECTRAL_NL_MIN; proto <= SPECTRAL_NL_MAX; proto++) {
        int fd = nl_open(proto);
        if (fd >= 0) {
            printf("  proto %2d: bound OK\n", proto);
            protos[nfds] = proto;
            fds[nfds++] = fd;
        } else {
            printf("  proto %2d: bind failed (%s)\n", proto, strerror(errno));
        }
    }

    if (nfds == 0) {
        printf("\nNo protocols could be bound. Try running as root.\n");
        return;
    }

    printf("\nListening on %d protocols for 10 seconds...\n", nfds);
    printf("Triggering spectraltool scan on wifi0...\n\n");

    system("spectraltool -i wifi0 startscan 2>/dev/null");

    uint8_t buf[RECV_BUF_SIZE];
    time_t start = time(NULL);

    while (time(NULL) - start < 10 && running) {
        fd_set fds_set;
        struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };
        int maxfd = 0;

        FD_ZERO(&fds_set);
        for (int i = 0; i < nfds; i++) {
            FD_SET(fds[i], &fds_set);
            if (fds[i] > maxfd) maxfd = fds[i];
        }

        int ret = select(maxfd + 1, &fds_set, NULL, NULL, &tv);
        if (ret <= 0) continue;

        for (int i = 0; i < nfds; i++) {
            if (!FD_ISSET(fds[i], &fds_set)) continue;
            int n = recv(fds[i], buf, sizeof(buf), MSG_DONTWAIT);
            if (n > 0) {
                printf("*** DATA on protocol %d (%d bytes) ***\n", protos[i], n);
                process_msg(buf, n, 0, 0);
                printf("\n");
            }
        }
    }

    system("spectraltool -i wifi0 stopscan 2>/dev/null");

    for (int i = 0; i < nfds; i++)
        close(fds[i]);
}

static int cmd_probe(void)
{
    probe_proc_netlink();
    probe_scan();
    return 0;
}

/* ---------- Listen / dump mode ---------- */

static int cmd_listen(int protocol, int raw_dump, int json_output)
{
    fprintf(stderr, "Listening on netlink protocol %d%s...\n",
            protocol, raw_dump ? " (raw dump)" : json_output ? " (JSON)" : "");

    int fd = nl_open(protocol);
    if (fd < 0) {
        fprintf(stderr, "Failed to bind protocol %d: %s\n",
                protocol, strerror(errno));
        return 1;
    }

    uint8_t buf[RECV_BUF_SIZE];
    unsigned long count = 0;

    while (running) {
        int n = nl_recv(fd, buf, sizeof(buf), 1000);
        if (n < 0) {
            if (errno == EINTR) continue;
            fprintf(stderr, "recv error: %s\n", strerror(errno));
            break;
        }
        if (n == 0) continue;
        count++;
        process_msg(buf, n, json_output, raw_dump);
    }

    fprintf(stderr, "\nReceived %lu messages total\n", count);
    close(fd);
    return 0;
}

/* ---------- Scan + listen mode ---------- */

static int cmd_scan(const char *iface, int protocol, int json_output)
{
    fprintf(stderr, "Scanning on %s, protocol %d\n", iface, protocol);

    int fd = nl_open(protocol);
    if (fd < 0) {
        fprintf(stderr, "Failed to bind protocol %d: %s\n",
                protocol, strerror(errno));
        return 1;
    }

    char cmd[256];
    snprintf(cmd, sizeof(cmd), "spectraltool -i %s scan_count 10 2>/dev/null", iface);
    system(cmd);

    char start_cmd[256];
    snprintf(start_cmd, sizeof(start_cmd), "spectraltool -i %s startscan 2>/dev/null", iface);
    system(start_cmd);

    uint8_t buf[RECV_BUF_SIZE];
    unsigned long count = 0;
    time_t last_trigger = time(NULL);

    while (running) {
        time_t now = time(NULL);
        if (now - last_trigger >= 1) {
            system(start_cmd);
            last_trigger = now;
        }
        int n = nl_recv(fd, buf, sizeof(buf), 200);
        if (n <= 0) continue;
        count++;
        process_msg(buf, n, json_output, 0);
    }

    snprintf(cmd, sizeof(cmd), "spectraltool -i %s stopscan 2>/dev/null", iface);
    system(cmd);

    fprintf(stderr, "\nReceived %lu messages total\n", count);
    close(fd);
    return 0;
}

/* ---------- Stream mode - scan and forward to collector ---------- */

static int cmd_stream(const char *iface, int protocol,
                      const char *host, int port)
{
    fprintf(stderr, "Streaming spectral from %s to %s:%d (proto %d, http %d)\n",
            iface, host, port, protocol, g_http_port);

    /* Store config for health API */
    g_start_time = time(NULL);
    strncpy(g_server_ip, host, sizeof(g_server_ip) - 1);
    g_server_port = port;
    strncpy(g_iface, iface, sizeof(g_iface) - 1);

    /* Discover own IP and hostname */
    discover_identity();
    fprintf(stderr, "Identity: hostname=%s ip=%s\n", g_hostname, g_my_ip);

    /* Start HTTP health server */
    start_http_server();

    /* Open UDP socket to collector */
    int udp_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (udp_fd < 0) {
        fprintf(stderr, "UDP socket failed: %s\n", strerror(errno));
        return 1;
    }

    struct sockaddr_in dest;
    memset(&dest, 0, sizeof(dest));
    dest.sin_family = AF_INET;
    dest.sin_port = htons(port);
    if (inet_pton(AF_INET, host, &dest.sin_addr) != 1) {
        fprintf(stderr, "Invalid host: %s\n", host);
        close(udp_fd);
        return 1;
    }

    /* Open netlink */
    int nl_fd = nl_open(protocol);
    if (nl_fd < 0) {
        fprintf(stderr, "Failed to bind protocol %d: %s\n",
                protocol, strerror(errno));
        close(udp_fd);
        return 1;
    }

    /* Fork child for periodic scan triggering */
    pid_t scan_pid = fork();
    if (scan_pid == 0) {
        close(nl_fd);
        close(udp_fd);
        char cmd[256];
        snprintf(cmd, sizeof(cmd), "spectraltool -i %s scan_count 10 2>/dev/null", iface);
        system(cmd);
        while (1) {
            snprintf(cmd, sizeof(cmd), "spectraltool -i %s startscan 2>/dev/null", iface);
            system(cmd);
            usleep(1500000);
        }
        _exit(0);
    }

    uint8_t buf[RECV_BUF_SIZE];
    char json[4096];

    while (running) {
        int n = nl_recv(nl_fd, buf, sizeof(buf), 1000);
        if (n <= 0) continue;

        /* Extract payload */
        const uint8_t *payload = buf;
        int payload_len = n;
        if (n >= (int)sizeof(struct nlmsghdr)) {
            const struct nlmsghdr *nlh = (const struct nlmsghdr *)buf;
            if (nlh->nlmsg_len >= sizeof(struct nlmsghdr)) {
                payload = buf + NLMSG_HDRLEN;
                payload_len = n - NLMSG_HDRLEN;
            }
        }

        if (payload_len < OFF_BIN_START) continue;
        uint32_t sig = rd32(payload + OFF_SIGNATURE);
        if (sig != SPECTRAL_SIGNATURE) continue;

        g_samples_sent++;

        /* Build compact JSON with AP identity */
        uint32_t freq       = rd32(payload + OFF_FREQ);
        int8_t   noise      = (int8_t)payload[OFF_NOISE_FLOOR];
        int8_t   rssi_val   = (int8_t)payload[OFF_RSSI];
        uint64_t tsf        = rd64(payload + OFF_TSF);
        const uint8_t *mac  = payload + OFF_MACADDR;

        int bin_len = payload_len - OFF_BIN_START;
        if (bin_len > MAX_FFT_BINS) bin_len = MAX_FFT_BINS;

        /* Find max bin */
        int max_bv = 0, max_bi = 0, nz = 0;
        for (int i = 0; i < bin_len; i++) {
            uint8_t v = payload[OFF_BIN_START + i];
            if (v > 0) {
                nz++;
                if (v > max_bv) { max_bv = v; max_bi = i; }
            }
        }

        int pos = snprintf(json, sizeof(json),
            "{\"h\":\"%s\",\"ip\":\"%s\","
            "\"mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\","
            "\"f\":%u,\"n\":%d,\"r\":%d,\"t\":%llu,"
            "\"nz\":%d,\"mv\":%d,\"mi\":%d,\"b\":[",
            g_hostname, g_my_ip,
            mac[0], mac[1], mac[2], mac[3], mac[4], mac[5],
            freq, noise, rssi_val, (unsigned long long)tsf,
            nz, max_bv, max_bi);

        int first = 1;
        for (int i = 0; i < bin_len && pos < (int)sizeof(json) - 32; i++) {
            uint8_t v = payload[OFF_BIN_START + i];
            if (v > 0) {
                if (!first) json[pos++] = ',';
                pos += snprintf(json + pos, sizeof(json) - pos,
                               "[%d,%u]", i, v);
                first = 0;
            }
        }
        pos += snprintf(json + pos, sizeof(json) - pos, "]}\n");

        if (sendto(udp_fd, json, pos, 0,
               (struct sockaddr *)&dest, sizeof(dest)) < 0) {
            g_errors++;
        }

        /* Periodic status */
        if (g_samples_sent % 100 == 0) {
            fprintf(stderr, "Sent %lu samples (freq=%u noise=%d)\n",
                    g_samples_sent, freq, noise);
        }
    }

    /* Cleanup */
    if (scan_pid > 0)
        kill(scan_pid, SIGTERM);

    char stop_cmd[256];
    snprintf(stop_cmd, sizeof(stop_cmd), "spectraltool -i %s stopscan 2>/dev/null", iface);
    system(stop_cmd);

    fprintf(stderr, "\nStreamed %lu messages to %s:%d\n", g_samples_sent, host, port);
    close(nl_fd);
    close(udp_fd);
    return 0;
}

/* ---------- Main ---------- */

static void usage(const char *prog)
{
    fprintf(stderr,
        "Usage: %s <command> [options]\n"
        "\n"
        "Commands:\n"
        "  probe                                          Discover spectral netlink protocol\n"
        "  listen [PROTO]                                 Listen and parse (default: 17)\n"
        "  json [PROTO]                                   Listen, output JSON (default: 17)\n"
        "  dump [PROTO]                                   Raw hex dump (default: 17)\n"
        "  scan <IFACE> [PROTO]                           Trigger scan + parse\n"
        "  scan-json <IFACE> [PROTO]                      Trigger scan + JSON output\n"
        "  stream <IFACE> [PROTO] <HOST> <PORT> [HTTP]    Scan + forward via UDP + health API\n"
        "\n"
        "Examples:\n"
        "  %s probe\n"
        "  %s scan wifi0\n"
        "  %s stream wifi0 17 REDACTED_BUILD_SERVER 8766\n"
        "  %s stream wifi0 17 REDACTED_BUILD_SERVER 8766 8080\n"
        "\n"
        "Health API (in stream mode):\n"
        "  GET /health  - returns {\"status\":\"ok\"}\n"
        "  GET /status  - returns detailed status with server_ip, uptime, samples_sent\n"
        "\n", prog, prog, prog, prog, prog);
}

int main(int argc, char *argv[])
{
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    if (argc < 2) {
        usage(argv[0]);
        return 1;
    }

    const char *cmd = argv[1];

    if (strcmp(cmd, "probe") == 0)
        return cmd_probe();

    if (strcmp(cmd, "listen") == 0)
        return cmd_listen(argc >= 3 ? atoi(argv[2]) : SPECTRAL_NL_PROTO, 0, 0);

    if (strcmp(cmd, "json") == 0)
        return cmd_listen(argc >= 3 ? atoi(argv[2]) : SPECTRAL_NL_PROTO, 0, 1);

    if (strcmp(cmd, "dump") == 0)
        return cmd_listen(argc >= 3 ? atoi(argv[2]) : SPECTRAL_NL_PROTO, 1, 0);

    if (strcmp(cmd, "scan") == 0) {
        if (argc < 3) { fprintf(stderr, "scan needs interface\n"); return 1; }
        return cmd_scan(argv[2], argc >= 4 ? atoi(argv[3]) : SPECTRAL_NL_PROTO, 0);
    }

    if (strcmp(cmd, "scan-json") == 0) {
        if (argc < 3) { fprintf(stderr, "scan-json needs interface\n"); return 1; }
        return cmd_scan(argv[2], argc >= 4 ? atoi(argv[3]) : SPECTRAL_NL_PROTO, 1);
    }

    if (strcmp(cmd, "stream") == 0) {
        if (argc < 5) {
            fprintf(stderr, "stream needs: <IFACE> [PROTO] <HOST> <PORT> [HTTP_PORT]\n");
            return 1;
        }
        const char *iface = argv[2];
        int proto, arg_offset;
        if (argc >= 6) {
            proto = atoi(argv[3]);
            arg_offset = 4;
        } else {
            proto = SPECTRAL_NL_PROTO;
            arg_offset = 3;
        }

        /* Optional HTTP port (last arg) */
        int remaining = argc - arg_offset;
        if (remaining >= 3) {
            g_http_port = atoi(argv[arg_offset + 2]);
        }

        return cmd_stream(iface, proto, argv[arg_offset], atoi(argv[arg_offset + 1]));
    }

    fprintf(stderr, "Unknown command: %s\n", cmd);
    usage(argv[0]);
    return 1;
}

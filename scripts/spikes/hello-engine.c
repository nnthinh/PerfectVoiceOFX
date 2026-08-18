/* Spike hello-engine — §3.8 serve contract, no Demucs/PyTorch.
 *
 * spawn(absPath, ["serve", "--bind", "127.0.0.1", "--port", "0",
 *                 "--token-file", tokenPath], {cwd: engineDir})
 */
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <unistd.h>

#define TOKEN_MAX 256
#define REQ_MAX 8192

static void die(const char *msg) {
    fprintf(stderr, "hello-engine: %s\n", msg);
    exit(1);
}

static void usage(void) {
    fprintf(stderr,
            "usage: hello-engine serve --bind 127.0.0.1 --port 0 --token-file PATH\n");
    exit(2);
}

static char *read_token_and_unlink(const char *path) {
    FILE *fp = fopen(path, "r");
    if (!fp) {
        fprintf(stderr, "hello-engine: cannot read token file: %s\n", strerror(errno));
        exit(1);
    }
    char buf[TOKEN_MAX];
    if (!fgets(buf, sizeof(buf), fp)) {
        fclose(fp);
        die("token file empty");
    }
    fclose(fp);
    if (unlink(path) != 0 && errno != ENOENT) {
        fprintf(stderr, "hello-engine: unlink token: %s\n", strerror(errno));
        exit(1);
    }
    size_t n = strlen(buf);
    while (n > 0 && (buf[n - 1] == '\n' || buf[n - 1] == '\r' || buf[n - 1] == ' ')) {
        buf[--n] = '\0';
    }
    if (n == 0) {
        die("token file empty");
    }
    char *token = malloc(n + 1);
    if (!token) {
        die("oom");
    }
    memcpy(token, buf, n + 1);
    return token;
}

static int header_is(const char *line, const char *name) {
    size_t n = strlen(name);
    if (strncasecmp(line, name, n) != 0) {
        return 0;
    }
    return line[n] == ':';
}

static const char *header_value(const char *line) {
    const char *p = strchr(line, ':');
    if (!p) {
        return "";
    }
    p++;
    while (*p == ' ' || *p == '\t') {
        p++;
    }
    return p;
}

static void trim_crlf(char *s) {
    size_t n = strlen(s);
    while (n > 0 && (s[n - 1] == '\n' || s[n - 1] == '\r')) {
        s[--n] = '\0';
    }
}

static void send_all(int fd, const char *s) {
    size_t n = strlen(s);
    while (n > 0) {
        ssize_t w = write(fd, s, n);
        if (w < 0) {
            if (errno == EINTR) {
                continue;
            }
            return;
        }
        s += (size_t)w;
        n -= (size_t)w;
    }
}

static void respond(int fd, int code, const char *reason, const char *body) {
    char hdr[256];
    int len = (int)strlen(body);
    snprintf(hdr, sizeof(hdr),
             "HTTP/1.1 %d %s\r\n"
             "Content-Type: application/json\r\n"
             "Content-Length: %d\r\n"
             "Connection: close\r\n"
             "\r\n",
             code, reason, len);
    send_all(fd, hdr);
    send_all(fd, body);
}

static void handle_client(int cfd, const char *token) {
    char req[REQ_MAX];
    size_t used = 0;
    while (used < sizeof(req) - 1) {
        ssize_t r = read(cfd, req + used, sizeof(req) - 1 - used);
        if (r < 0) {
            if (errno == EINTR) {
                continue;
            }
            return;
        }
        if (r == 0) {
            break;
        }
        used += (size_t)r;
        req[used] = '\0';
        if (strstr(req, "\r\n\r\n") || strstr(req, "\n\n")) {
            break;
        }
    }
    req[used] = '\0';

    char *save = NULL;
    char *line = strtok_r(req, "\r\n", &save);
    if (!line) {
        respond(cfd, 400, "Bad Request", "{\"ok\":false,\"error\":\"bad_request\"}");
        return;
    }

    char method[16] = {0};
    char path[256] = {0};
    if (sscanf(line, "%15s %255s", method, path) != 2) {
        respond(cfd, 400, "Bad Request", "{\"ok\":false,\"error\":\"bad_request\"}");
        return;
    }

    int authorized = 0;
    char expect[TOKEN_MAX + 16];
    snprintf(expect, sizeof(expect), "Bearer %s", token);

    while ((line = strtok_r(NULL, "\r\n", &save)) != NULL) {
        trim_crlf(line);
        if (line[0] == '\0') {
            break;
        }
        if (header_is(line, "Authorization")) {
            if (strcmp(header_value(line), expect) == 0) {
                authorized = 1;
            }
        }
    }

    if (!authorized) {
        respond(cfd, 401, "Unauthorized", "{\"ok\":false,\"error\":\"unauthorized\"}");
        return;
    }
    if (strcmp(method, "GET") != 0) {
        respond(cfd, 405, "Method Not Allowed", "{\"ok\":false,\"error\":\"method\"}");
        return;
    }
    if (strcmp(path, "/v1/health") != 0) {
        respond(cfd, 404, "Not Found", "{\"ok\":false,\"error\":\"not_found\"}");
        return;
    }
    respond(cfd, 200, "OK", "{\"ok\": true, \"protocol_version\": 1}");
}

int main(int argc, char **argv) {
    const char *bind_addr = NULL;
    const char *token_path = NULL;
    int port = -1;

    if (argc < 2 || strcmp(argv[1], "serve") != 0) {
        usage();
    }
    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--bind") == 0 && i + 1 < argc) {
            bind_addr = argv[++i];
        } else if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
            port = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--token-file") == 0 && i + 1 < argc) {
            token_path = argv[++i];
        } else if (strcmp(argv[i], "--token-fd") == 0) {
            die("--token-fd is not supported (use --token-file)");
        } else {
            usage();
        }
    }
    if (!bind_addr || !token_path || port < 0) {
        usage();
    }
    if (strcmp(bind_addr, "127.0.0.1") != 0) {
        die("bind must be 127.0.0.1");
    }

    char *token = read_token_and_unlink(token_path);

    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        die("socket");
    }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, bind_addr, &addr.sin_addr) != 1) {
        die("inet_pton");
    }
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        fprintf(stderr, "hello-engine: bind: %s\n", strerror(errno));
        exit(1);
    }
    if (listen(fd, 16) != 0) {
        die("listen");
    }

    socklen_t alen = sizeof(addr);
    if (getsockname(fd, (struct sockaddr *)&addr, &alen) != 0) {
        die("getsockname");
    }
    unsigned ready_port = (unsigned)ntohs(addr.sin_port);
    printf("READY http://127.0.0.1:%u\n", ready_port);
    fflush(stdout);

    for (;;) {
        int cfd = accept(fd, NULL, NULL);
        if (cfd < 0) {
            if (errno == EINTR) {
                continue;
            }
            fprintf(stderr, "hello-engine: accept: %s\n", strerror(errno));
            break;
        }
        handle_client(cfd, token);
        close(cfd);
    }

    free(token);
    close(fd);
    return 1;
}

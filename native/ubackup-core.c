/*
 * ubackup-core: low-level capture scanner and content-addressed object store.
 *
 * This is a standalone native core, not yet wired into the Python front end.
 * It walks a tree with openat/fdopendir, stores each regular file once by
 * SHA-256, and writes NDJSON entries plus a JSON report.
 */
#define _GNU_SOURCE

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <ftw.h>
#include <getopt.h>
#include <grp.h>
#include <inttypes.h>
#include <limits.h>
#include <openssl/evp.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/xattr.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define FORMAT "ubackup-native-scan-v1"
#define INCOMING ".incoming"
#define IO_BUFFER (1024 * 1024)
#define MAX_DEPTH 4096
#define HL_BUCKETS 65536
#define SYNC_INTERVAL 1024

static const unsigned char zero_block[64 * 1024];

typedef struct {
    const char *source;
    const char *objects;
    const char *entries_path;
    const char *report_path;
    const char *stage;
    const char *timeline;
    const char *run_id;
    const char *identity_json;
    const char *exclusions_json;
    char **exclude_prefix;
    size_t exclude_prefix_count;
    char **exclude_name;
    size_t exclude_name_count;
    bool one_file_system;
    bool no_xattrs;
    long owner_uid;
    long owner_gid;
    bool have_owner;
} Options;

typedef struct {
    char *name;
    unsigned char *value;
    size_t length;
} Xattr;

typedef struct {
    Xattr *items;
    size_t count;
    size_t capacity;
} Xattrs;

typedef struct {
    char *path;
    char *op;
    int error;
} Omission;

typedef struct {
    uint64_t offset;
    uint64_t length;
} Segment;

typedef struct {
    Segment *items;
    size_t count;
    size_t capacity;
} Segments;

typedef struct HardLink {
    dev_t dev;
    ino_t ino;
    char *path;
    struct HardLink *next;
} HardLink;

static Options options;
static int source_fd = -1;
static dev_t source_dev;
static char *published_timeline = NULL;
static int objects_sync_fd = -1;
static uint64_t objects_since_sync;
static FILE *entries_file;
static uint64_t entry_count;
static uint64_t file_count;
static uint64_t dir_count;
static uint64_t symlink_count;
static uint64_t hardlink_count;
static uint64_t special_count;
static uint64_t logical_bytes;
static uint64_t objects_created;
static uint64_t objects_reused;
static uint64_t skipped_mounts;
static Omission *omissions;
static size_t omission_count;
static size_t omission_capacity;
static HardLink *hardlinks[HL_BUCKETS];

static void fatal(const char *format, ...) __attribute__((noreturn));

static void fatal(const char *format, ...) {
    va_list args;
    fprintf(stderr, "ubackup-core: ");
    va_start(args, format);
    vfprintf(stderr, format, args);
    va_end(args);
    fputc('\n', stderr);
    exit(1);
}

static void usage(void) {
    fprintf(stderr,
            "Usage: ubackup-core scan --source DIR --objects DIR --entries FILE [options]\n"
            "       ubackup-core restore --entries FILE --objects DIR --destination DIR [options]\n"
            "\n"
            "scan options:\n"
            "  --report FILE             JSON report path (or - for stdout)\n"
            "  --run-id ID               Optional UTC run identifier\n"
            "  --exclude-prefix REL      Skip a relative path prefix (repeatable)\n"
            "  --exclude-name NAME       Skip an exact component name (repeatable)\n"
            "  --one-file-system         Do not cross the source filesystem\n"
            "  --owner-uid N             chown created files (requires root)\n"
            "  --owner-gid N             chown created files (requires root)\n"
            "  --no-xattrs               Do not capture extended attributes or ACLs\n"
            "  --stage DIR               Stage objects locally before bulk copy\n"
            "  --timeline DIR            Publish timeline/home/home-full-RUNID\n"
            "  --identity-json JSON      Identity object for the manifest\n"
            "  --exclusions-json JSON    Exclusion list for the manifest\n"
            "\n"
            "restore options:\n"
            "  --report FILE             JSON report path (or - for stdout)\n"
            "  --preserve-owner          Restore original uid/gid (requires root)\n"
            "  --ignore-xattrs           Skip restoring extended attributes and ACLs\n"
            "  --help                    Show this help\n");
    exit(2);
}

static void *xmalloc(size_t size) {
    void *value = malloc(size ? size : 1);
    if (!value) {
        fatal("out of memory");
    }
    return value;
}

static void *xrealloc(void *pointer, size_t size) {
    void *value = realloc(pointer, size ? size : 1);
    if (!value) {
        fatal("out of memory");
    }
    return value;
}

static char *xstrdup(const char *text) {
    char *value = strdup(text);
    if (!value) {
        fatal("out of memory");
    }
    return value;
}

static char *xasprintf(const char *format, ...) {
    va_list args;
    char *value = NULL;
    va_start(args, format);
    if (vasprintf(&value, format, args) < 0) {
        va_end(args);
        fatal("out of memory");
    }
    va_end(args);
    return value;
}

static void append_string(char ***array, size_t *count, const char *value) {
    *array = xrealloc(*array, (*count + 1) * sizeof(**array));
    (*array)[*count] = xstrdup(value);
    (*count)++;
}

static void add_omission(const char *path, const char *operation, int error) {
    if (omission_count == omission_capacity) {
        omission_capacity = omission_capacity ? omission_capacity * 2 : 64;
        omissions = xrealloc(omissions, omission_capacity * sizeof(*omissions));
    }
    omissions[omission_count].path = xstrdup(path);
    omissions[omission_count].op = xstrdup(operation);
    omissions[omission_count].error = error;
    omission_count++;
}

static void ensure_dir(const char *path, mode_t mode) {
    struct stat info;
    if (mkdir(path, mode) == 0) {
        return;
    }
    if (errno != EEXIST) {
        fatal("cannot create directory %s: %s", path, strerror(errno));
    }
    if (lstat(path, &info) != 0 || S_ISLNK(info.st_mode) || !S_ISDIR(info.st_mode)) {
        fatal("path is not a real directory: %s", path);
    }
}

static void maybe_chown_path(const char *path);

static void mkdir_p(const char *path, mode_t mode) {
    char *copy = xstrdup(path);
    size_t length = strlen(copy);
    while (length > 1 && copy[length - 1] == '/') {
        copy[--length] = '\0';
    }
    for (char *cursor = copy + 1; *cursor; cursor++) {
        if (*cursor == '/') {
            *cursor = '\0';
            if (*copy) {
                struct stat before;
                bool existed = lstat(copy, &before) == 0;
                ensure_dir(copy, mode);
                if (!existed && strcmp(copy, "/") != 0) {
                    maybe_chown_path(copy);
                }
            }
            *cursor = '/';
        }
    }
    if (*copy) {
        struct stat before;
        bool existed = lstat(copy, &before) == 0;
        ensure_dir(copy, mode);
        if (!existed && strcmp(copy, "/") != 0) {
            maybe_chown_path(copy);
        }
    }
    free(copy);
}

static char *parent_dir(const char *path) {
    const char *slash = strrchr(path, '/');
    if (!slash) {
        return xstrdup(".");
    }
    if (slash == path) {
        return xstrdup("/");
    }
    size_t length = (size_t)(slash - path);
    char *value = xmalloc(length + 1);
    memcpy(value, path, length);
    value[length] = '\0';
    return value;
}

static void maybe_chown_fd(int fd) {
    if (!options.have_owner) {
        return;
    }
    if (fchown(fd, (uid_t)options.owner_uid, (gid_t)options.owner_gid) != 0) {
        fatal("cannot chown output: %s", strerror(errno));
    }
}

static void maybe_chown_path(const char *path) {
    if (!options.have_owner) {
        return;
    }
    if (chown(path, (uid_t)options.owner_uid, (gid_t)options.owner_gid) != 0) {
        fatal("cannot chown %s: %s", path, strerror(errno));
    }
}

static void fsync_directory(const char *path) {
    int fd = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) {
        fatal("cannot open directory %s for fsync: %s", path, strerror(errno));
    }
    if (fsync(fd) != 0) {
        int error = errno;
        close(fd);
        fatal("cannot fsync directory %s: %s", path, strerror(error));
    }
    close(fd);
}

static void json_string(FILE *output, const char *text) {
    fputc('"', output);
    for (const unsigned char *cursor = (const unsigned char *)text; *cursor; cursor++) {
        switch (*cursor) {
        case '"': fputs("\\\"", output); break;
        case '\\': fputs("\\\\", output); break;
        case '\b': fputs("\\b", output); break;
        case '\f': fputs("\\f", output); break;
        case '\n': fputs("\\n", output); break;
        case '\r': fputs("\\r", output); break;
        case '\t': fputs("\\t", output); break;
        default:
            if (*cursor < 0x20) {
                fprintf(output, "\\u%04x", *cursor);
            } else {
                fputc(*cursor, output);
            }
        }
    }
    fputc('"', output);
}

static char *base64_encode(const unsigned char *data, size_t length) {
    static const char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t output_length = 4 * ((length + 2) / 3);
    char *output = xmalloc(output_length + 1);
    size_t source = 0;
    size_t target = 0;
    while (source + 3 <= length) {
        unsigned value = ((unsigned)data[source] << 16) |
                         ((unsigned)data[source + 1] << 8) |
                         (unsigned)data[source + 2];
        output[target++] = alphabet[(value >> 18) & 0x3f];
        output[target++] = alphabet[(value >> 12) & 0x3f];
        output[target++] = alphabet[(value >> 6) & 0x3f];
        output[target++] = alphabet[value & 0x3f];
        source += 3;
    }
    if (source < length) {
        unsigned value = (unsigned)data[source] << 16;
        if (source + 1 < length) {
            value |= (unsigned)data[source + 1] << 8;
        }
        output[target++] = alphabet[(value >> 18) & 0x3f];
        output[target++] = alphabet[(value >> 12) & 0x3f];
        output[target++] = source + 1 < length ? alphabet[(value >> 6) & 0x3f] : '=';
        output[target++] = '=';
    }
    output[target] = '\0';
    return output;
}

static int base64_value(char value) {
    if (value >= 'A' && value <= 'Z') return value - 'A';
    if (value >= 'a' && value <= 'z') return value - 'a' + 26;
    if (value >= '0' && value <= '9') return value - '0' + 52;
    if (value == '+') return 62;
    if (value == '/') return 63;
    return -1;
}

static bool base64_decode(const char *text, unsigned char **data, size_t *length) {
    size_t input_length = strlen(text);
    if (input_length % 4 != 0) {
        return false;
    }
    size_t output_length = input_length / 4 * 3;
    if (input_length && text[input_length - 1] == '=') output_length--;
    if (input_length > 1 && text[input_length - 2] == '=') output_length--;
    unsigned char *output = xmalloc(output_length ? output_length : 1);
    size_t target = 0;
    for (size_t index = 0; index < input_length; index += 4) {
        int values[4];
        for (int part = 0; part < 4; part++) {
            char value = text[index + (size_t)part];
            values[part] = value == '=' ? -2 : base64_value(value);
            if (values[part] == -1) {
                free(output);
                return false;
            }
        }
        if (values[0] < 0 || values[1] < 0) {
            free(output);
            return false;
        }
        unsigned combined = ((unsigned)values[0] << 18) |
                            ((unsigned)values[1] << 12);
        if (values[2] >= 0) combined |= (unsigned)values[2] << 6;
        if (values[3] >= 0) combined |= (unsigned)values[3];
        if (target < output_length) output[target++] = (unsigned char)(combined >> 16);
        if (target < output_length) output[target++] = (unsigned char)(combined >> 8);
        if (target < output_length) output[target++] = (unsigned char)combined;
    }
    *data = output;
    *length = output_length;
    return true;
}

static void xattr_free(Xattrs *xattrs) {
    for (size_t index = 0; index < xattrs->count; index++) {
        free(xattrs->items[index].name);
        free(xattrs->items[index].value);
    }
    free(xattrs->items);
    memset(xattrs, 0, sizeof(*xattrs));
}

static void xattr_add(Xattrs *xattrs, const char *name,
                      const unsigned char *value, size_t length) {
    if (xattrs->count == xattrs->capacity) {
        xattrs->capacity = xattrs->capacity ? xattrs->capacity * 2 : 8;
        xattrs->items = xrealloc(xattrs->items,
                                 xattrs->capacity * sizeof(*xattrs->items));
    }
    Xattr *item = &xattrs->items[xattrs->count++];
    item->name = xstrdup(name);
    item->value = xmalloc(length ? length : 1);
    if (length) {
        memcpy(item->value, value, length);
    }
    item->length = length;
}

static void xattrs_copy(Xattrs *destination, const Xattrs *source) {
    for (size_t index = 0; index < source->count; index++) {
        xattr_add(destination, source->items[index].name,
                  source->items[index].value, source->items[index].length);
    }
}

static void collect_xattrs_fd(int fd, Xattrs *xattrs, const char *path) {
    ssize_t names_length = flistxattr(fd, NULL, 0);
    if (names_length < 0) {
        if (errno == ENOTSUP || errno == EOPNOTSUPP) {
            return;
        }
        add_omission(path, "listxattr", errno);
        return;
    }
    if (names_length == 0) {
        return;
    }
    char *names = xmalloc((size_t)names_length);
    ssize_t received = flistxattr(fd, names, (size_t)names_length);
    if (received < 0) {
        int error = errno;
        free(names);
        add_omission(path, "listxattr", error);
        return;
    }
    for (ssize_t offset = 0; offset < received;) {
        const char *name = names + offset;
        size_t name_length = strlen(name);
        offset += (ssize_t)name_length + 1;
        ssize_t value_length = fgetxattr(fd, name, NULL, 0);
        if (value_length < 0) {
            add_omission(path, "getxattr", errno);
            continue;
        }
        unsigned char *value = xmalloc(value_length ? (size_t)value_length : 1);
        ssize_t value_received = fgetxattr(fd, name, value, (size_t)value_length);
        if (value_received < 0) {
            int error = errno;
            free(value);
            add_omission(path, "getxattr", error);
            continue;
        }
        xattr_add(xattrs, name, value, (size_t)value_received);
        free(value);
    }
    free(names);
}

static void write_xattrs(FILE *output, const Xattrs *xattrs) {
    if (!xattrs || xattrs->count == 0) {
        return;
    }
    fputs(",\"xattrs\":[", output);
    for (size_t index = 0; index < xattrs->count; index++) {
        if (index) {
            fputc(',', output);
        }
        fputs("{\"name\":", output);
        json_string(output, xattrs->items[index].name);
        fputs(",\"value\":", output);
        char *encoded = base64_encode(xattrs->items[index].value,
                                      xattrs->items[index].length);
        json_string(output, encoded);
        free(encoded);
        fputc('}', output);
    }
    fputc(']', output);
}

static int64_t mtime_ns(const struct stat *info) {
    return (int64_t)info->st_mtim.tv_sec * 1000000000LL + info->st_mtim.tv_nsec;
}

static void entry_prefix(const char *path, const char *type, const struct stat *info) {
    fputc('{', entries_file);
    fputs("\"path\":", entries_file);
    json_string(entries_file, path);
    fputs(",\"type\":", entries_file);
    json_string(entries_file, type);
    fprintf(entries_file, ",\"mode\":%u,\"uid\":%ju,\"gid\":%ju,\"mtime_ns\":%lld",
            (unsigned)(info->st_mode & 07777),
            (uintmax_t)info->st_uid, (uintmax_t)info->st_gid,
            (long long)mtime_ns(info));
}

static void write_dir_entry(const char *path, const struct stat *info,
                            const Xattrs *xattrs) {
    entry_prefix(path, "dir", info);
    write_xattrs(entries_file, xattrs);
    fputs("}\n", entries_file);
    entry_count++;
    dir_count++;
}

static void write_symlink_entry(const char *path, const struct stat *info, const char *target) {
    entry_prefix(path, "symlink", info);
    fputs(",\"target\":", entries_file);
    json_string(entries_file, target);
    fputs("}\n", entries_file);
    entry_count++;
    symlink_count++;
}

static void write_hardlink_entry(const char *path, const struct stat *info, const char *target) {
    entry_prefix(path, "hardlink", info);
    fputs(",\"hardlink_to\":", entries_file);
    json_string(entries_file, target);
    fputs("}\n", entries_file);
    entry_count++;
    hardlink_count++;
}

static void write_special_entry(const char *path, const struct stat *info) {
    entry_prefix(path, "special", info);
    fputs("}\n", entries_file);
    entry_count++;
    special_count++;
}

static void write_file_entry(const char *path, const struct stat *info, const char *sha256,
                             const Segments *segments, bool sparse,
                             const Xattrs *xattrs) {
    entry_prefix(path, "file", info);
    fprintf(entries_file, ",\"size\":%llu,\"sha256\":\"%s\"",
            (unsigned long long)info->st_size, sha256);
    if (sparse) {
        fputs(",\"sparse\":[", entries_file);
        for (size_t index = 0; index < segments->count; index++) {
            if (index) {
                fputc(',', entries_file);
            }
            fprintf(entries_file, "[%" PRIu64 ",%" PRIu64 "]",
                    segments->items[index].offset, segments->items[index].length);
        }
        fputc(']', entries_file);
    }
    write_xattrs(entries_file, xattrs);
    fputs("}\n", entries_file);
    entry_count++;
    file_count++;
    logical_bytes += (uint64_t)info->st_size;
}

typedef struct {
    EVP_MD_CTX *context;
} Hasher;

static void hasher_init(Hasher *hasher) {
    hasher->context = EVP_MD_CTX_new();
    if (!hasher->context || EVP_DigestInit_ex(hasher->context, EVP_sha256(), NULL) != 1) {
        fatal("cannot initialize SHA-256");
    }
}

static void hasher_update(Hasher *hasher, const void *data, size_t size) {
    if (EVP_DigestUpdate(hasher->context, data, size) != 1) {
        fatal("SHA-256 update failed");
    }
}

static void hasher_final(Hasher *hasher, char output[65]) {
    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int size = 0;
    if (EVP_DigestFinal_ex(hasher->context, digest, &size) != 1 || size != 32) {
        EVP_MD_CTX_free(hasher->context);
        fatal("SHA-256 finalization failed");
    }
    EVP_MD_CTX_free(hasher->context);
    hasher->context = NULL;
    static const char hex[] = "0123456789abcdef";
    for (unsigned int index = 0; index < size; index++) {
        output[index * 2] = hex[digest[index] >> 4];
        output[index * 2 + 1] = hex[digest[index] & 0xf];
    }
    output[64] = '\0';
}

static void hasher_abort(Hasher *hasher) {
    if (hasher->context) {
        EVP_MD_CTX_free(hasher->context);
        hasher->context = NULL;
    }
}

static void hash_zeros(Hasher *hasher, uint64_t size) {
    while (size) {
        size_t chunk = size > sizeof(zero_block) ? sizeof(zero_block) : (size_t)size;
        hasher_update(hasher, zero_block, chunk);
        size -= chunk;
    }
}

static void add_segment(Segments *segments, uint64_t offset, uint64_t length) {
    if (segments->count == segments->capacity) {
        segments->capacity = segments->capacity ? segments->capacity * 2 : 8;
        segments->items = xrealloc(segments->items, segments->capacity * sizeof(*segments->items));
    }
    segments->items[segments->count].offset = offset;
    segments->items[segments->count].length = length;
    segments->count++;
}

static void free_segments(Segments *segments) {
    free(segments->items);
    segments->items = NULL;
    segments->count = 0;
    segments->capacity = 0;
}

static int collect_sparse(int fd, uint64_t size, Segments *segments) {
    if (size == 0) {
        return 0;
    }
    uint64_t position = 0;
    while (position < size) {
        errno = 0;
        off_t data = lseek(fd, (off_t)position, SEEK_DATA);
        if (data < 0) {
            if (errno == ENXIO) {
                break;
            }
            return -1;
        }
        if ((uint64_t)data >= size) {
            break;
        }
        off_t hole = lseek(fd, data, SEEK_HOLE);
        if (hole < 0) {
            return -1;
        }
        if ((uint64_t)hole > size) {
            hole = (off_t)size;
        }
        if (hole <= data) {
            break;
        }
        add_segment(segments, (uint64_t)data, (uint64_t)(hole - data));
        position = (uint64_t)hole;
    }
    return 0;
}

static int write_all(int fd, const void *buffer, size_t size, off_t offset, bool positioned) {
    const unsigned char *cursor = buffer;
    while (size) {
        ssize_t written = positioned
            ? pwrite(fd, cursor, size, offset)
            : write(fd, cursor, size);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        cursor += written;
        size -= (size_t)written;
        if (positioned) {
            offset += written;
        }
    }
    return 0;
}

static void ensure_object_dir(const char *objects) {
    mkdir_p(objects, 0700);
    maybe_chown_path(objects);
    char *incoming = xasprintf("%s/%s", objects, INCOMING);
    ensure_dir(incoming, 0700);
    maybe_chown_path(incoming);
    free(incoming);
}

static void sync_objects(void) {
    if (objects_sync_fd < 0) {
        objects_sync_fd = open(options.objects,
                               O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (objects_sync_fd < 0) {
            fatal("cannot open object store for sync: %s", strerror(errno));
        }
    }
    if (syncfs(objects_sync_fd) != 0) {
        fatal("cannot sync object store: %s", strerror(errno));
    }
    objects_since_sync = 0;
}

static void object_paths(const char *objects, const char *sha256, char **shard, char **path) {
    *shard = xasprintf("%s/%c%c", objects, sha256[0], sha256[1]);
    *path = xasprintf("%s/%s", *shard, sha256);
}

static void verify_existing_object(const char *path, uint64_t size) {
    struct stat info;
    if (lstat(path, &info) != 0 || S_ISLNK(info.st_mode) ||
        !S_ISREG(info.st_mode) || (uint64_t)info.st_size != size) {
        fatal("existing object has the wrong size or type: %s", path);
    }
}

static bool publish_with_rename(const char *temporary, const char *destination) {
    if (renameat2(AT_FDCWD, temporary, AT_FDCWD, destination, RENAME_NOREPLACE) == 0) {
        return true;
    }
    if (errno == EEXIST) {
        return false;
    }
    if (errno == EINVAL || errno == ENOSYS) {
        if (link(temporary, destination) == 0) {
            unlink(temporary);
            return true;
        }
        if (errno == EEXIST) {
            return false;
        }
    }
    fatal("cannot publish object %s: %s", destination, strerror(errno));
}

static void publish_object(int fd, char *temporary, const char *sha256, uint64_t size) {
    const char *write_root = options.stage ? options.stage : options.objects;
    char *final_shard = NULL;
    char *final_destination = NULL;
    object_paths(options.objects, sha256, &final_shard, &final_destination);
    if (lstat(final_destination, &(struct stat){0}) == 0) {
        verify_existing_object(final_destination, size);
        close(fd);
        unlink(temporary);
        objects_reused++;
        free(final_shard);
        free(final_destination);
        free(temporary);
        return;
    }
    if (errno != ENOENT) {
        int error = errno;
        close(fd);
        unlink(temporary);
        fatal("cannot inspect object %s: %s", final_destination, strerror(error));
    }
    free(final_shard);
    free(final_destination);

    char *shard = NULL;
    char *destination = NULL;
    object_paths(write_root, sha256, &shard, &destination);
    struct stat shard_info;
    bool shard_existed = lstat(shard, &shard_info) == 0;
    if (!shard_existed && errno != ENOENT) {
        int error = errno;
        close(fd);
        unlink(temporary);
        fatal("cannot inspect object shard %s: %s", shard, strerror(error));
    }
    if (fchmod(fd, 0400) != 0) {
        int error = errno;
        close(fd);
        unlink(temporary);
        fatal("cannot finalize object %s: %s", temporary, strerror(error));
    }
    close(fd);
    ensure_dir(shard, 0700);
    if (!options.stage && !shard_existed) {
        maybe_chown_path(shard);
    }
    bool created = publish_with_rename(temporary, destination);
    if (!created) {
        verify_existing_object(destination, size);
        unlink(temporary);
        objects_reused++;
    } else {
        if (!options.stage) {
            maybe_chown_path(destination);
            objects_since_sync++;
            if (objects_since_sync >= SYNC_INTERVAL) {
                sync_objects();
            }
        }
        objects_created++;
    }
    free(shard);
    free(destination);
    free(temporary);
}

static int make_temporary(char **path) {
    const char *write_root = options.stage ? options.stage : options.objects;
    char *template = xasprintf("%s/%s/obj-XXXXXX", write_root, INCOMING);
    int fd = mkstemp(template);
    if (fd < 0) {
        free(template);
        return -1;
    }
    if (fchmod(fd, 0600) != 0) {
        int error = errno;
        close(fd);
        unlink(template);
        free(template);
        errno = error;
        return -1;
    }
    *path = template;
    return fd;
}

static unsigned hardlink_bucket(dev_t dev, ino_t ino) {
    uint64_t value = (uint64_t)dev * 1315423911u ^ (uint64_t)ino;
    value ^= value >> 33;
    value *= 0xff51afd7ed558ccdULL;
    value ^= value >> 33;
    return (unsigned)(value & (HL_BUCKETS - 1));
}

static char *hardlink_lookup(dev_t dev, ino_t ino) {
    for (HardLink *entry = hardlinks[hardlink_bucket(dev, ino)]; entry; entry = entry->next) {
        if (entry->dev == dev && entry->ino == ino) {
            return entry->path;
        }
    }
    return NULL;
}

static void hardlink_insert(dev_t dev, ino_t ino, const char *path) {
    unsigned bucket = hardlink_bucket(dev, ino);
    HardLink *entry = xmalloc(sizeof(*entry));
    entry->dev = dev;
    entry->ino = ino;
    entry->path = xstrdup(path);
    entry->next = hardlinks[bucket];
    hardlinks[bucket] = entry;
}

static int open_source_file(int dirfd, const char *name) {
    int flags = O_RDONLY | O_NOFOLLOW | O_CLOEXEC;
    int fd = openat(dirfd, name, flags | O_NOATIME);
    if (fd < 0 && errno == EPERM) {
        fd = openat(dirfd, name, flags);
    }
    return fd;
}

static bool file_changed(const struct stat *before, const struct stat *after) {
    return before->st_size != after->st_size ||
           before->st_mtim.tv_sec != after->st_mtim.tv_sec ||
           before->st_mtim.tv_nsec != after->st_mtim.tv_nsec ||
           before->st_ctim.tv_sec != after->st_ctim.tv_sec ||
           before->st_ctim.tv_nsec != after->st_ctim.tv_nsec;
}

static void process_file(int dirfd, const char *name, const char *relative,
                         const struct stat *listed) {
    for (int attempt = 0; attempt < 2; attempt++) {
        int fd = open_source_file(dirfd, name);
        if (fd < 0) {
            if (errno == ENOENT) {
                return;
            }
            add_omission(relative, "open", errno);
            return;
        }
        struct stat info;
        if (fstat(fd, &info) != 0) {
            int error = errno;
            close(fd);
            add_omission(relative, "fstat", error);
            return;
        }
        if (!S_ISREG(info.st_mode) ||
            info.st_dev != listed->st_dev || info.st_ino != listed->st_ino) {
            close(fd);
            add_omission(relative, "fstat", ESTALE);
            return;
        }
        if (info.st_nlink > 1) {
            char *first = hardlink_lookup(info.st_dev, info.st_ino);
            if (first) {
                write_hardlink_entry(relative, &info, first);
                close(fd);
                return;
            }
        }

        Xattrs xattrs = {0};
        if (!options.no_xattrs) {
            collect_xattrs_fd(fd, &xattrs, relative);
        }
        Segments segments = {0};
        bool sparse = false;
        if (info.st_size > 0) {
            if (collect_sparse(fd, (uint64_t)info.st_size, &segments) == 0) {
                if (!(segments.count == 1 && segments.items[0].offset == 0 &&
                      segments.items[0].length == (uint64_t)info.st_size)) {
                    sparse = true;
                }
            } else if (errno != EINVAL && errno != ENOTSUP) {
                int error = errno;
                close(fd);
                free_segments(&segments);
                xattr_free(&xattrs);
                add_omission(relative, "sparse-map", error);
                return;
            }
        }

        char *temporary = NULL;
        int temporary_fd = make_temporary(&temporary);
        if (temporary_fd < 0) {
            int error = errno;
            close(fd);
            free_segments(&segments);
            xattr_free(&xattrs);
            fatal("cannot create object for %s: %s", relative, strerror(error));
            return;
        }
        Hasher hasher;
        hasher_init(&hasher);
        unsigned char *buffer = xmalloc(IO_BUFFER);
        int read_error = 0;
        int write_error = 0;

        if (sparse) {
            uint64_t current = 0;
            for (size_t index = 0; index < segments.count && !read_error; index++) {
                Segment segment = segments.items[index];
                if (segment.offset > current) {
                    hash_zeros(&hasher, segment.offset - current);
                }
                uint64_t offset = segment.offset;
                uint64_t remaining = segment.length;
                while (remaining) {
                    size_t chunk = remaining > IO_BUFFER ? IO_BUFFER : (size_t)remaining;
                    ssize_t got = pread(fd, buffer, chunk, (off_t)offset);
                    if (got < 0) {
                        if (errno == EINTR) {
                            continue;
                        }
                        read_error = errno;
                        break;
                    }
                    if (got == 0) {
                        read_error = EIO;
                        break;
                    }
                    if (write_all(temporary_fd, buffer, (size_t)got, (off_t)offset, true) != 0) {
                        write_error = errno;
                        break;
                    }
                    hasher_update(&hasher, buffer, (size_t)got);
                    offset += (uint64_t)got;
                    remaining -= (uint64_t)got;
                }
                current = segment.offset + segment.length;
            }
            if (!read_error && current < (uint64_t)info.st_size) {
                hash_zeros(&hasher, (uint64_t)info.st_size - current);
            }
            if (!read_error && !write_error && ftruncate(temporary_fd, info.st_size) != 0) {
                write_error = errno;
            }
        } else {
            if (lseek(fd, 0, SEEK_SET) < 0) {
                read_error = errno;
            }
            uint64_t total = 0;
            while (!read_error && total < (uint64_t)info.st_size) {
                size_t chunk = (uint64_t)(info.st_size - (off_t)total) > IO_BUFFER
                    ? IO_BUFFER : (size_t)(info.st_size - (off_t)total);
                ssize_t got = read(fd, buffer, chunk);
                if (got < 0) {
                    if (errno == EINTR) {
                        continue;
                    }
                    read_error = errno;
                    break;
                }
                if (got == 0) {
                    read_error = EIO;
                    break;
                }
                if (write_all(temporary_fd, buffer, (size_t)got, (off_t)total, true) != 0) {
                    write_error = errno;
                    break;
                }
                hasher_update(&hasher, buffer, (size_t)got);
                total += (uint64_t)got;
            }
            if (!read_error && total != (uint64_t)info.st_size) {
                read_error = EIO;
            }
            if (!read_error && !write_error && ftruncate(temporary_fd, info.st_size) != 0) {
                write_error = errno;
            }
        }
        free(buffer);
        if (write_error) {
            hasher_abort(&hasher);
            close(temporary_fd);
            unlink(temporary);
            free(temporary);
            free_segments(&segments);
            xattr_free(&xattrs);
            fatal("cannot write object for %s: %s", relative, strerror(write_error));
        }

        struct stat after;
        bool changed = fstat(fd, &after) != 0 || file_changed(&info, &after);
        close(fd);
        if (read_error) {
            hasher_abort(&hasher);
            close(temporary_fd);
            unlink(temporary);
            free(temporary);
            free_segments(&segments);
            xattr_free(&xattrs);
            if (attempt == 0 && (read_error == EIO || read_error == ESTALE)) {
                continue;
            }
            add_omission(relative, "read", read_error);
            return;
        }
        if (changed) {
            hasher_abort(&hasher);
            close(temporary_fd);
            unlink(temporary);
            free(temporary);
            free_segments(&segments);
            xattr_free(&xattrs);
            if (attempt == 0) {
                continue;
            }
            add_omission(relative, "changed-during-read", ESTALE);
            return;
        }

        char sha256[65];
        hasher_final(&hasher, sha256);
        publish_object(temporary_fd, temporary, sha256, (uint64_t)info.st_size);
        write_file_entry(relative, &info, sha256, &segments, sparse, &xattrs);
        if (info.st_nlink > 1) {
            hardlink_insert(info.st_dev, info.st_ino, relative);
        }
        free_segments(&segments);
        xattr_free(&xattrs);
        return;
    }
}

static bool excluded(const char *relative, const char *name) {
    for (size_t index = 0; index < options.exclude_name_count; index++) {
        if (strcmp(name, options.exclude_name[index]) == 0) {
            return true;
        }
    }
    for (size_t index = 0; index < options.exclude_prefix_count; index++) {
        const char *prefix = options.exclude_prefix[index];
        size_t length = strlen(prefix);
        if (strcmp(relative, prefix) == 0 ||
            (strncmp(relative, prefix, length) == 0 && relative[length] == '/')) {
            return true;
        }
    }
    return false;
}

static char *read_link(int dirfd, const char *name) {
    size_t size = 256;
    for (;;) {
        char *buffer = xmalloc(size + 1);
        ssize_t length = readlinkat(dirfd, name, buffer, size);
        if (length < 0) {
            int error = errno;
            free(buffer);
            errno = error;
            return NULL;
        }
        if ((size_t)length < size) {
            buffer[length] = '\0';
            return buffer;
        }
        free(buffer);
        if (size > 1024 * 1024) {
            errno = ENAMETOOLONG;
            return NULL;
        }
        size *= 2;
    }
}

static void scan_directory(int directory_fd, const char *relative, int depth) {
    if (depth > MAX_DEPTH) {
        add_omission(relative, "depth", ENAMETOOLONG);
        close(directory_fd);
        return;
    }
    DIR *directory = fdopendir(directory_fd);
    if (!directory) {
        int error = errno;
        close(directory_fd);
        add_omission(relative, "opendir", error);
        return;
    }
    int fd = dirfd(directory);
    struct dirent *item;
    errno = 0;
    while ((item = readdir(directory)) != NULL) {
        if (strcmp(item->d_name, ".") == 0 || strcmp(item->d_name, "..") == 0) {
            continue;
        }
        char *child = relative[0]
            ? xasprintf("%s/%s", relative, item->d_name)
            : xstrdup(item->d_name);
        if (excluded(child, item->d_name)) {
            free(child);
            continue;
        }
        struct stat info;
        if (fstatat(fd, item->d_name, &info, AT_SYMLINK_NOFOLLOW) != 0) {
            if (errno != ENOENT) {
                add_omission(child, "fstatat", errno);
            }
            free(child);
            continue;
        }
        if (options.one_file_system && info.st_dev != source_dev) {
            skipped_mounts++;
            free(child);
            continue;
        }
        if (S_ISLNK(info.st_mode)) {
            char *target = read_link(fd, item->d_name);
            if (!target) {
                add_omission(child, "readlink", errno);
            } else {
                write_symlink_entry(child, &info, target);
                free(target);
            }
        } else if (S_ISDIR(info.st_mode)) {
            int child_fd = openat(fd, item->d_name,
                                  O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
            Xattrs xattrs = {0};
            if (child_fd >= 0 && !options.no_xattrs) {
                collect_xattrs_fd(child_fd, &xattrs, child);
            }
            write_dir_entry(child, &info, &xattrs);
            xattr_free(&xattrs);
            if (child_fd < 0) {
                add_omission(child, "open-directory", errno);
            } else {
                scan_directory(child_fd, child, depth + 1);
            }
        } else if (S_ISREG(info.st_mode)) {
            process_file(fd, item->d_name, child, &info);
        } else {
            write_special_entry(child, &info);
        }
        free(child);
        errno = 0;
    }
    if (errno != 0) {
        add_omission(relative, "readdir", errno);
    }
    closedir(directory);
}

static void open_entries(void) {
    char *parent = parent_dir(options.entries_path);
    mkdir_p(parent, 0700);
    free(parent);
    int fd = open(options.entries_path,
                  O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) {
        fatal("cannot open entries file %s: %s", options.entries_path, strerror(errno));
    }
    maybe_chown_fd(fd);
    entries_file = fdopen(fd, "w");
    if (!entries_file) {
        int error = errno;
        close(fd);
        fatal("cannot open entries stream: %s", strerror(error));
    }
    setvbuf(entries_file, NULL, _IOFBF, IO_BUFFER);
}

static void close_entries(void) {
    if (!entries_file) {
        return;
    }
    if (fflush(entries_file) != 0 || ferror(entries_file)) {
        fatal("cannot flush entries file: %s", strerror(errno));
    }
    if (fsync(fileno(entries_file)) != 0) {
        fatal("cannot fsync entries file: %s", strerror(errno));
    }
    if (fclose(entries_file) != 0) {
        fatal("cannot close entries file: %s", strerror(errno));
    }
    entries_file = NULL;
}

static void write_report(void) {
    if (!options.report_path) {
        return;
    }
    FILE *output = NULL;
    char *temporary = NULL;
    int fd = -1;
    if (strcmp(options.report_path, "-") == 0) {
        output = stdout;
    } else {
        char *parent = parent_dir(options.report_path);
        mkdir_p(parent, 0700);
        free(parent);
        temporary = xasprintf("%s.tmp-XXXXXX", options.report_path);
        fd = mkstemp(temporary);
        if (fd < 0) {
            fatal("cannot create report %s: %s", temporary, strerror(errno));
        }
        if (fchmod(fd, 0600) != 0) {
            fatal("cannot protect report %s: %s", temporary, strerror(errno));
        }
        maybe_chown_fd(fd);
        output = fdopen(fd, "w");
        if (!output) {
            fatal("cannot open report stream: %s", strerror(errno));
        }
    }

    fputs("{\"format\":", output);
    json_string(output, FORMAT);
    fputs(",\"source\":", output);
    json_string(output, options.source);
    if (options.run_id) {
        fputs(",\"run_id\":", output);
        json_string(output, options.run_id);
    }
    if (published_timeline) {
        fputs(",\"timeline\":", output);
        json_string(output, published_timeline);
    }
    fprintf(output,
            ",\"entries\":%" PRIu64 ",\"files\":%" PRIu64 ",\"dirs\":%" PRIu64
            ",\"symlinks\":%" PRIu64 ",\"hardlinks\":%" PRIu64
            ",\"special\":%" PRIu64 ",\"logical_bytes\":%" PRIu64
            ",\"objects_created\":%" PRIu64 ",\"objects_reused\":%" PRIu64
            ",\"skipped_mounts\":%" PRIu64 ",\"omissions\":[",
            entry_count, file_count, dir_count, symlink_count, hardlink_count,
            special_count, logical_bytes, objects_created, objects_reused, skipped_mounts);
    for (size_t index = 0; index < omission_count; index++) {
        if (index) {
            fputc(',', output);
        }
        fputs("{\"path\":", output);
        json_string(output, omissions[index].path);
        fputs(",\"operation\":", output);
        json_string(output, omissions[index].op);
        fprintf(output, ",\"errno\":%d,\"message\":", omissions[index].error);
        json_string(output, strerror(omissions[index].error));
        fputc('}', output);
    }
    fprintf(output, "],\"omission_count\":%zu,\"complete\":%s}\n",
            omission_count, omission_count ? "false" : "true");

    if (output == stdout) {
        if (fflush(output) != 0) {
            fatal("cannot flush report: %s", strerror(errno));
        }
        return;
    }
    if (fflush(output) != 0 || fsync(fd) != 0 || fclose(output) != 0) {
        fatal("cannot finalize report %s: %s", temporary, strerror(errno));
    }
    if (rename(temporary, options.report_path) != 0) {
        fatal("cannot publish report %s: %s", options.report_path, strerror(errno));
    }
    free(temporary);
}

static void parse_id(const char *text, long *value, const char *name) {
    char *end = NULL;
    errno = 0;
    long parsed = strtol(text, &end, 10);
    if (errno || !end || *end || parsed < 0) {
        fatal("invalid %s: %s", name, text);
    }
    *value = parsed;
}

typedef struct {
    char *path;
    char *type;
    char *sha256;
    char *target;
    char *hardlink_to;
    unsigned mode;
    uint64_t uid;
    uint64_t gid;
    int64_t mtime_ns;
    uint64_t size;
    bool has_size;
    Segment *sparse;
    size_t sparse_count;
    size_t sparse_capacity;
    Xattrs xattrs;
} RestoreEntry;

typedef struct {
    const char *entries_path;
    const char *objects;
    const char *destination;
    const char *report_path;
    bool preserve_owner;
    bool ignore_xattrs;
} RestoreOptions;

typedef struct {
    const char *text;
    size_t position;
} JsonParser;

typedef struct {
    char *path;
    unsigned mode;
    uint64_t uid;
    uint64_t gid;
    int64_t mtime_ns;
    Xattrs xattrs;
} PendingDir;

static RestoreOptions restore_options;
static PendingDir *pending_dirs;
static size_t pending_dir_count;
static size_t pending_dir_capacity;
static int restore_objects_fd = -1;
static int restore_destination_fd = -1;
static uint64_t restore_entries;
static uint64_t restore_files;
static uint64_t restore_dirs;
static uint64_t restore_symlinks;
static uint64_t restore_hardlinks;
static uint64_t restore_special;
static uint64_t restore_bytes;

static void restore_entry_free(RestoreEntry *entry) {
    free(entry->path);
    free(entry->type);
    free(entry->sha256);
    free(entry->target);
    free(entry->hardlink_to);
    free(entry->sparse);
    xattr_free(&entry->xattrs);
    memset(entry, 0, sizeof(*entry));
}

static void json_skip_ws(JsonParser *parser) {
    while (parser->text[parser->position] &&
           isspace((unsigned char)parser->text[parser->position])) {
        parser->position++;
    }
}

static bool json_take(JsonParser *parser, char expected) {
    json_skip_ws(parser);
    if (parser->text[parser->position] != expected) {
        return false;
    }
    parser->position++;
    return true;
}

static int json_hex_value(char value) {
    if (value >= '0' && value <= '9') return value - '0';
    if (value >= 'a' && value <= 'f') return value - 'a' + 10;
    if (value >= 'A' && value <= 'F') return value - 'A' + 10;
    return -1;
}

static bool json_hex4(JsonParser *parser, unsigned *value) {
    unsigned result = 0;
    for (int index = 0; index < 4; index++) {
        int digit = json_hex_value(parser->text[parser->position++]);
        if (digit < 0) {
            return false;
        }
        result = (result << 4) | (unsigned)digit;
    }
    *value = result;
    return true;
}

static void append_byte(char **buffer, size_t *length, size_t *capacity, unsigned char value) {
    if (*length + 1 >= *capacity) {
        *capacity *= 2;
        *buffer = xrealloc(*buffer, *capacity);
    }
    (*buffer)[(*length)++] = (char)value;
}

static void append_utf8(char **buffer, size_t *length, size_t *capacity, unsigned value) {
    if (value < 0x80) {
        append_byte(buffer, length, capacity, (unsigned char)value);
    } else if (value < 0x800) {
        append_byte(buffer, length, capacity, (unsigned char)(0xc0 | (value >> 6)));
        append_byte(buffer, length, capacity, (unsigned char)(0x80 | (value & 0x3f)));
    } else if (value < 0x10000) {
        append_byte(buffer, length, capacity, (unsigned char)(0xe0 | (value >> 12)));
        append_byte(buffer, length, capacity, (unsigned char)(0x80 | ((value >> 6) & 0x3f)));
        append_byte(buffer, length, capacity, (unsigned char)(0x80 | (value & 0x3f)));
    } else {
        append_byte(buffer, length, capacity, (unsigned char)(0xf0 | (value >> 18)));
        append_byte(buffer, length, capacity, (unsigned char)(0x80 | ((value >> 12) & 0x3f)));
        append_byte(buffer, length, capacity, (unsigned char)(0x80 | ((value >> 6) & 0x3f)));
        append_byte(buffer, length, capacity, (unsigned char)(0x80 | (value & 0x3f)));
    }
}

static char *json_parse_string(JsonParser *parser) {
    json_skip_ws(parser);
    if (parser->text[parser->position] != '"') {
        return NULL;
    }
    parser->position++;
    size_t capacity = 64;
    size_t length = 0;
    char *result = xmalloc(capacity);
    while (true) {
        unsigned char value = (unsigned char)parser->text[parser->position];
        if (!value) {
            free(result);
            return NULL;
        }
        if (value == '"') {
            parser->position++;
            result[length] = '\0';
            return result;
        }
        if (value == '\\') {
            parser->position++;
            char escape = parser->text[parser->position++];
            switch (escape) {
            case '"': append_byte(&result, &length, &capacity, '"'); break;
            case '\\': append_byte(&result, &length, &capacity, '\\'); break;
            case '/': append_byte(&result, &length, &capacity, '/'); break;
            case 'b': append_byte(&result, &length, &capacity, '\b'); break;
            case 'f': append_byte(&result, &length, &capacity, '\f'); break;
            case 'n': append_byte(&result, &length, &capacity, '\n'); break;
            case 'r': append_byte(&result, &length, &capacity, '\r'); break;
            case 't': append_byte(&result, &length, &capacity, '\t'); break;
            case 'u': {
                unsigned codepoint;
                if (!json_hex4(parser, &codepoint)) {
                    free(result);
                    return NULL;
                }
                if (codepoint >= 0xd800 && codepoint <= 0xdbff) {
                    if (parser->text[parser->position] != '\\' ||
                        parser->text[parser->position + 1] != 'u') {
                        free(result);
                        return NULL;
                    }
                    parser->position += 2;
                    unsigned low;
                    if (!json_hex4(parser, &low) || low < 0xdc00 || low > 0xdfff) {
                        free(result);
                        return NULL;
                    }
                    codepoint = 0x10000 + ((codepoint - 0xd800) << 10) + (low - 0xdc00);
                }
                if (codepoint == 0) {
                    free(result);
                    return NULL;
                }
                append_utf8(&result, &length, &capacity, codepoint);
                break;
            }
            default:
                free(result);
                return NULL;
            }
            continue;
        }
        if (value < 0x20) {
            free(result);
            return NULL;
        }
        append_byte(&result, &length, &capacity, value);
        parser->position++;
    }
}

static bool json_parse_uint(JsonParser *parser, uint64_t *value) {
    json_skip_ws(parser);
    const char *start = parser->text + parser->position;
    if (*start == '-') {
        return false;
    }
    errno = 0;
    char *end = NULL;
    unsigned long long parsed = strtoull(start, &end, 10);
    if (end == start || errno) {
        return false;
    }
    parser->position += (size_t)(end - start);
    *value = (uint64_t)parsed;
    return true;
}

static bool json_parse_int(JsonParser *parser, int64_t *value) {
    json_skip_ws(parser);
    const char *start = parser->text + parser->position;
    errno = 0;
    char *end = NULL;
    long long parsed = strtoll(start, &end, 10);
    if (end == start || errno) {
        return false;
    }
    parser->position += (size_t)(end - start);
    *value = (int64_t)parsed;
    return true;
}

static bool json_skip_value(JsonParser *parser, int depth) {
    if (depth > 64) {
        return false;
    }
    json_skip_ws(parser);
    char value = parser->text[parser->position];
    if (value == '"') {
        char *text = json_parse_string(parser);
        free(text);
        return text != NULL;
    }
    if (value == '{') {
        parser->position++;
        if (json_take(parser, '}')) {
            return true;
        }
        for (;;) {
            char *key = json_parse_string(parser);
            if (!key || !json_take(parser, ':')) {
                free(key);
                return false;
            }
            free(key);
            if (!json_skip_value(parser, depth + 1)) {
                return false;
            }
            if (json_take(parser, ',')) {
                continue;
            }
            return json_take(parser, '}');
        }
    }
    if (value == '[') {
        parser->position++;
        if (json_take(parser, ']')) {
            return true;
        }
        for (;;) {
            if (!json_skip_value(parser, depth + 1)) {
                return false;
            }
            if (json_take(parser, ',')) {
                continue;
            }
            return json_take(parser, ']');
        }
    }
    if (strncmp(parser->text + parser->position, "true", 4) == 0) {
        parser->position += 4;
        return true;
    }
    if (strncmp(parser->text + parser->position, "false", 5) == 0) {
        parser->position += 5;
        return true;
    }
    if (strncmp(parser->text + parser->position, "null", 4) == 0) {
        parser->position += 4;
        return true;
    }
    const char *start = parser->text + parser->position;
    char *end = NULL;
    errno = 0;
    (void)strtod(start, &end);
    if (end == start || errno) {
        return false;
    }
    parser->position += (size_t)(end - start);
    return true;
}

static void add_restore_segment(RestoreEntry *entry, uint64_t offset, uint64_t length) {
    if (entry->sparse_count == entry->sparse_capacity) {
        entry->sparse_capacity = entry->sparse_capacity ? entry->sparse_capacity * 2 : 8;
        entry->sparse = xrealloc(entry->sparse,
                                 entry->sparse_capacity * sizeof(*entry->sparse));
    }
    entry->sparse[entry->sparse_count].offset = offset;
    entry->sparse[entry->sparse_count].length = length;
    entry->sparse_count++;
}

static bool safe_relative_path(const char *path) {
    if (!path || !*path || path[0] == '/' || path[strlen(path) - 1] == '/') {
        return false;
    }
    const char *cursor = path;
    while (*cursor) {
        const char *slash = strchr(cursor, '/');
        size_t length = slash ? (size_t)(slash - cursor) : strlen(cursor);
        if (length == 0 ||
            (length == 1 && cursor[0] == '.') ||
            (length == 2 && cursor[0] == '.' && cursor[1] == '.')) {
            return false;
        }
        if (!slash) {
            break;
        }
        cursor = slash + 1;
    }
    return true;
}

static bool valid_sha256(const char *text) {
    if (!text || strlen(text) != 64) {
        return false;
    }
    for (size_t index = 0; index < 64; index++) {
        if (!isxdigit((unsigned char)text[index])) {
            return false;
        }
    }
    return true;
}

static void parse_restore_entry(const char *line, size_t line_number, RestoreEntry *entry) {
    memset(entry, 0, sizeof(*entry));
    JsonParser parser = {line, 0};
    if (!json_take(&parser, '{')) {
        fatal("invalid entries line %zu", line_number);
    }
    for (;;) {
        json_skip_ws(&parser);
        if (json_take(&parser, '}')) {
            break;
        }
        char *key = json_parse_string(&parser);
        if (!key || !json_take(&parser, ':')) {
            free(key);
            fatal("invalid entries line %zu", line_number);
        }
        if (strcmp(key, "path") == 0) {
            entry->path = json_parse_string(&parser);
        } else if (strcmp(key, "type") == 0) {
            entry->type = json_parse_string(&parser);
        } else if (strcmp(key, "sha256") == 0) {
            entry->sha256 = json_parse_string(&parser);
        } else if (strcmp(key, "target") == 0) {
            entry->target = json_parse_string(&parser);
        } else if (strcmp(key, "hardlink_to") == 0) {
            entry->hardlink_to = json_parse_string(&parser);
        } else if (strcmp(key, "mode") == 0) {
            uint64_t value;
            if (!json_parse_uint(&parser, &value)) fatal("invalid entries line %zu", line_number);
            entry->mode = (unsigned)value;
        } else if (strcmp(key, "uid") == 0) {
            if (!json_parse_uint(&parser, &entry->uid)) fatal("invalid entries line %zu", line_number);
        } else if (strcmp(key, "gid") == 0) {
            if (!json_parse_uint(&parser, &entry->gid)) fatal("invalid entries line %zu", line_number);
        } else if (strcmp(key, "mtime_ns") == 0) {
            if (!json_parse_int(&parser, &entry->mtime_ns)) fatal("invalid entries line %zu", line_number);
        } else if (strcmp(key, "size") == 0) {
            if (!json_parse_uint(&parser, &entry->size)) fatal("invalid entries line %zu", line_number);
            entry->has_size = true;
        } else if (strcmp(key, "xattrs") == 0) {
            if (!json_take(&parser, '[')) fatal("invalid entries line %zu", line_number);
            for (;;) {
                json_skip_ws(&parser);
                if (json_take(&parser, ']')) {
                    break;
                }
                if (!json_take(&parser, '{')) fatal("invalid entries line %zu", line_number);
                char *name = NULL;
                char *encoded = NULL;
                for (;;) {
                    json_skip_ws(&parser);
                    if (json_take(&parser, '}')) {
                        break;
                    }
                    char *attribute = json_parse_string(&parser);
                    if (!attribute || !json_take(&parser, ':')) {
                        free(attribute);
                        free(name);
                        free(encoded);
                        fatal("invalid entries line %zu", line_number);
                    }
                    if (strcmp(attribute, "name") == 0) {
                        free(name);
                        name = json_parse_string(&parser);
                    } else if (strcmp(attribute, "value") == 0) {
                        free(encoded);
                        encoded = json_parse_string(&parser);
                    } else if (!json_skip_value(&parser, 0)) {
                        free(attribute);
                        free(name);
                        free(encoded);
                        fatal("invalid entries line %zu", line_number);
                    }
                    free(attribute);
                    if (json_take(&parser, ',')) {
                        continue;
                    }
                    if (json_take(&parser, '}')) {
                        break;
                    }
                    free(name);
                    free(encoded);
                    fatal("invalid entries line %zu", line_number);
                }
                if (!name || !encoded) {
                    free(name);
                    free(encoded);
                    fatal("invalid xattr at line %zu", line_number);
                }
                unsigned char *value = NULL;
                size_t value_length = 0;
                if (!base64_decode(encoded, &value, &value_length)) {
                    free(name);
                    free(encoded);
                    fatal("invalid xattr encoding at line %zu", line_number);
                }
                xattr_add(&entry->xattrs, name, value, value_length);
                free(value);
                free(name);
                free(encoded);
                if (json_take(&parser, ',')) {
                    continue;
                }
                if (json_take(&parser, ']')) {
                    break;
                }
                fatal("invalid entries line %zu", line_number);
            }
        } else if (strcmp(key, "sparse") == 0) {
            if (!json_take(&parser, '[')) fatal("invalid entries line %zu", line_number);
            for (;;) {
                json_skip_ws(&parser);
                if (json_take(&parser, ']')) {
                    break;
                }
                uint64_t offset;
                uint64_t length;
                if (!json_take(&parser, '[') ||
                    !json_parse_uint(&parser, &offset) ||
                    !json_take(&parser, ',') ||
                    !json_parse_uint(&parser, &length) ||
                    !json_take(&parser, ']')) {
                    fatal("invalid entries line %zu", line_number);
                }
                add_restore_segment(entry, offset, length);
                if (json_take(&parser, ',')) {
                    continue;
                }
                if (json_take(&parser, ']')) {
                    break;
                }
                fatal("invalid entries line %zu", line_number);
            }
        } else if (!json_skip_value(&parser, 0)) {
            free(key);
            fatal("invalid entries line %zu", line_number);
        }
        free(key);
        if (json_take(&parser, ',')) {
            continue;
        }
        if (json_take(&parser, '}')) {
            break;
        }
        fatal("invalid entries line %zu", line_number);
    }
    json_skip_ws(&parser);
    if (parser.text[parser.position] &&
        parser.text[parser.position] != '\n' &&
        parser.text[parser.position] != '\r') {
        fatal("trailing data in entries line %zu", line_number);
    }
    if (!entry->path || !entry->type || !safe_relative_path(entry->path)) {
        fatal("unsafe or incomplete entries line %zu", line_number);
    }
    if (strcmp(entry->type, "file") == 0) {
        if (!entry->has_size || !valid_sha256(entry->sha256)) {
            fatal("invalid file entry at line %zu", line_number);
        }
    } else if (strcmp(entry->type, "symlink") == 0) {
        if (!entry->target) fatal("invalid symlink entry at line %zu", line_number);
    } else if (strcmp(entry->type, "hardlink") == 0) {
        if (!entry->hardlink_to || !safe_relative_path(entry->hardlink_to)) {
            fatal("invalid hardlink entry at line %zu", line_number);
        }
    } else if (strcmp(entry->type, "dir") != 0 && strcmp(entry->type, "special") != 0) {
        fatal("unknown entry type at line %zu", line_number);
    }
}

static int open_parent_dir_at(int rootfd, const char *path, char **leaf) {
    char *copy = xstrdup(path);
    char *save = NULL;
    char *component = strtok_r(copy, "/", &save);
    if (!component) {
        free(copy);
        fatal("invalid restore path: %s", path);
    }
    int dirfd = dup(rootfd);
    if (dirfd < 0) {
        fatal("cannot duplicate destination descriptor: %s", strerror(errno));
    }
    char *next;
    while ((next = strtok_r(NULL, "/", &save)) != NULL) {
        int child = openat(dirfd, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (child < 0 && errno == ENOENT) {
            if (mkdirat(dirfd, component, 0700) != 0 && errno != EEXIST) {
                fatal("cannot create directory %s: %s", component, strerror(errno));
            }
            child = openat(dirfd, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        }
        if (child < 0) {
            fatal("cannot open destination directory %s: %s", component, strerror(errno));
        }
        close(dirfd);
        dirfd = child;
        component = next;
    }
    *leaf = xstrdup(component);
    free(copy);
    return dirfd;
}

static int create_restore_temp(int dirfd, char **name) {
    for (unsigned attempt = 0; attempt < 10000; attempt++) {
        char *candidate = xasprintf(".restore-%ld-%u", (long)getpid(), attempt);
        int fd = openat(dirfd, candidate,
                        O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (fd >= 0) {
            *name = candidate;
            return fd;
        }
        int error = errno;
        free(candidate);
        if (error != EEXIST) {
            errno = error;
            return -1;
        }
    }
    errno = EEXIST;
    return -1;
}

static int open_restore_object(const char *sha256, uint64_t size, struct stat *info) {
    if (!valid_sha256(sha256)) {
        fatal("invalid object checksum in entries");
    }
    char shard[3] = {sha256[0], sha256[1], '\0'};
    int shard_fd = openat(restore_objects_fd, shard,
                          O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (shard_fd < 0) {
        fatal("cannot open object shard %s: %s", shard, strerror(errno));
    }
    int fd = openat(shard_fd, sha256, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    int error = errno;
    close(shard_fd);
    if (fd < 0) {
        fatal("cannot open object %s: %s", sha256, strerror(error));
    }
    if (fstat(fd, info) != 0 || !S_ISREG(info->st_mode) ||
        (uint64_t)info->st_size != size) {
        close(fd);
        fatal("object %s has the wrong size or type", sha256);
    }
    return fd;
}

static void copy_and_hash(int source_fd, int target_fd, uint64_t size,
                          const Segments *segments, bool sparse, char sha256[65]) {
    Hasher hasher;
    hasher_init(&hasher);
    unsigned char *buffer = xmalloc(IO_BUFFER);
    if (sparse) {
        uint64_t current = 0;
        for (size_t index = 0; index < segments->count; index++) {
            Segment segment = segments->items[index];
            if (segment.offset > current) {
                hash_zeros(&hasher, segment.offset - current);
            }
            uint64_t offset = segment.offset;
            uint64_t remaining = segment.length;
            while (remaining) {
                size_t chunk = remaining > IO_BUFFER ? IO_BUFFER : (size_t)remaining;
                ssize_t got = pread(source_fd, buffer, chunk, (off_t)offset);
                if (got <= 0) {
                    fatal("short read from object: %s", strerror(got < 0 ? errno : EIO));
                }
                if (write_all(target_fd, buffer, (size_t)got, (off_t)offset, true) != 0) {
                    fatal("cannot write restored object: %s", strerror(errno));
                }
                hasher_update(&hasher, buffer, (size_t)got);
                offset += (uint64_t)got;
                remaining -= (uint64_t)got;
            }
            current = segment.offset + segment.length;
        }
        if (current < size) {
            hash_zeros(&hasher, size - current);
        }
        if (ftruncate(target_fd, (off_t)size) != 0) {
            fatal("cannot size restored object: %s", strerror(errno));
        }
    } else {
        if (lseek(source_fd, 0, SEEK_SET) < 0) {
            fatal("cannot seek object: %s", strerror(errno));
        }
        uint64_t total = 0;
        while (total < size) {
            size_t chunk = size - total > IO_BUFFER ? IO_BUFFER : (size_t)(size - total);
            ssize_t got = read(source_fd, buffer, chunk);
            if (got <= 0) {
                fatal("short read from object: %s", strerror(got < 0 ? errno : EIO));
            }
            if (write_all(target_fd, buffer, (size_t)got, (off_t)total, true) != 0) {
                fatal("cannot write restored object: %s", strerror(errno));
            }
            hasher_update(&hasher, buffer, (size_t)got);
            total += (uint64_t)got;
        }
        if (ftruncate(target_fd, (off_t)size) != 0) {
            fatal("cannot size restored object: %s", strerror(errno));
        }
    }
    free(buffer);
    hasher_final(&hasher, sha256);
}

static struct timespec *entry_times(const RestoreEntry *entry, struct timespec times[2]) {
    int64_t nanoseconds = entry->mtime_ns;
    time_t seconds = (time_t)(nanoseconds / 1000000000LL);
    long remainder = (long)(nanoseconds % 1000000000LL);
    if (remainder < 0) {
        seconds--;
        remainder += 1000000000L;
    }
    times[0].tv_sec = seconds;
    times[0].tv_nsec = remainder;
    times[1] = times[0];
    return times;
}

static void restore_owner_if_requested(int dirfd, const char *name, int flags,
                                       const RestoreEntry *entry) {
    if (!restore_options.preserve_owner) {
        return;
    }
    if (fchownat(dirfd, name, (uid_t)entry->uid, (gid_t)entry->gid, flags) != 0) {
        fatal("cannot restore owner for %s: %s", entry->path, strerror(errno));
    }
}

static void apply_xattrs_fd_list(int fd, const Xattrs *xattrs, const char *path) {
    if (restore_options.ignore_xattrs) {
        return;
    }
    for (size_t index = 0; index < xattrs->count; index++) {
        if (fsetxattr(fd, xattrs->items[index].name,
                      xattrs->items[index].value,
                      xattrs->items[index].length, 0) != 0) {
            fatal("cannot restore xattr %s on %s: %s",
                  xattrs->items[index].name, path, strerror(errno));
        }
    }
}

static void apply_xattrs_directory(int dirfd, const char *name,
                                   const Xattrs *xattrs, const char *path) {
    if (restore_options.ignore_xattrs || xattrs->count == 0) {
        return;
    }
    int fd = openat(dirfd, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) {
        fatal("cannot open directory for xattrs %s: %s", path, strerror(errno));
    }
    apply_xattrs_fd_list(fd, xattrs, path);
    close(fd);
}

static void restore_file(int parent_fd, const char *name, const RestoreEntry *entry) {
    struct stat object_info;
    int object_fd = open_restore_object(entry->sha256, entry->size, &object_info);
    char *temporary = NULL;
    int temporary_fd = create_restore_temp(parent_fd, &temporary);
    if (temporary_fd < 0) {
        close(object_fd);
        fatal("cannot create restore temporary file: %s", strerror(errno));
    }
    Segments segments = {0};
    bool sparse = false;
    if (entry->size > 0) {
        if (collect_sparse(object_fd, entry->size, &segments) == 0) {
            if (!(segments.count == 1 &&
                  segments.items[0].offset == 0 &&
                  segments.items[0].length == entry->size)) {
                sparse = true;
            }
        } else if (errno != EINVAL && errno != ENOTSUP) {
            int error = errno;
            close(object_fd);
            close(temporary_fd);
            unlinkat(parent_fd, temporary, 0);
            free(temporary);
            free_segments(&segments);
            fatal("cannot map sparse object %s: %s", entry->sha256, strerror(error));
        }
    }
    char sha256[65];
    copy_and_hash(object_fd, temporary_fd, entry->size, &segments, sparse, sha256);
    close(object_fd);
    free_segments(&segments);
    if (strcmp(sha256, entry->sha256) != 0) {
        close(temporary_fd);
        unlinkat(parent_fd, temporary, 0);
        free(temporary);
        fatal("restored object checksum mismatch: %s", entry->path);
    }
    if (fchmod(temporary_fd, entry->mode & 07777) != 0) {
        fatal("cannot set mode for %s: %s", entry->path, strerror(errno));
    }
    if (restore_options.preserve_owner &&
        fchown(temporary_fd, (uid_t)entry->uid, (gid_t)entry->gid) != 0) {
        fatal("cannot set owner for %s: %s", entry->path, strerror(errno));
    }
    struct timespec times[2];
    entry_times(entry, times);
    if (futimens(temporary_fd, times) != 0) {
        fatal("cannot set time for %s: %s", entry->path, strerror(errno));
    }
    apply_xattrs_fd_list(temporary_fd, &entry->xattrs, entry->path);
    if (fsync(temporary_fd) != 0) {
        fatal("cannot fsync %s: %s", entry->path, strerror(errno));
    }
    close(temporary_fd);
    if (linkat(parent_fd, temporary, parent_fd, name, 0) != 0) {
        int error = errno;
        unlinkat(parent_fd, temporary, 0);
        free(temporary);
        fatal("cannot publish restored file %s: %s", entry->path, strerror(error));
    }
    unlinkat(parent_fd, temporary, 0);
    free(temporary);
    if (fsync(parent_fd) != 0) {
        fatal("cannot fsync restored directory for %s: %s", entry->path, strerror(errno));
    }
    restore_files++;
    restore_bytes += entry->size;
}

static void restore_symlink(int parent_fd, const char *name, const RestoreEntry *entry) {
    if (symlinkat(entry->target, parent_fd, name) != 0) {
        fatal("cannot create symlink %s: %s", entry->path, strerror(errno));
    }
    restore_owner_if_requested(parent_fd, name, AT_SYMLINK_NOFOLLOW, entry);
    struct timespec times[2];
    entry_times(entry, times);
    if (utimensat(parent_fd, name, times, AT_SYMLINK_NOFOLLOW) != 0) {
        fatal("cannot set symlink time for %s: %s", entry->path, strerror(errno));
    }
    restore_symlinks++;
}

static void restore_directory(int parent_fd, const char *name, const RestoreEntry *entry) {
    if (mkdirat(parent_fd, name, 0700) != 0 && errno != EEXIST) {
        fatal("cannot create directory %s: %s", entry->path, strerror(errno));
    }
    struct stat info;
    if (fstatat(parent_fd, name, &info, AT_SYMLINK_NOFOLLOW) != 0 ||
        S_ISLNK(info.st_mode) || !S_ISDIR(info.st_mode)) {
        fatal("destination is not a real directory: %s", entry->path);
    }
    if (pending_dir_count == pending_dir_capacity) {
        pending_dir_capacity = pending_dir_capacity ? pending_dir_capacity * 2 : 32;
        pending_dirs = xrealloc(pending_dirs,
                                pending_dir_capacity * sizeof(*pending_dirs));
    }
    PendingDir *pending = &pending_dirs[pending_dir_count++];
    pending->path = xstrdup(entry->path);
    pending->mode = entry->mode & 07777;
    pending->uid = entry->uid;
    pending->gid = entry->gid;
    pending->mtime_ns = entry->mtime_ns;
    memset(&pending->xattrs, 0, sizeof(pending->xattrs));
    xattrs_copy(&pending->xattrs, &entry->xattrs);
    restore_dirs++;
}

static void apply_pending_directories(void) {
    for (size_t index = pending_dir_count; index > 0; index--) {
        PendingDir *pending = &pending_dirs[index - 1];
        char *leaf = NULL;
        int parent_fd = open_parent_dir_at(restore_destination_fd, pending->path, &leaf);
        apply_xattrs_directory(parent_fd, leaf, &pending->xattrs, pending->path);
        if (fchmodat(parent_fd, leaf, pending->mode, 0) != 0) {
            fatal("cannot set directory mode for %s: %s",
                  pending->path, strerror(errno));
        }
        if (restore_options.preserve_owner &&
            fchownat(parent_fd, leaf, (uid_t)pending->uid, (gid_t)pending->gid, 0) != 0) {
            fatal("cannot set directory owner for %s: %s",
                  pending->path, strerror(errno));
        }
        struct timespec times[2];
        int64_t nanoseconds = pending->mtime_ns;
        time_t seconds = (time_t)(nanoseconds / 1000000000LL);
        long remainder = (long)(nanoseconds % 1000000000LL);
        if (remainder < 0) {
            seconds--;
            remainder += 1000000000L;
        }
        times[0].tv_sec = seconds;
        times[0].tv_nsec = remainder;
        times[1] = times[0];
        if (utimensat(parent_fd, leaf, times, 0) != 0) {
            fatal("cannot set directory time for %s: %s",
                  pending->path, strerror(errno));
        }
        close(parent_fd);
        free(leaf);
        free(pending->path);
        xattr_free(&pending->xattrs);
    }
    pending_dir_count = 0;
}

static void restore_one(const RestoreEntry *entry) {
    char *leaf = NULL;
    int parent_fd = open_parent_dir_at(restore_destination_fd, entry->path, &leaf);
    if (strcmp(entry->type, "file") == 0) {
        restore_file(parent_fd, leaf, entry);
    } else if (strcmp(entry->type, "dir") == 0) {
        restore_directory(parent_fd, leaf, entry);
    } else if (strcmp(entry->type, "symlink") == 0) {
        restore_symlink(parent_fd, leaf, entry);
    } else if (strcmp(entry->type, "hardlink") == 0) {
        if (linkat(restore_destination_fd, entry->hardlink_to, parent_fd, leaf, 0) != 0) {
            fatal("cannot create hardlink %s -> %s: %s",
                  entry->path, entry->hardlink_to, strerror(errno));
        }
        restore_hardlinks++;
    } else {
        restore_special++;
    }
    if (strcmp(entry->type, "hardlink") == 0 ||
        strcmp(entry->type, "symlink") == 0 ||
        strcmp(entry->type, "dir") == 0) {
        if (fsync(parent_fd) != 0) {
            fatal("cannot fsync restored directory for %s: %s",
                  entry->path, strerror(errno));
        }
    }
    close(parent_fd);
    free(leaf);
    restore_entries++;
}

static void restore_usage(void) {
    fprintf(stderr,
            "Usage: ubackup-core restore --entries FILE --objects DIR --destination DIR [options]\n"
            "  --report FILE             JSON report path (or - for stdout)\n"
            "  --preserve-owner          Restore original uid/gid (requires root)\n"
            "  --help                    Show this help\n");
    exit(2);
}

static int open_restore_destination(const char *path) {
    struct stat info;
    if (lstat(path, &info) == 0) {
        if (S_ISLNK(info.st_mode) || !S_ISDIR(info.st_mode)) {
            fatal("restore destination is not a real directory: %s", path);
        }
        DIR *directory = opendir(path);
        if (!directory) {
            fatal("cannot open restore destination %s: %s", path, strerror(errno));
        }
        int count = 0;
        struct dirent *item;
        while ((item = readdir(directory)) != NULL) {
            if (strcmp(item->d_name, ".") != 0 && strcmp(item->d_name, "..") != 0) {
                count++;
            }
        }
        closedir(directory);
        if (count) {
            fatal("restore destination is not empty: %s", path);
        }
    } else if (errno == ENOENT) {
        char *parent = parent_dir(path);
        mkdir_p(parent, 0700);
        free(parent);
        if (mkdir(path, 0700) != 0 && errno != EEXIST) {
            fatal("cannot create restore destination %s: %s", path, strerror(errno));
        }
    } else {
        fatal("cannot inspect restore destination %s: %s", path, strerror(errno));
    }
    int fd = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) {
        fatal("cannot open restore destination %s: %s", path, strerror(errno));
    }
    return fd;
}

static void write_restore_report(void) {
    if (!restore_options.report_path) {
        return;
    }
    FILE *output = NULL;
    char *temporary = NULL;
    int fd = -1;
    if (strcmp(restore_options.report_path, "-") == 0) {
        output = stdout;
    } else {
        char *parent = parent_dir(restore_options.report_path);
        mkdir_p(parent, 0700);
        free(parent);
        temporary = xasprintf("%s.tmp-XXXXXX", restore_options.report_path);
        fd = mkstemp(temporary);
        if (fd < 0) {
            fatal("cannot create restore report %s: %s", temporary, strerror(errno));
        }
        if (fchmod(fd, 0600) != 0) {
            fatal("cannot protect restore report %s: %s", temporary, strerror(errno));
        }
        maybe_chown_fd(fd);
        output = fdopen(fd, "w");
        if (!output) {
            fatal("cannot open restore report stream: %s", strerror(errno));
        }
    }
    fprintf(output,
            "{\"format\":\"ubackup-native-restore-v1\",\"destination\":");
    json_string(output, restore_options.destination);
    fprintf(output,
            ",\"entries\":%" PRIu64 ",\"files\":%" PRIu64 ",\"dirs\":%" PRIu64
            ",\"symlinks\":%" PRIu64 ",\"hardlinks\":%" PRIu64
            ",\"special_skipped\":%" PRIu64 ",\"bytes\":%" PRIu64
            ",\"complete\":true}\n",
            restore_entries, restore_files, restore_dirs, restore_symlinks,
            restore_hardlinks, restore_special, restore_bytes);
    if (output == stdout) {
        if (fflush(output) != 0) {
            fatal("cannot flush restore report: %s", strerror(errno));
        }
        return;
    }
    if (fflush(output) != 0 || fsync(fd) != 0 || fclose(output) != 0) {
        fatal("cannot finalize restore report %s: %s", temporary, strerror(errno));
    }
    if (rename(temporary, restore_options.report_path) != 0) {
        fatal("cannot publish restore report %s: %s",
              restore_options.report_path, strerror(errno));
    }
    free(temporary);
}

static int restore_main(int argc, char **argv) {
    static const struct option long_options[] = {
        {"entries", required_argument, NULL, 'e'},
        {"objects", required_argument, NULL, 'o'},
        {"destination", required_argument, NULL, 'd'},
        {"report", required_argument, NULL, 'r'},
        {"preserve-owner", no_argument, NULL, 'p'},
        {"ignore-xattrs", no_argument, NULL, 'x'},
        {"help", no_argument, NULL, 'h'},
        {NULL, 0, NULL, 0},
    };
    optind = 2;
    int option;
    while ((option = getopt_long(argc, argv, "", long_options, NULL)) != -1) {
        switch (option) {
        case 'e': restore_options.entries_path = optarg; break;
        case 'o': restore_options.objects = optarg; break;
        case 'd': restore_options.destination = optarg; break;
        case 'r': restore_options.report_path = optarg; break;
        case 'p': restore_options.preserve_owner = true; break;
        case 'x': restore_options.ignore_xattrs = true; break;
        case 'h': restore_usage(); break;
        default: restore_usage();
        }
    }
    if (optind != argc || !restore_options.entries_path ||
        !restore_options.objects || !restore_options.destination) {
        restore_usage();
    }
    if (restore_options.preserve_owner && geteuid() != 0) {
        fatal("--preserve-owner requires root");
    }
    umask(0);
    restore_objects_fd = open(restore_options.objects,
                              O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (restore_objects_fd < 0) {
        fatal("cannot open object store %s: %s", restore_options.objects, strerror(errno));
    }
    restore_destination_fd = open_restore_destination(restore_options.destination);
    FILE *input = fopen(restore_options.entries_path, "r");
    if (!input) {
        fatal("cannot open entries file %s: %s",
              restore_options.entries_path, strerror(errno));
    }
    char *line = NULL;
    size_t capacity = 0;
    size_t line_number = 0;
    ssize_t length;
    while ((length = getline(&line, &capacity, input)) >= 0) {
        line_number++;
        char *content = line;
        while (isspace((unsigned char)*content)) {
            content++;
        }
        if (!*content) {
            continue;
        }
        RestoreEntry entry;
        parse_restore_entry(content, line_number, &entry);
        restore_one(&entry);
        restore_entry_free(&entry);
    }
    if (ferror(input)) {
        fatal("cannot read entries file %s: %s",
              restore_options.entries_path, strerror(errno));
    }
    free(line);
    fclose(input);
    apply_pending_directories();
    write_restore_report();
    close(restore_objects_fd);
    close(restore_destination_fd);
    return 0;
}

static void append_text(char **buffer, size_t *length, size_t *capacity, const char *text) {
    size_t addition = strlen(text);
    if (*length + addition + 1 > *capacity) {
        while (*length + addition + 1 > *capacity) {
            *capacity *= 2;
        }
        *buffer = xrealloc(*buffer, *capacity);
    }
    memcpy(*buffer + *length, text, addition);
    *length += addition;
    (*buffer)[*length] = '\0';
}

static char *relative_path(const char *from_dir, const char *to_path) {
    char *from_copy = xstrdup(from_dir);
    char *to_copy = xstrdup(to_path);
    char *from_parts[512];
    char *to_parts[512];
    size_t from_count = 0;
    size_t to_count = 0;
    char *save = NULL;
    for (char *part = strtok_r(from_copy, "/", &save); part && from_count < 512;
         part = strtok_r(NULL, "/", &save)) {
        from_parts[from_count++] = part;
    }
    save = NULL;
    for (char *part = strtok_r(to_copy, "/", &save); part && to_count < 512;
         part = strtok_r(NULL, "/", &save)) {
        to_parts[to_count++] = part;
    }
    size_t common = 0;
    while (common < from_count && common < to_count &&
           strcmp(from_parts[common], to_parts[common]) == 0) {
        common++;
    }
    size_t capacity = 128;
    size_t length = 0;
    char *result = xmalloc(capacity);
    result[0] = '\0';
    for (size_t index = common; index < from_count; index++) {
        append_text(&result, &length, &capacity, "../");
    }
    for (size_t index = common; index < to_count; index++) {
        append_text(&result, &length, &capacity, to_parts[index]);
        if (index + 1 < to_count) {
            append_text(&result, &length, &capacity, "/");
        }
    }
    if (length == 0) {
        append_text(&result, &length, &capacity, ".");
    }
    free(from_copy);
    free(to_copy);
    return result;
}

static char *utc_run_id(void) {
    struct timespec now;
    struct tm broken;
    if (clock_gettime(CLOCK_REALTIME, &now) != 0) {
        fatal("cannot read clock: %s", strerror(errno));
    }
    gmtime_r(&now.tv_sec, &broken);
    char *value = xmalloc(64);
    snprintf(value, 64, "%04d%02d%02dT%02d%02d%02d.%06dZ",
             broken.tm_year + 1900, broken.tm_mon + 1, broken.tm_mday,
             broken.tm_hour, broken.tm_min, broken.tm_sec,
             (int)(now.tv_nsec / 1000));
    return value;
}

static char *utc_iso(void) {
    struct timespec now;
    struct tm broken;
    if (clock_gettime(CLOCK_REALTIME, &now) != 0) {
        fatal("cannot read clock: %s", strerror(errno));
    }
    gmtime_r(&now.tv_sec, &broken);
    char *value = xmalloc(64);
    snprintf(value, 64, "%04d-%02d-%02dT%02d:%02d:%02d.%06d+00:00",
             broken.tm_year + 1900, broken.tm_mon + 1, broken.tm_mday,
             broken.tm_hour, broken.tm_min, broken.tm_sec,
             (int)(now.tv_nsec / 1000));
    return value;
}

static bool valid_run_id(const char *value) {
    if (!value || strlen(value) != 23 ||
        value[8] != 'T' || value[15] != '.' || value[22] != 'Z') {
        return false;
    }
    for (size_t index = 0; index < 23; index++) {
        if (index == 8 || index == 15 || index == 22) continue;
        if (!isdigit((unsigned char)value[index])) return false;
    }
    return true;
}

static char *hash_file_sha256(const char *path) {
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) {
        fatal("cannot open %s for hashing: %s", path, strerror(errno));
    }
    Hasher hasher;
    hasher_init(&hasher);
    unsigned char *buffer = xmalloc(IO_BUFFER);
    ssize_t received;
    while ((received = read(fd, buffer, IO_BUFFER)) > 0) {
        hasher_update(&hasher, buffer, (size_t)received);
    }
    int error = errno;
    free(buffer);
    close(fd);
    if (received < 0) {
        hasher_abort(&hasher);
        fatal("cannot read %s for hashing: %s", path, strerror(error));
    }
    char sha256[65];
    hasher_final(&hasher, sha256);
    return xstrdup(sha256);
}

static void copy_file_path(const char *source, const char *destination) {
    int input = open(source, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (input < 0) {
        fatal("cannot open %s: %s", source, strerror(errno));
    }
    int output = open(destination, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (output < 0) {
        int error = errno;
        close(input);
        fatal("cannot create %s: %s", destination, strerror(error));
    }
    unsigned char *buffer = xmalloc(IO_BUFFER);
    ssize_t received;
    while ((received = read(input, buffer, IO_BUFFER)) > 0) {
        if (write_all(output, buffer, (size_t)received, 0, false) != 0) {
            int error = errno;
            free(buffer);
            close(input);
            close(output);
            unlink(destination);
            fatal("cannot write %s: %s", destination, strerror(error));
        }
    }
    int error = errno;
    free(buffer);
    close(input);
    if (received < 0) {
        close(output);
        unlink(destination);
        fatal("cannot read %s: %s", source, strerror(error));
    }
    if (fsync(output) != 0 || close(output) != 0) {
        unlink(destination);
        fatal("cannot finalize %s: %s", destination, strerror(errno));
    }
    maybe_chown_path(destination);
}

static int chown_tree_callback(const char *path, const struct stat *info, int flag,
                               struct FTW *walk) {
    (void)info;
    (void)flag;
    (void)walk;
    if (lchown(path, (uid_t)options.owner_uid, (gid_t)options.owner_gid) != 0) {
        return -1;
    }
    return 0;
}

static void maybe_chown_tree(const char *root) {
    if (!options.have_owner) {
        return;
    }
    if (nftw(root, chown_tree_callback, 64, FTW_PHYS) != 0) {
        fatal("cannot chown restore tree %s: %s", root, strerror(errno));
    }
}

static void publish_timeline(void) {
    if (!options.timeline) {
        return;
    }
    char *run_id = options.run_id ? xstrdup(options.run_id) : utc_run_id();
    if (!valid_run_id(run_id)) {
        fatal("invalid timeline run id: %s", run_id);
    }
    mkdir_p(options.timeline, 0700);
    maybe_chown_path(options.timeline);
    char *final = xasprintf("%s/home-full-%s", options.timeline, run_id);
    struct stat info;
    if (lstat(final, &info) == 0) {
        fatal("full /home timeline already exists: %s", final);
    }
    if (errno != ENOENT) {
        fatal("cannot inspect timeline destination %s: %s", final, strerror(errno));
    }
    char *template = xasprintf("%s/.home-timeline-XXXXXX", options.timeline);
    if (!mkdtemp(template)) {
        fatal("cannot create timeline staging directory: %s", strerror(errno));
    }
    char *tree = xasprintf("%s/tree", template);
    ensure_dir(tree, 0700);
    int tree_fd = open(tree, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (tree_fd < 0) {
        fatal("cannot open timeline tree %s: %s", tree, strerror(errno));
    }
    char *objects_absolute = realpath(options.objects, NULL);
    char *tree_absolute = realpath(tree, NULL);
    if (!objects_absolute || !tree_absolute) {
        fatal("cannot resolve timeline/object paths: %s", strerror(errno));
    }

    FILE *input = fopen(options.entries_path, "r");
    if (!input) {
        fatal("cannot open entries file %s: %s", options.entries_path, strerror(errno));
    }
    char *line = NULL;
    size_t capacity = 0;
    size_t line_number = 0;
    ssize_t length;
    uint64_t timeline_entries = 0;
    uint64_t timeline_bytes = 0;
    while ((length = getline(&line, &capacity, input)) >= 0) {
        line_number++;
        char *content = line;
        while (isspace((unsigned char)*content)) {
            content++;
        }
        if (!*content) {
            continue;
        }
        RestoreEntry entry;
        parse_restore_entry(content, line_number, &entry);
        char *leaf = NULL;
        int parent_fd = open_parent_dir_at(tree_fd, entry.path, &leaf);
        if (strcmp(entry.type, "dir") == 0) {
            if (mkdirat(parent_fd, leaf, 0700) != 0 && errno != EEXIST) {
                fatal("cannot create timeline directory %s: %s",
                      entry.path, strerror(errno));
            }
        } else if (strcmp(entry.type, "file") == 0) {
            char *object = xasprintf("%s/%c%c/%s", objects_absolute,
                                     entry.sha256[0], entry.sha256[1], entry.sha256);
            char *parent_path = xasprintf("%s/%s", tree_absolute, entry.path);
            char *parent_directory = parent_dir(parent_path);
            char *target = relative_path(parent_directory, object);
            if (symlinkat(target, parent_fd, leaf) != 0) {
                fatal("cannot create timeline link %s: %s", entry.path, strerror(errno));
            }
            free(target);
            free(parent_directory);
            free(parent_path);
            free(object);
            timeline_bytes += entry.size;
        } else if (strcmp(entry.type, "symlink") == 0) {
            if (symlinkat(entry.target, parent_fd, leaf) != 0) {
                fatal("cannot create timeline symlink %s: %s",
                      entry.path, strerror(errno));
            }
        } else if (strcmp(entry.type, "hardlink") == 0) {
            char *link_path = xasprintf("%s/%s", tree_absolute, entry.path);
            char *parent_directory = parent_dir(link_path);
            char *target_path = xasprintf("%s/%s", tree_absolute, entry.hardlink_to);
            char *target = relative_path(parent_directory, target_path);
            if (symlinkat(target, parent_fd, leaf) != 0) {
                fatal("cannot create timeline hardlink %s: %s",
                      entry.path, strerror(errno));
            }
            free(target);
            free(target_path);
            free(parent_directory);
            free(link_path);
        }
        close(parent_fd);
        free(leaf);
        restore_entry_free(&entry);
        timeline_entries++;
    }
    if (ferror(input)) {
        fatal("cannot read entries file %s: %s", options.entries_path, strerror(errno));
    }
    free(line);
    fclose(input);

    char *entries_copy = xasprintf("%s/entries.ndjson", template);
    copy_file_path(options.entries_path, entries_copy);
    char *entries_sha = hash_file_sha256(options.entries_path);
    char *manifest_path = xasprintf("%s/manifest.json", template);
    int manifest_fd = open(manifest_path,
                           O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (manifest_fd < 0) {
        fatal("cannot create timeline manifest %s: %s", manifest_path, strerror(errno));
    }
    maybe_chown_fd(manifest_fd);
    FILE *manifest = fdopen(manifest_fd, "w");
    if (!manifest) {
        fatal("cannot open timeline manifest stream: %s", strerror(errno));
    }
    char *created_at = utc_iso();
    fputs("{\"format\":\"ubuntu-configuration-v1\",\"kind\":\"full-home-linked\","
          "\"storage\":\"ubuntu-linked-symlink-v1\",\"created_at\":", manifest);
    json_string(manifest, created_at);
    fputs(",\"source\":", manifest);
    json_string(manifest, options.source);
    fputs(",\"identity\":", manifest);
    fputs(options.identity_json ? options.identity_json : "{}", manifest);
    fputs(",\"exclusions\":", manifest);
    fputs(options.exclusions_json ? options.exclusions_json : "[]", manifest);
    fprintf(manifest,
            ",\"entry_count\":%" PRIu64 ",\"logical_bytes\":%" PRIu64
            ",\"warnings\":[", timeline_entries, timeline_bytes);
    size_t warning_limit = omission_count > 200 ? 200 : omission_count;
    for (size_t index = 0; index < warning_limit; index++) {
        if (index) fputc(',', manifest);
        fputs("{\"path\":", manifest);
        json_string(manifest, omissions[index].path);
        fputs(",\"operation\":", manifest);
        json_string(manifest, omissions[index].op);
        fprintf(manifest, ",\"errno\":%d,\"message\":", omissions[index].error);
        json_string(manifest, strerror(omissions[index].error));
        fputc('}', manifest);
    }
    fprintf(manifest, "],\"warning_count\":%" PRIu64
            ",\"entries_sha256\":", (uint64_t)omission_count);
    json_string(manifest, entries_sha);
    fputs(",\"restore_policy\":\"Stage privately. Never overwrite live /home or replay UID/hardware bindings blindly.\"}\n",
          manifest);
    if (fflush(manifest) != 0 || fsync(manifest_fd) != 0 || fclose(manifest) != 0) {
        fatal("cannot finalize timeline manifest: %s", strerror(errno));
    }
    free(created_at);
    free(entries_sha);
    free(entries_copy);
    free(manifest_path);

    maybe_chown_tree(tree);
    close(tree_fd);
    maybe_chown_path(template);
    if (rename(template, final) != 0) {
        fatal("cannot publish timeline %s: %s", final, strerror(errno));
    }
    maybe_chown_path(final);
    fsync_directory(options.timeline);
    published_timeline = final;
    free(objects_absolute);
    free(tree_absolute);
    free(tree);
    free(template);
    free(run_id);
}

static int remove_tree_callback(const char *path, const struct stat *info,
                                int flag, struct FTW *walk) {
    (void)info;
    (void)flag;
    (void)walk;
    if (remove(path) != 0) {
        return -1;
    }
    return 0;
}

static void remove_tree(const char *path) {
    struct stat info;
    if (lstat(path, &info) != 0) {
        if (errno == ENOENT) return;
        fatal("cannot inspect %s: %s", path, strerror(errno));
    }
    if (nftw(path, remove_tree_callback, 64, FTW_DEPTH | FTW_PHYS) != 0) {
        fatal("cannot remove %s: %s", path, strerror(errno));
    }
}

static void copy_sparse_file_c(int source_fd, int target_fd, uint64_t size) {
    Segments segments = {0};
    bool sparse = false;
    if (size > 0) {
        if (collect_sparse(source_fd, size, &segments) == 0) {
            if (!(segments.count == 1 && segments.items[0].offset == 0 &&
                  segments.items[0].length == size)) {
                sparse = true;
            }
        } else if (errno != EINVAL && errno != ENOTSUP) {
            int error = errno;
            free_segments(&segments);
            fatal("cannot map staged object: %s", strerror(error));
        }
    }
    unsigned char *buffer = xmalloc(IO_BUFFER);
    if (sparse) {
        for (size_t index = 0; index < segments.count; index++) {
            uint64_t offset = segments.items[index].offset;
            uint64_t remaining = segments.items[index].length;
            while (remaining) {
                size_t chunk = remaining > IO_BUFFER ? IO_BUFFER : (size_t)remaining;
                ssize_t got = pread(source_fd, buffer, chunk, (off_t)offset);
                if (got <= 0) {
                    free(buffer);
                    free_segments(&segments);
                    fatal("short read from staged object: %s",
                          strerror(got < 0 ? errno : EIO));
                }
                if (write_all(target_fd, buffer, (size_t)got, (off_t)offset, true) != 0) {
                    free(buffer);
                    free_segments(&segments);
                    fatal("cannot copy staged object: %s", strerror(errno));
                }
                offset += (uint64_t)got;
                remaining -= (uint64_t)got;
            }
        }
        if (ftruncate(target_fd, (off_t)size) != 0) {
            free(buffer);
            free_segments(&segments);
            fatal("cannot size copied object: %s", strerror(errno));
        }
    } else {
        if (lseek(source_fd, 0, SEEK_SET) < 0) {
            free(buffer);
            fatal("cannot seek staged object: %s", strerror(errno));
        }
        uint64_t total = 0;
        while (total < size) {
            size_t chunk = size - total > IO_BUFFER ? IO_BUFFER : (size_t)(size - total);
            ssize_t got = read(source_fd, buffer, chunk);
            if (got <= 0) {
                free(buffer);
                fatal("short read from staged object: %s",
                      strerror(got < 0 ? errno : EIO));
            }
            if (write_all(target_fd, buffer, (size_t)got, (off_t)total, true) != 0) {
                free(buffer);
                fatal("cannot copy staged object: %s", strerror(errno));
            }
            total += (uint64_t)got;
        }
        if (ftruncate(target_fd, (off_t)size) != 0) {
            free(buffer);
            fatal("cannot size copied object: %s", strerror(errno));
        }
    }
    free(buffer);
    free_segments(&segments);
}

static void bulk_copy_stage(void) {
    if (!options.stage || strcmp(options.stage, options.objects) == 0) {
        return;
    }
    char *stage_objects = xstrdup(options.stage);
    DIR *stage_directory = opendir(stage_objects);
    if (!stage_directory) {
        fatal("cannot open staged object store %s: %s",
              stage_objects, strerror(errno));
    }
    struct dirent *shard_entry;
    while ((shard_entry = readdir(stage_directory)) != NULL) {
        if (strcmp(shard_entry->d_name, ".") == 0 ||
            strcmp(shard_entry->d_name, "..") == 0 ||
            strcmp(shard_entry->d_name, INCOMING) == 0) {
            continue;
        }
        if (strlen(shard_entry->d_name) != 2) {
            continue;
        }
        char *stage_shard = xasprintf("%s/%s", stage_objects, shard_entry->d_name);
        DIR *shard_directory = opendir(stage_shard);
        if (!shard_directory) {
            fatal("cannot open staged shard %s: %s", stage_shard, strerror(errno));
        }
        struct dirent *object_entry;
        while ((object_entry = readdir(shard_directory)) != NULL) {
            if (strcmp(object_entry->d_name, ".") == 0 ||
                strcmp(object_entry->d_name, "..") == 0) {
                continue;
            }
            if (!valid_sha256(object_entry->d_name)) {
                fatal("unexpected staged object name: %s", object_entry->d_name);
            }
            char *source = xasprintf("%s/%s", stage_shard, object_entry->d_name);
            struct stat source_info;
            if (lstat(source, &source_info) != 0 || !S_ISREG(source_info.st_mode) ||
                S_ISLNK(source_info.st_mode)) {
                fatal("unexpected staged object: %s", source);
            }
            char *target_shard = xasprintf("%s/%s", options.objects, shard_entry->d_name);
            char *target = xasprintf("%s/%s", target_shard, object_entry->d_name);
            struct stat target_info;
            if (lstat(target, &target_info) == 0) {
                if (!S_ISREG(target_info.st_mode) || S_ISLNK(target_info.st_mode) ||
                    target_info.st_size != source_info.st_size) {
                    fatal("existing object has the wrong size or type: %s", target);
                }
                free(target);
                free(target_shard);
                free(source);
                continue;
            }
            if (errno != ENOENT) {
                fatal("cannot inspect target object %s: %s", target, strerror(errno));
            }
            ensure_dir(target_shard, 0700);
            char *template = xasprintf("%s/.copy-XXXXXX", target_shard);
            int target_fd = mkstemp(template);
            if (target_fd < 0) {
                fatal("cannot create copy temporary in %s: %s",
                      target_shard, strerror(errno));
            }
            int source_fd = open(source, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
            if (source_fd < 0) {
                int error = errno;
                close(target_fd);
                unlink(template);
                fatal("cannot open staged object %s: %s", source, strerror(error));
            }
            copy_sparse_file_c(source_fd, target_fd, (uint64_t)source_info.st_size);
            close(source_fd);
            if (fchmod(target_fd, 0400) != 0) {
                fatal("cannot protect copied object %s: %s", target, strerror(errno));
            }
            close(target_fd);
            if (!publish_with_rename(template, target)) {
                struct stat copied;
                if (lstat(target, &copied) != 0 || !S_ISREG(copied.st_mode) ||
                    S_ISLNK(copied.st_mode) ||
                    copied.st_size != source_info.st_size) {
                    unlink(template);
                    fatal("existing object has the wrong size or type: %s", target);
                }
                unlink(template);
            }
            free(template);
            free(target);
            free(target_shard);
            free(source);
        }
        closedir(shard_directory);
        free(stage_shard);
    }
    closedir(stage_directory);
    free(stage_objects);
}

static void drop_to_owner(void) {
    if (!options.have_owner || geteuid() != 0) {
        return;
    }
    if (options.owner_uid < 0 || options.owner_gid < 0) {
        fatal("staged root capture requires both owner uid and gid");
    }
    if (options.stage) {
        maybe_chown_tree(options.stage);
    }
    if (setgroups(0, NULL) != 0 ||
        setgid((gid_t)options.owner_gid) != 0 ||
        setuid((uid_t)options.owner_uid) != 0) {
        fatal("cannot drop to backup owner: %s", strerror(errno));
    }
    options.have_owner = false;
}

static bool is_hex_component(const char *name) {
    if (strlen(name) != 2) return false;
    return isxdigit((unsigned char)name[0]) && isxdigit((unsigned char)name[1]);
}

static void prepare_stage_directory(const char *stage) {
    struct stat info;
    if (lstat(stage, &info) != 0) {
        if (errno == ENOENT) {
            return;
        }
        fatal("cannot inspect stage %s: %s", stage, strerror(errno));
    }
    if (S_ISLNK(info.st_mode) || !S_ISDIR(info.st_mode)) {
        fatal("stage is not a real directory: %s", stage);
    }
    DIR *directory = opendir(stage);
    if (!directory) {
        fatal("cannot open stage %s: %s", stage, strerror(errno));
    }
    struct dirent *entry;
    while ((entry = readdir(directory)) != NULL) {
        if (strcmp(entry->d_name, ".") == 0 ||
            strcmp(entry->d_name, "..") == 0 ||
            strcmp(entry->d_name, ".incoming") == 0 ||
            strcmp(entry->d_name, "objects") == 0 ||
            strcmp(entry->d_name, "incoming") == 0 ||
            is_hex_component(entry->d_name)) {
            continue;
        }
        closedir(directory);
        fatal("refusing to remove unrecognized stage directory: %s", stage);
    }
    closedir(directory);
    remove_tree(stage);
}

static int scan_main(int argc, char **argv) {
    if (argc < 2 || strcmp(argv[1], "scan") != 0) {
        usage();
    }
    options.owner_uid = -1;
    options.owner_gid = -1;

    static const struct option long_options[] = {
        {"source", required_argument, NULL, 's'},
        {"objects", required_argument, NULL, 'o'},
        {"entries", required_argument, NULL, 'e'},
        {"report", required_argument, NULL, 'r'},
        {"run-id", required_argument, NULL, 'i'},
        {"exclude-prefix", required_argument, NULL, 'p'},
        {"exclude-name", required_argument, NULL, 'n'},
        {"one-file-system", no_argument, NULL, 'x'},
        {"owner-uid", required_argument, NULL, 'u'},
        {"owner-gid", required_argument, NULL, 'g'},
        {"no-xattrs", no_argument, NULL, 'q'},
        {"stage", required_argument, NULL, 'S'},
        {"timeline", required_argument, NULL, 't'},
        {"identity-json", required_argument, NULL, 'd'},
        {"exclusions-json", required_argument, NULL, 'j'},
        {"help", no_argument, NULL, 'h'},
        {NULL, 0, NULL, 0},
    };

    optind = 2;
    int option;
    while ((option = getopt_long(argc, argv, "", long_options, NULL)) != -1) {
        switch (option) {
        case 's': options.source = optarg; break;
        case 'o': options.objects = optarg; break;
        case 'e': options.entries_path = optarg; break;
        case 'r': options.report_path = optarg; break;
        case 'i': options.run_id = optarg; break;
        case 'p': append_string(&options.exclude_prefix, &options.exclude_prefix_count, optarg); break;
        case 'n': append_string(&options.exclude_name, &options.exclude_name_count, optarg); break;
        case 'x': options.one_file_system = true; break;
        case 'u': parse_id(optarg, &options.owner_uid, "owner uid"); break;
        case 'g': parse_id(optarg, &options.owner_gid, "owner gid"); break;
        case 'q': options.no_xattrs = true; break;
        case 'S': options.stage = optarg; break;
        case 't': options.timeline = optarg; break;
        case 'd': options.identity_json = optarg; break;
        case 'j': options.exclusions_json = optarg; break;
        case 'h': usage(); break;
        default: usage();
        }
    }
    if (optind != argc || !options.source || !options.objects || !options.entries_path) {
        usage();
    }
    options.have_owner = options.owner_uid >= 0 || options.owner_gid >= 0;
    if (options.have_owner && geteuid() != 0) {
        fatal("owner options require root");
    }

    for (size_t index = 0; index < options.exclude_prefix_count; index++) {
        char *prefix = options.exclude_prefix[index];
        size_t length = strlen(prefix);
        while (length && prefix[length - 1] == '/') {
            prefix[--length] = '\0';
        }
    }

    source_fd = open(options.source, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (source_fd < 0) {
        fatal("cannot open source %s: %s", options.source, strerror(errno));
    }
    struct stat source_info;
    if (fstat(source_fd, &source_info) != 0 || !S_ISDIR(source_info.st_mode)) {
        fatal("source is not a directory: %s", options.source);
    }
    source_dev = source_info.st_dev;

    if (options.stage && strcmp(options.stage, options.objects) == 0) {
        options.stage = NULL;
    }
    if (options.stage) {
        size_t stage_length = strlen(options.stage);
        if (strcmp(options.stage, "/") == 0 ||
            strcmp(options.stage, options.source) == 0 ||
            strcmp(options.stage, options.objects) == 0 ||
            (strncmp(options.source, options.stage, stage_length) == 0 &&
             options.source[stage_length] == '/')) {
            fatal("stage must not be the source, object store, filesystem root, or a parent of the source");
        }
        prepare_stage_directory(options.stage);
        ensure_object_dir(options.stage);
    }
    ensure_object_dir(options.objects);
    open_entries();
    scan_directory(source_fd, "", 0);
    close_entries();
    if (options.stage) {
        drop_to_owner();
        bulk_copy_stage();
        sync_objects();
        remove_tree(options.stage);
    } else {
        sync_objects();
    }
    publish_timeline();
    write_report();

    for (size_t index = 0; index < omission_count; index++) {
        free(omissions[index].path);
        free(omissions[index].op);
    }
    free(omissions);
    for (size_t index = 0; index < options.exclude_prefix_count; index++) {
        free(options.exclude_prefix[index]);
    }
    free(options.exclude_prefix);
    for (size_t index = 0; index < options.exclude_name_count; index++) {
        free(options.exclude_name[index]);
    }
    free(options.exclude_name);
    return omission_count ? 3 : 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        usage();
    }
    if (strcmp(argv[1], "scan") == 0) {
        return scan_main(argc, argv);
    }
    if (strcmp(argv[1], "restore") == 0) {
        return restore_main(argc, argv);
    }
    usage();
}

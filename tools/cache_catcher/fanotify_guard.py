#!/usr/bin/env python3
"""cache-catcher guard: watch the cache filesystem for (1) chunk DELETIONS/eviction
and (2) PERMISSION changes that make a file unreadable by www-data (the mode-000
issue). Emails on either, via Gmail SMTP, with a per-type cooldown.

fanotify FAN_DELETE|FAN_MOVED_FROM|FAN_ATTRIB on the whole cache fs (FAN_MARK_FILESYSTEM),
FAN_REPORT_DFID_NAME so events carry the parent-dir FID + filename. Deletes log the culprit
(nfsd/nginx/etc). Attrib events by a non-nginx process are stat'd (via open_by_handle_at) and
alerted if the file is no longer www-data-readable.
"""

import ctypes
import os
import re
import smtplib
import ssl
import struct
import sys
import threading
import time
from collections import deque
from email.message import EmailMessage

# #337: tell a commanded purge from an uncommanded eviction. Kept in its own
# stdlib-only module so the decision is testable off-box; this file cannot be
# imported anywhere but the NAS container because of the libc load above.
from delete_actor import ACTOR_ORCHESTRATOR, classify_actor, counts_toward_eviction

# Kuma heartbeats and the keys_zone gauge. Flat imports because in the container
# these all sit together in /log/; in the repo they are a package.
import kuma
from key_budget_probe import load_cfg, probe_loop

libc = ctypes.CDLL("libc.so.6", use_errno=True)

FAN_CLASS_NOTIF = 0x0
FAN_REPORT_DFID_NAME = 0xC00
FAN_DELETE = 0x200
FAN_MOVED_FROM = 0x40
FAN_ATTRIB = 0x4
FAN_ONDIR = 0x40000000
FAN_MARK_ADD = 0x1
FAN_MARK_FILESYSTEM = 0x100
AT_FDCWD = -100
O_RDONLY = 0
O_PATH = 0x200000
FAN_EVENT_INFO_TYPE_DFID_NAME = 2
WWW_UID = 33
WWW_GID = 33

# eviction alert: >= EVICT_THRESHOLD nginx cache-file deletes within EVICT_WINDOW seconds
EVICT_THRESHOLD = 10
# A commanded purge is reported, never alarmed. Same threshold so the operator
# still learns a bulk delete happened, on its own cooldown so it can never
# suppress a real eviction alert (#337).
PURGE_THRESHOLD = 10
EVICT_WINDOW = 60
COOLDOWN = 900  # 15 min per alert type

# TWO Kuma monitors, never one. A single monitor carrying both signals cannot
# distinguish "the guard is dead" from "the cache is being evicted" -- exactly
# the defect #326, #330 and #337 all describe. KUMA_PUSH_CACHE_GUARD answers
# only "is this process running"; KUMA_PUSH_CACHE_EVICTION answers only "is
# cache loss happening now". Pushed from the same thread, never the same value,
# so a liveness tick can never paint over a live eviction.
LIVENESS_INTERVAL_SEC = 900

# Kuma treats silence as DOWN, so the eviction monitor needs a heartbeat of its
# own -- and therefore a latch, or the next heartbeat would flip it green while
# the incident is still running. It clears an hour after the last alert.
EVICT_LATCH_SEC = 3600

# The alert kinds that mean real cache loss. "purge" is a commanded operator
# action and stays an email NOTICE that changes no monitor state -- that was the
# whole point of #337. "external" is the prefill-stall one-shot and is nothing
# to do with the cache guard.
EVICT_ALERT_KINDS = ("eviction", "mode000")

# An nginx cache object under levels=2:2 is named by its full md5 (32 lowercase
# hex chars). ONLY these count toward eviction. Anything else on the same
# watched filesystem -- GOG manifests, installers, scratch files -- is not
# cache loss, and counting it produced the 2026-08-10 false alarm.
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")

# Cache WRITES (FAN_MOVED_FROM) are high-volume and uninteresting one by one.
WRITE_SUMMARY_EVERY = 300  # seconds between write-summary lines
LOG_MAX_BYTES = 64 * 1024 * 1024  # rotate deletions.log at 64 MiB, keep 1 old

libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
libc.fanotify_mark.argtypes = [
    ctypes.c_int,
    ctypes.c_uint,
    ctypes.c_uint64,
    ctypes.c_int,
    ctypes.c_char_p,
]
libc.open_by_handle_at.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
libc.open_by_handle_at.restype = ctypes.c_int

LOG = "/log/deletions.log"
ALERT_ENV = "/log/alert.env"
_last_sent = {}
_evict_ts = deque()
_purge_ts = deque()  # commanded purges, counted separately (#337)
_mount_fd = None


def _rotate_if_needed():
    """Size-based rotation, one generation kept."""
    try:
        if os.path.getsize(LOG) >= LOG_MAX_BYTES:
            os.replace(LOG, LOG + ".1")
    except FileNotFoundError:
        pass
    except OSError:
        pass


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), msg)
    _rotate_if_needed()
    with open(LOG, "a") as f:
        f.write(line + "\n")
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def load_env():
    cfg = {}
    try:
        with open(ALERT_ENV) as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg


def send_email(subject, body):
    cfg = load_env()
    host = cfg.get("SMTP_HOST", "smtp.gmail.com")
    port = int(cfg.get("SMTP_PORT", "587"))
    user = cfg.get("SMTP_USER", "")
    pw = cfg.get("SMTP_PASS", "")
    frm = cfg.get("MAIL_FROM", user)
    to = cfg.get("MAIL_TO", "")
    if not (user and pw and to):
        log("ALERT-EMAIL skipped (SMTP not configured): %s" % subject)
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = frm
        msg["To"] = to
        msg.set_content(body)
        _CA_PERSIST = "/log/ca-certificates.crt"
        ctx = (
            ssl.create_default_context(cafile=_CA_PERSIST)
            if os.path.exists(_CA_PERSIST)
            else ssl.create_default_context()
        )
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.send_message(msg)
        log("ALERT-EMAIL sent: %s" % subject)
    except Exception as e:
        log("ALERT-EMAIL FAILED (%s): %s" % (type(e).__name__, subject))


def alert(kind, subject, body):
    now = time.time()
    last = _last_sent.get(kind, 0)
    if now - last < COOLDOWN:
        log("ALERT-suppressed (cooldown) %s: %s" % (kind, subject))
        return
    _last_sent[kind] = now
    log("ALERT %s: %s" % (kind, subject))
    send_email(subject, body)
    if kind in EVICT_ALERT_KINDS:
        kuma.push(load_cfg().get("KUMA_PUSH_CACHE_EVICTION"), "down", subject)


def proc_info(pid):
    comm = exe = cmd = "?"
    try:
        comm = open("/proc/%d/comm" % pid).read().strip()
    except Exception:
        pass
    try:
        exe = os.readlink("/proc/%d/exe" % pid)
    except Exception:
        pass
    try:
        cmd = open("/proc/%d/cmdline" % pid).read().replace("\0", " ").strip()[:200]
    except Exception:
        pass
    return comm, exe, cmd


def stat_via_handle(info_bytes, name):
    """Best-effort: resolve the parent-dir file_handle and stat name within it."""
    global _mount_fd
    try:
        if _mount_fd is None:
            _mount_fd = os.open("/volume1/cache", O_RDONLY)
        handle_bytes = struct.unpack_from("=I", info_bytes, 8)[0]
        # struct file_handle = handle_bytes(4) + handle_type(4) + f_handle[handle_bytes]
        fh = info_bytes[8 : 8 + 8 + handle_bytes]
        buf = ctypes.create_string_buffer(fh, len(fh))
        dirfd = libc.open_by_handle_at(_mount_fd, buf, O_PATH)
        if dirfd < 0:
            return None
        try:
            st = os.stat(name, dir_fd=dirfd)
            return st
        finally:
            os.close(dirfd)
    except Exception:
        return None


def www_readable(st):
    m = st.st_mode
    if st.st_uid == WWW_UID:
        return bool(m & 0o400)
    if st.st_gid == WWW_GID:
        return bool(m & 0o040)
    return bool(m & 0o004)


def parse_name(info_bytes):
    try:
        if len(info_bytes) < 16:
            return "?"
        handle_bytes = struct.unpack_from("=I", info_bytes, 8)[0]
        name_off = 8 + 4 + 4 + handle_bytes
        raw = info_bytes[name_off:]
        nul = raw.find(b"\0")
        if nul >= 0:
            raw = raw[:nul]
        return raw.decode("utf-8", "replace") or "?"
    except Exception:
        return "?"


META_FMT = "=IBBHQii"
META_SZ = struct.calcsize(META_FMT)


def _is_write_temp(nm):
    i = nm.rfind(".")
    return i > 0 and nm[i + 1 :].isdigit() and len(nm) - i - 1 >= 4


_writes = {"n": 0, "since": time.time()}


def _note_write():
    """Aggregate nginx cache writes into one line per window."""
    _writes["n"] += 1
    now = time.time()
    if now - _writes["since"] >= WRITE_SUMMARY_EVERY:
        log(
            "WRITES %d in %ds (nginx cache writes, MOVED_FROM)"
            % (_writes["n"], int(now - _writes["since"]))
        )
        _writes["n"] = 0
        _writes["since"] = now


def handle_delete(kind, pid, comm, exe, name, cmd):
    # MOVED_FROM is a cache WRITE: with use_temp_path=off nginx renames
    # <md5>.NNNN into place. Summarise instead of one line per write.
    if kind != "DELETE":
        _note_write()
        return

    log("DEL %s pid=%d comm=%s exe=%s name=%s cmd=[%s]" % (kind, pid, comm, exe, name, cmd))

    # Eviction counter: real cache-object unlinks ONLY.
    if not _MD5_RE.match(name):
        log("DEL-ignored (not a cache object) name=%s comm=%s" % (name, comm))
        return

    if not _is_write_temp(name):
        now = time.time()
        actor = classify_actor(comm, exe, cmd)

        # #337: an operator-initiated purge is a deliberate act the orchestrator
        # has already recorded as commanded=1 so the circuit breaker ignores it.
        # It must not feed the mass-eviction counter, or every purge cries wolf
        # and the alert that matters lands in an inbox trained to dismiss it.
        # Reported on its own channel rather than silently dropped: a bulk delete
        # is still worth knowing about, it is simply not an emergency.
        if not counts_toward_eviction(actor):
            _purge_ts.append(now)
            while _purge_ts and now - _purge_ts[0] > EVICT_WINDOW:
                _purge_ts.popleft()
            if len(_purge_ts) >= PURGE_THRESHOLD:
                alert(
                    "purge",
                    "lancache NOTICE: commanded purge (%d deletes/%ds) by %s"
                    % (len(_purge_ts), EVICT_WINDOW, ACTOR_ORCHESTRATOR),
                    "the orchestrator agent is deleting cached game files on 192.168.1.40.\n"
                    "This is a COMMANDED purge, not an eviction - expected after a purge job.\n"
                    "Latest: comm=%s name=%s cmd=[%s]\n"
                    "If you did not start a purge, check the orchestrator jobs table."
                    % (comm, name, cmd),
                )
            return

        _evict_ts.append(now)
        while _evict_ts and now - _evict_ts[0] > EVICT_WINDOW:
            _evict_ts.popleft()
        if len(_evict_ts) >= EVICT_THRESHOLD:
            alert(
                "eviction",
                "lancache ALERT: cache eviction detected (%d deletes/%ds) by %s"
                % (len(_evict_ts), EVICT_WINDOW, actor),
                "a process is deleting cached game files at >= %d in %ds on 192.168.1.40.\n"
                "Latest: actor=%s comm=%s name=%s cmd=[%s]\n"
                "This is the mass-eviction signature. Check the cache index (keys_zone)."
                % (EVICT_THRESHOLD, EVICT_WINDOW, actor, comm, name, cmd),
            )


def handle_attrib(pid, comm, exe, name, cmd, info_bytes):
    if comm == "nginx":
        return  # nginx maintains its own files
    st = stat_via_handle(info_bytes, name)
    mode = ("%04o" % (st.st_mode & 0o7777)) if st else "?"
    log("ATTRIB pid=%d comm=%s exe=%s name=%s mode=%s cmd=[%s]" % (pid, comm, exe, name, mode, cmd))
    if st is not None and not www_readable(st):
        alert(
            "mode000",
            "lancache ALERT: cache file made unreadable (mode %s) by %s" % (mode, comm),
            "A non-nginx process changed a cache file's permissions so www-data can no longer read it "
            "(this causes HTTP 500s / false-partials).\nprocess=%s pid=%d exe=%s\nfile=%s mode=%s\n"
            "On 192.168.1.40 /volume1/cache. Investigate this process (UGOS indexer/scanner?)."
            % (comm, pid, exe, name, mode),
        )


def liveness_loop(cfg):
    """Push both guard monitors on a wall clock, so that silence means dead.

    Pushing on a clock rather than on event arrival is what makes "no heartbeat"
    mean "the guard is dead" instead of "the LAN was quiet tonight".
    """
    while True:
        kuma.push(
            cfg.get("KUMA_PUSH_CACHE_GUARD"),
            "up",
            "guard alive; evict %d/%ds window, purges %d"
            % (len(_evict_ts), EVICT_WINDOW, len(_purge_ts)),
        )

        last_alert = max(_last_sent.get(k, 0) for k in EVICT_ALERT_KINDS)
        since = time.time() - last_alert
        if last_alert and since < EVICT_LATCH_SEC:
            kuma.push(
                cfg.get("KUMA_PUSH_CACHE_EVICTION"),
                "down",
                "cache loss alerted %.0fs ago; latched for %ds" % (since, EVICT_LATCH_SEC),
            )
        else:
            kuma.push(
                cfg.get("KUMA_PUSH_CACHE_EVICTION"),
                "up",
                "no eviction or mode-000 alert in the last %ds" % EVICT_LATCH_SEC,
            )

        time.sleep(LIVENESS_INTERVAL_SEC)


def main():
    fd = libc.fanotify_init(FAN_CLASS_NOTIF | FAN_REPORT_DFID_NAME, O_RDONLY)
    if fd < 0:
        log("FANOTIFY init FAILED errno=%d" % ctypes.get_errno())
        return
    mask = FAN_DELETE | FAN_MOVED_FROM | FAN_ATTRIB | FAN_ONDIR
    for p in (b"/volume1/cache", b"/volume1/cache/cache"):
        r = libc.fanotify_mark(fd, FAN_MARK_ADD | FAN_MARK_FILESYSTEM, mask, AT_FDCWD, p)
        log("FANOTIFY mark %s -> %d (errno %d if -1)" % (p.decode(), r, ctypes.get_errno()))
    log(
        "FANOTIFY guard started (delete+attrib; evict>=%d/%ds; email cooldown %ds)"
        % (EVICT_THRESHOLD, EVICT_WINDOW, COOLDOWN)
    )

    # Daemon threads: a dead thread takes its own monitor silent-and-red while
    # the others stay green, so a partial failure stays visible.
    cfg = load_cfg()
    threading.Thread(target=liveness_loop, args=(cfg,), daemon=True).start()
    # The guard's log(), not print: it flushes stdout so `docker logs` shows the
    # line at once, and keeps the record in /log/deletions.log.
    threading.Thread(target=probe_loop, args=(cfg, log), daemon=True).start()
    log(
        "MONITOR threads started (liveness %ds, key-budget %ss)"
        % (LIVENESS_INTERVAL_SEC, cfg["PROBE_INTERVAL_SEC"])
    )

    while True:
        buf = os.read(fd, 65536)
        off = 0
        while off + META_SZ <= len(buf):
            event_len, vers, _res, _mlen, evmask, evfd, pid = struct.unpack_from(META_FMT, buf, off)
            if event_len < META_SZ:
                break
            info = buf[off + META_SZ : off + event_len]
            name = "?"
            name_info = b""
            i = 0
            while i + 4 <= len(info):
                itype, _pad, ilen = struct.unpack_from("=BBH", info, i)
                if ilen < 4:
                    break
                if itype == FAN_EVENT_INFO_TYPE_DFID_NAME:
                    name_info = info[i + 4 : i + ilen]
                    name = parse_name(name_info)
                i += ilen
            if evfd is not None and evfd >= 0:
                try:
                    os.close(evfd)
                except Exception:
                    pass
            comm, exe, cmd = proc_info(pid)
            if evmask & FAN_ATTRIB:
                handle_attrib(pid, comm, exe, name, cmd, name_info)
            elif evmask & FAN_DELETE:
                handle_delete("DELETE", pid, comm, exe, name, cmd)
            elif evmask & FAN_MOVED_FROM:
                handle_delete("MOVED_FROM", pid, comm, exe, name, cmd)
            off += event_len


if __name__ == "__main__":
    # One-shot alert mode: reuse this module's SMTP config and TLS context so
    # there is a single place that reads /log/alert.env. Used by the prefill
    # cron wrapper to make a stalled prefill loud instead of silent.
    if len(sys.argv) >= 4 and sys.argv[1] == "--send-alert":
        alert("external", sys.argv[2], sys.argv[3])
        sys.exit(0)
    main()

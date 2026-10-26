"""Files on an SSH server, read over SFTP through the system `ssh`.

A saved server can be nothing more than a place the images are: HMS O2's
transfer node is the case that asked for this. It is a machine for moving
files, so nothing may run there -- no job, no data node, no Plexora -- but it
answers SFTP, and SFTP is a filesystem protocol with random access. That is
all the chunk cache in `remote_store` needs: an `sftp://user@host/path` is
read exactly the way an `https://` or `gs://` address is, a byte range at a
time, through the same on-disk cache.

**Why not paramiko or asyncssh (fsspec's own `sftp` and `ssh`).** Both are an
SSH client of their own. They do not read `~/.ssh/config`, cannot hand a Duo
prompt to the page, and know nothing about the keys, agents and jump hosts a
terminal `ssh` already works with. Every other connection Plexora makes goes
through the system ssh with the askpass relay (see `remote_sessions`), and a
second login path that behaves differently from the first is how somebody's
working setup stops working in one half of the app. So the transport here is
`ssh -s <target> sftp`, spawned by `connect.FilesSession`, and this module
speaks SFTP version 3 over that process's stdin and stdout -- a small protocol
(open, read, stat, list), and nothing else is needed.

Three pieces:

**`SFTPClient`**, one SFTP conversation over one process. Thread-safe: any
number of callers send requests, each tagged with an id, and one reader thread
hands every reply to whoever asked. That is what makes one login serve many
reads at once -- a tile burst is dozens of requests in flight on one channel,
not one round trip after another.

**The registry** (`register` / `client_for`), keyed by the ssh target, which
is what a URL's `user@host` is. The connection is opened by the user (it can
ask for a Duo push), so reading an address whose server is not connected
fails at once with a sentence naming the saved server, rather than opening a
login nobody asked for.

**`SFTPFileSystem`**, the async fsspec filesystem `remote_store` builds a
zarr `FsspecStore` over. Every call looks its client up afresh, so a
reconnect is picked up without reopening any store.

A path in an address is percent-encoded only where a URL parser would
otherwise misread it (`%`, `?`, `#`), so an address still reads as the path it
names. `url_for` and `path_of` are the one pair that converts, and everything
that builds an `sftp://` address goes through them.
"""

from __future__ import annotations

import collections
import errno
import itertools
import logging
import stat as stat_module
import struct
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import quote, unquote, urlsplit

_log = logging.getLogger(__name__)

SCHEME = "sftp"

# -- the protocol (draft-ietf-secsh-filexfer-02, "version 3") ---------------

_INIT, _VERSION = 1, 2
_OPEN, _CLOSE, _READ = 3, 4, 5
_LSTAT = 7
_OPENDIR, _READDIR = 11, 12
_REALPATH, _STAT = 16, 17
_EXTENDED = 200
_STATUS, _HANDLE, _DATA, _NAME, _ATTRS = 101, 102, 103, 104, 105
_EXTENDED_REPLY = 201

_FXF_READ = 0x1

_ATTR_SIZE = 0x1
_ATTR_UIDGID = 0x2
_ATTR_PERMISSIONS = 0x4
_ATTR_ACMODTIME = 0x8
_ATTR_EXTENDED = 0x80000000

FX_OK, FX_EOF, FX_NO_SUCH_FILE, FX_PERMISSION_DENIED = 0, 1, 2, 3
FX_FAILURE, FX_BAD_MESSAGE, FX_NO_CONNECTION, FX_CONNECTION_LOST = 4, 5, 6, 7
FX_OP_UNSUPPORTED = 8

#: The read length every server must accept. OpenSSH accepts far more and says
#: so through `limits@openssh.com`; a server that does not say gets this, and
#: a server that answers a read with fewer bytes than asked is followed up
#: rather than believed to have ended.
_DEFAULT_READ = 64 * 1024

#: The most one READ asks for even when a server would allow more: a reply is
#: held whole in memory, and past this the request count stops mattering.
_MAX_READ = 256 * 1024

#: A reply longer than this is not a reply. The first four bytes of a stream
#: that a shell startup file wrote text into ("Welc", "This") read as a length
#: in the gigabytes, which is how that failure is told from a real packet.
_MAX_PACKET = 4 * 1024 ** 2

#: Requests one client lets be outstanding at once. OpenSSH's own `sftp` keeps
#: 64 in flight; a server queues the rest of a burst anyway, and an unbounded
#: number would only move the queue into memory here.
_MAX_IN_FLIGHT = 64

#: Open file handles kept for reuse, and how long an unused one is kept. A TIFF
#: is thousands of small reads of one file, and an OPEN + CLOSE around each is
#: two round trips per tile; a handle held across them is none.
_HANDLE_CAP = 32
_HANDLE_IDLE_S = 60

#: Threads per client running the blocking calls the async filesystem makes.
#: Each holds one call while its replies are outstanding -- not one request --
#: so this bounds concurrent CALLS, and a 2 MiB block is still 32 pipelined
#: reads on a single thread.
_WORKERS = 16

#: How long one call waits for its replies on a connection that is up. Long,
#: because a scratch filesystem can stall a stat for tens of seconds; finite,
#: because a channel that died without closing would otherwise hang forever.
_CALL_TIMEOUT_S = 120


class SFTPError(OSError):
    """A failure that is about this connection rather than about one file.

    `diagnosed` has `connect.ConnectError`'s meaning: the sentence was built by
    something that knew exactly what went wrong, and nothing downstream should
    guess at a different cause from substrings.
    """

    def __init__(self, message, *, diagnosed=False):
        super().__init__(errno.EIO, message)
        self.diagnosed = diagnosed

    def __str__(self):
        return str(self.strerror)


class SFTPNotConnected(ConnectionError):
    """An `sftp://` address whose server has no live connection.

    A ConnectionError, so `remote_store` treats it like a host that cannot be
    reached -- cached bytes still answer -- but `not_connected` lets it skip
    the retries and the ten-second back-off: the fix is a button press away,
    and a viewer should start reading the moment it is pressed.
    """

    not_connected = True

    def __init__(self, target):
        self.target = target
        super().__init__(_not_connected_sentence(target))


def _not_connected_sentence(target):
    name = _profile_name_for(target)
    if name:
        return f"Not connected to {target}. Connect “{name}” to read files there."
    return (f"Not connected to {target}. Add it as a files-only server in "
            "Settings → Remote servers and connect it.")


def _profile_name_for(target):
    """The saved files-only server whose ssh target is `target`, or ""."""
    try:
        from plexora.server.models import remotes
    except Exception:  # noqa: BLE001 -- no profiles is "no name"
        return ""
    try:
        for remote in remotes.load_all().values():
            if remote.files and canonical_target(remote.target) == target:
                return remote.name
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _status_error(code, message, path=""):
    """The OSError an SFTP status stands for, shaped like a local one."""
    text = message or {FX_NO_SUCH_FILE: "No such file",
                       FX_PERMISSION_DENIED: "Permission denied"}.get(code, "")
    if code == FX_NO_SUCH_FILE:
        return FileNotFoundError(errno.ENOENT, text or "No such file", path)
    if code == FX_PERMISSION_DENIED:
        return PermissionError(errno.EACCES, text or "Permission denied", path)
    if code in (FX_NO_CONNECTION, FX_CONNECTION_LOST):
        return ConnectionError(text or "The SFTP connection was lost")
    if code == FX_OP_UNSUPPORTED:
        return OSError(errno.ENOTSUP, text or "Not supported by this server", path)
    return OSError(errno.EIO, text or f"SFTP failure (status {code})", path)


class _Reader:
    """Big-endian fields out of one reply body."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def uint32(self) -> int:
        value = struct.unpack_from(">I", self.data, self.pos)[0]
        self.pos += 4
        return value

    def uint64(self) -> int:
        value = struct.unpack_from(">Q", self.data, self.pos)[0]
        self.pos += 8
        return value

    def bytes(self) -> bytes:
        length = self.uint32()
        value = self.data[self.pos:self.pos + length]
        self.pos += length
        return value

    def text(self) -> str:
        return self.bytes().decode("utf-8", "replace")

    def attrs(self) -> "Attrs":
        flags = self.uint32()
        size = mode = mtime = None
        if flags & _ATTR_SIZE:
            size = self.uint64()
        if flags & _ATTR_UIDGID:
            self.uint32()
            self.uint32()
        if flags & _ATTR_PERMISSIONS:
            mode = self.uint32()
        if flags & _ATTR_ACMODTIME:
            self.uint32()
            mtime = self.uint32()
        if flags & _ATTR_EXTENDED:
            for _ in range(self.uint32()):
                self.bytes()
                self.bytes()
        return Attrs(size=size, mode=mode, mtime=mtime)

    def remaining(self) -> int:
        return len(self.data) - self.pos


def _string(value) -> bytes:
    data = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return struct.pack(">I", len(data)) + data


@dataclass(frozen=True)
class Attrs:
    """What a stat says, as far as reading files needs."""

    size: Optional[int] = None
    mode: Optional[int] = None
    mtime: Optional[int] = None

    @property
    def is_dir(self) -> bool:
        return self.mode is not None and stat_module.S_ISDIR(self.mode)

    @property
    def is_link(self) -> bool:
        return self.mode is not None and stat_module.S_ISLNK(self.mode)


@dataclass
class _Handle:
    handle: bytes
    last_used: float


def _read_exact(stream, count) -> Optional[bytes]:
    """`count` bytes from a blocking binary stream, or None at end of stream."""
    data = stream.read(count)
    if not data:
        return None
    while len(data) < count:
        more = stream.read(count - len(data))
        if not more:
            return None
        data += more
    return data


class SFTPClient:
    """One SFTP conversation over a process's stdin and stdout.

    `process` is anything with binary `stdin`/`stdout` and `poll()` -- in
    production the `ssh -s ... sftp` that `connect.FilesSession` spawned; in
    the tests, OpenSSH's own `sftp-server` run directly, which speaks the same
    protocol with no network and no login in front of it.

    `describe_exit` returns the lines worth quoting when the process goes away
    (ssh's stderr); it is how "the connection closed" becomes "Permission
    denied (publickey,keyboard-interactive)".
    """

    def __init__(self, process, *, label: str = "",
                 describe_exit: Optional[Callable[[], list]] = None,
                 call_timeout: float = _CALL_TIMEOUT_S):
        self.process = process
        self.label = label or "the server"
        self._describe_exit = describe_exit or (lambda: [])
        self.call_timeout = call_timeout
        self.version: Optional[int] = None
        self.extensions: dict[str, bytes] = {}
        self.max_read = _DEFAULT_READ
        self.home: Optional[str] = None
        self._ids = itertools.count(1)
        self._write_lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        self._pending_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(_MAX_IN_FLIGHT)
        self._version = Future()
        self._failure: Optional[BaseException] = None
        self._closed = False
        self._handles: "collections.OrderedDict[str, _Handle]" = collections.OrderedDict()
        self._handles_lock = threading.Lock()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._executor_lock = threading.Lock()
        #: What crossed this conversation, for `stats()`: requests sent and
        #: replies received, their bytes on the wire (SFTP framing included),
        #: READ requests and the data they returned, and the time each
        #: request waited for its reply.
        self.counters = {"requests": 0, "replies": 0, "bytes_out": 0, "bytes_in": 0,
                         "reads": 0, "read_bytes": 0, "wait_s": 0.0}
        self._sent_at: dict[int, float] = {}
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"plexora-sftp-{self.label}")
        self._reader.start()

    # -- lifecycle ---------------------------------------------------------

    def handshake(self, timeout: Optional[float] = None) -> None:
        """Send INIT and wait for VERSION -- which, over ssh, is the login.

        ssh holds what was written to it until the channel opens, so this
        returns once the user has answered every prompt and the server has
        started its SFTP side. `timeout` None waits as long as the process
        lives: a Duo push can take as long as somebody takes to find a phone.
        """
        self._write(struct.pack(">IBI", 5, _INIT, 3))
        try:
            self._version.result(timeout=timeout)
        except FutureTimeout as exc:
            raise SFTPError(
                f"{self.label} did not start an SFTP session within "
                f"{int(timeout or 0)} seconds.", diagnosed=True) from exc
        if "limits@openssh.com" in self.extensions:
            try:
                self._ask_limits()
            except Exception:  # noqa: BLE001 -- a refinement, never required
                pass

    def _ask_limits(self):
        reply = self._expect(self._send(_EXTENDED, _string("limits@openssh.com")),
                             _EXTENDED_REPLY)
        reply.uint64()                       # max packet length
        max_read = reply.uint64()
        if max_read:
            self.max_read = max(4096, min(int(max_read), _MAX_READ))

    @property
    def alive(self) -> bool:
        if self._closed or self._failure is not None:
            return False
        poll = getattr(self.process, "poll", None)
        return poll is None or poll() is None

    def close(self) -> None:
        """Stop taking requests and fail every one still waiting.

        The process is its owner's to end (`FilesSession.stop`); closing its
        stdin is what tells a server that the conversation is over, which is
        all this does to it.
        """
        if self._closed:
            return
        self._closed = True
        self._fail_all(ConnectionError(f"The connection to {self.label} was closed."))
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        with self._executor_lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def run(self, func, *args):
        """`func(*args)` on this client's own worker threads, as a Future.

        For the async filesystem: its calls block on replies, and doing that on
        zarr's IO loop would stall every other read in the process.
        """
        with self._executor_lock:
            if self._executor is None:
                if self._closed:
                    raise self._connection_error()
                self._executor = ThreadPoolExecutor(
                    max_workers=_WORKERS, thread_name_prefix="plexora-sftp-call")
            return self._executor.submit(func, *args)

    # -- the wire ----------------------------------------------------------

    def _write(self, data: bytes) -> None:
        with self._write_lock:
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise self._connection_error() from exc

    def _send(self, ptype: int, payload: bytes) -> Future:
        if self._closed or self._failure is not None:
            raise self._connection_error()
        self._slots.acquire()
        future: Future = Future()
        future.add_done_callback(lambda _: self._slots.release())
        request_id = next(self._ids) & 0xFFFFFFFF
        body = struct.pack(">BI", ptype, request_id) + payload
        with self._pending_lock:
            self._pending[request_id] = future
            counters = self.counters
            counters["requests"] += 1
            counters["bytes_out"] += 4 + len(body)
            if ptype == _READ:
                counters["reads"] += 1
            self._sent_at[request_id] = time.monotonic()
        try:
            self._write(struct.pack(">I", len(body)) + body)
        except BaseException as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            if not future.done():
                future.set_exception(exc)
            raise
        # A reply can only be lost to a failure after the check above; the
        # failure path fails everything pending, this one included.
        if self._failure is not None and not future.done():
            with self._pending_lock:
                self._pending.pop(request_id, None)
            future.set_exception(self._connection_error())
        return future

    def _read_loop(self) -> None:
        stream = self.process.stdout
        failure: Optional[BaseException] = None
        try:
            while True:
                head = _read_exact(stream, 4)
                if head is None:
                    break
                (length,) = struct.unpack(">I", head)
                if length == 0 or length > _MAX_PACKET:
                    failure = self._garbage(head, stream)
                    break
                body = _read_exact(stream, length)
                if body is None:
                    break
                self._dispatch(body)
        except Exception as exc:  # noqa: BLE001 -- ends the conversation
            failure = exc
        if failure is None:
            failure = self._connection_error(ended=True)
        self._fail_all(failure)

    def _garbage(self, head: bytes, stream) -> SFTPError:
        """The error for a stream that is not SFTP: text in front of it."""
        try:
            more = stream.read1(200) if hasattr(stream, "read1") else b""
        except Exception:  # noqa: BLE001
            more = b""
        text = (head + (more or b"")).decode("utf-8", "replace")
        text = " ".join(text.split())[:120]
        return SFTPError(
            f"{self.label} printed text where its SFTP reply should be "
            f"(“{text}”). A shell startup file that prints on every login -- "
            "an `echo` in ~/.bashrc, say -- does this. Make it print only in "
            "interactive shells (wrap it in `if [[ $- == *i* ]]; then ... fi`) "
            "and connect again.", diagnosed=True)

    def _dispatch(self, body: bytes) -> None:
        ptype = body[0]
        if ptype == _VERSION:
            reader = _Reader(body, 1)
            version = reader.uint32()
            extensions = {}
            while reader.remaining() > 0:
                name = reader.text()
                extensions[name] = reader.bytes()
            self.version = version
            self.extensions = extensions
            if not self._version.done():
                self._version.set_result(version)
            return
        if len(body) < 5:
            return
        (request_id,) = struct.unpack_from(">I", body, 1)
        counters = self.counters
        counters["replies"] += 1
        counters["bytes_in"] += 4 + len(body)
        if ptype == _DATA:
            counters["read_bytes"] += max(0, len(body) - 9)
        sent = self._sent_at.pop(request_id, None)
        if sent is not None:
            counters["wait_s"] += time.monotonic() - sent
        with self._pending_lock:
            future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result((ptype, _Reader(body, 5)))

    def _fail_all(self, failure: BaseException) -> None:
        if self._failure is None:
            self._failure = failure
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(failure)
        if not self._version.done():
            self._version.set_exception(failure)

    def _connection_error(self, ended: bool = False) -> BaseException:
        if self._failure is not None and not ended:
            return self._failure
        lines = [str(line).strip() for line in (self._describe_exit() or [])
                 if str(line).strip()]
        said = lines[-1] if lines else ""
        if said:
            return ConnectionError(f"The connection to {self.label} closed: {said}")
        return ConnectionError(f"The connection to {self.label} closed.")

    def _reply(self, future: Future):
        try:
            return future.result(timeout=self.call_timeout)
        except FutureTimeout as exc:
            raise TimeoutError(
                f"{self.label} did not answer within {int(self.call_timeout)} "
                "seconds") from exc

    def _expect(self, future: Future, want: int, path: str = "") -> _Reader:
        ptype, reader = self._reply(future)
        if ptype == _STATUS:
            code = reader.uint32()
            message = reader.text() if reader.remaining() >= 4 else ""
            if code == FX_OK and want == _STATUS:
                return reader
            raise _status_error(code, message, path)
        if ptype != want:
            raise SFTPError(f"{self.label} sent an unexpected SFTP reply "
                            f"({ptype}, wanted {want}).")
        return reader

    # -- operations --------------------------------------------------------

    def stat(self, path: str) -> Attrs:
        """What `path` is, following symlinks."""
        return self._expect(self._send(_STAT, _string(path)), _ATTRS, path).attrs()

    def lstat(self, path: str) -> Attrs:
        return self._expect(self._send(_LSTAT, _string(path)), _ATTRS, path).attrs()

    def realpath(self, path: str) -> str:
        reply = self._expect(self._send(_REALPATH, _string(path)), _NAME, path)
        if reply.uint32() < 1:
            raise SFTPError(f"{self.label} gave no answer for {path}")
        return reply.text()

    def listdir(self, path: str) -> list:
        """`[(name, Attrs)]` in `path`, `.` and `..` left out, links followed.

        A symlink is stat'ed through (all of them at once): a cluster's group
        and scratch folders are very often links, and one listed as a file
        would be a folder nobody could open from the picker.
        """
        handle = self._expect(self._send(_OPENDIR, _string(path)), _HANDLE,
                              path).bytes()
        found = []
        try:
            while True:
                ptype, reader = self._reply(self._send(_READDIR, _string(handle)))
                if ptype == _STATUS:
                    code = reader.uint32()
                    if code == FX_EOF:
                        break
                    message = reader.text() if reader.remaining() >= 4 else ""
                    raise _status_error(code, message, path)
                if ptype != _NAME:
                    raise SFTPError(f"{self.label} sent an unexpected listing reply")
                for _ in range(reader.uint32()):
                    name = reader.text()
                    reader.bytes()           # the `ls -l` line, unused
                    attrs = reader.attrs()
                    if name in (".", ".."):
                        continue
                    found.append((name, attrs))
        finally:
            self._close_handle(handle)
        links = [(i, self._send(_STAT, _string(_join(path, name))))
                 for i, (name, attrs) in enumerate(found) if attrs.is_link]
        for index, future in links:
            try:
                followed = self._expect(future, _ATTRS).attrs()
            except OSError:
                continue                     # a broken link stays a link
            found[index] = (found[index][0], followed)
        return found

    def read_range(self, path: str, start: int, end: int) -> bytes:
        """Bytes `[start, end)` of the file at `path`; short at end of file."""
        start, end = max(0, int(start)), int(end)
        if end <= start:
            return b""
        handle = self._handle_for(path)
        try:
            return self._read_span(handle, start, end, path)
        except OSError as exc:
            if isinstance(exc, (FileNotFoundError, PermissionError, ConnectionError,
                                TimeoutError, SFTPError)):
                raise
            # A handle this client closed for being idle, or one the server
            # dropped, answers FAILURE: reopen once before believing it.
            self._forget_handle(path, handle)
            return self._read_span(self._handle_for(path), start, end, path)

    def read_all(self, path: str) -> bytes:
        size = self.stat(path).size
        if size is None:
            raise SFTPError(f"{self.label} does not say how long {path} is")
        return self.read_range(path, 0, size)

    def _read_span(self, handle: bytes, start: int, end: int, path: str) -> bytes:
        pieces: dict[int, bytes] = {}
        wanted = [(offset, min(self.max_read, end - offset))
                  for offset in range(start, end, self.max_read)]
        while wanted:
            sent = [(offset, length,
                     self._send(_READ, _string(handle) + struct.pack(">QI", offset, length)))
                    for offset, length in wanted]
            wanted = []
            for offset, length, future in sent:
                ptype, reader = self._reply(future)
                if ptype == _STATUS:
                    code = reader.uint32()
                    if code == FX_EOF:
                        continue
                    message = reader.text() if reader.remaining() >= 4 else ""
                    raise _status_error(code, message, path)
                if ptype != _DATA:
                    raise SFTPError(f"{self.label} sent an unexpected read reply")
                data = reader.bytes()
                if not data:
                    continue
                pieces[offset] = data
                if len(data) < length:
                    # Fewer bytes than asked is not the end of the file: a
                    # server may cap a read below what it advertised. The end
                    # is an EOF status, and only that.
                    wanted.append((offset + len(data), length - len(data)))
        out = bytearray()
        position = start
        while position in pieces:
            chunk = pieces[position]
            out += chunk
            position += len(chunk)
        return bytes(out)

    # -- handles -----------------------------------------------------------

    def _handle_for(self, path: str) -> bytes:
        now = time.monotonic()
        with self._handles_lock:
            entry = self._handles.get(path)
            if entry is not None:
                entry.last_used = now
                self._handles.move_to_end(path)
                return entry.handle
        handle = self._expect(
            self._send(_OPEN, _string(path) + struct.pack(">II", _FXF_READ, 0)),
            _HANDLE, path).bytes()
        stale = []
        with self._handles_lock:
            existing = self._handles.get(path)
            if existing is not None:
                # Opened twice by two callers at once: keep the first.
                stale.append(handle)
                handle = existing.handle
            else:
                self._handles[path] = _Handle(handle, now)
            for name in list(self._handles):
                entry = self._handles[name]
                idle = now - entry.last_used > _HANDLE_IDLE_S
                if (idle or len(self._handles) > _HANDLE_CAP) and name != path:
                    stale.append(self._handles.pop(name).handle)
        for old in stale:
            self._close_handle(old, wait=False)
        return handle

    def _forget_handle(self, path: str, handle: bytes) -> None:
        with self._handles_lock:
            entry = self._handles.get(path)
            if entry is not None and entry.handle == handle:
                del self._handles[path]
        self._close_handle(handle, wait=False)

    def _close_handle(self, handle: bytes, wait: bool = True) -> None:
        try:
            future = self._send(_CLOSE, _string(handle))
        except Exception:  # noqa: BLE001 -- a closed connection closed it
            return
        if wait:
            try:
                self._reply(future)
            except Exception:  # noqa: BLE001
                pass


def _join(directory: str, name: str) -> str:
    return directory.rstrip("/") + "/" + name if directory != "/" else "/" + name


# -- addresses -------------------------------------------------------------

#: Kept literal in a URL path: everything printable except what a URL parser
#: would read as the end of the path (`?`, `#`) or as an escape (`%`).
_PATH_SAFE = "/!$&'()*+,;=:@~-._ "


def canonical_target(target) -> str:
    """`user@host[:port]` with the host lower-cased and the user left alone.

    A username is case-sensitive on the machine it belongs to, and a host name
    is not; this is the one spelling the registry and every URL agree on.
    """
    text = str(target or "").strip()
    user, at, host = text.rpartition("@")
    return f"{user}{at}{host.lower()}"


def url_for(target, path) -> str:
    """The `sftp://` address of `path` on `target`."""
    path = "/" + str(path or "").lstrip("/")
    return f"{SCHEME}://{canonical_target(target)}{quote(path, safe=_PATH_SAFE)}"


def path_of(url) -> str:
    """The absolute path an `sftp://` address names on its server."""
    path = unquote(urlsplit(str(url)).path) or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def target_of(url) -> str:
    """The ssh target an `sftp://` address belongs to."""
    return canonical_target(urlsplit(str(url)).netloc)


# -- the registry ----------------------------------------------------------

_clients: dict[str, SFTPClient] = {}
_clients_lock = threading.Lock()


def register(target, client: SFTPClient) -> None:
    with _clients_lock:
        _clients[canonical_target(target)] = client


def unregister(target, client: Optional[SFTPClient] = None) -> None:
    """Forget `target`'s client -- only `client`, when one is named, so a
    session ending late cannot take down the reconnection that replaced it."""
    key = canonical_target(target)
    with _clients_lock:
        if client is None or _clients.get(key) is client:
            _clients.pop(key, None)


def client_for(target) -> Optional[SFTPClient]:
    """The live client for `target`, or None."""
    with _clients_lock:
        client = _clients.get(canonical_target(target))
    return client if client is not None and client.alive else None


def require_client(target) -> SFTPClient:
    client = client_for(target)
    if client is None:
        raise SFTPNotConnected(canonical_target(target))
    return client


def stats() -> dict:
    """What every registered client has sent and received so far, by target.

    Counters only grow while a client lives; a reconnect starts a new client
    at zero. `wait_s` is summed over requests, so concurrent requests count
    their overlap once each.
    """
    import os

    with _clients_lock:
        clients = dict(_clients)
    out = {}
    for key, client in clients.items():
        counts = dict(client.counters)
        counts["wait_s"] = round(counts["wait_s"], 3)
        counts["alive"] = client.alive
        counts["ssh_pid"] = getattr(client.process, "pid", None)
        out[key] = counts
    return {"pid": os.getpid(), "clients": out}


def connected_targets() -> list:
    with _clients_lock:
        return [key for key, client in _clients.items() if client.alive]


# -- browsing --------------------------------------------------------------


def listing(base, raw="", *, limit: int = 2000, show_hidden: bool = False) -> dict:
    """`/list_dir`'s answer for a folder on a files-only server.

    The same shape `dir_listing.listing` gives for a local folder, with every
    path a whole `sftp://` address: the picker opens what it is handed and
    never joins paths, so what it hands back is already what an import takes.
    `raw` empty means the account's home folder; a file means its folder.
    """
    from plexora.server.utils import dataset_marks
    from plexora.server.utils.dir_listing import ListingError

    target = target_of(base)
    client = require_client(target)
    text = str(raw or "").strip()
    if text.lower().startswith(f"{SCHEME}://"):
        if target_of(text) != target:
            raise ListingError(f"{text} is not on {target}")
        path = path_of(text)
    elif text:
        path = text if text.startswith("/") else _join(client.home or "/", text)
    else:
        path = client.home or client.realpath(".")
    try:
        attrs = client.stat(path)
        if not attrs.is_dir:
            path = path.rsplit("/", 1)[0] or "/"
        found = client.listdir(path)
    except PermissionError as exc:
        raise ListingError(f"Permission denied: {path}") from exc
    except FileNotFoundError as exc:
        raise ListingError(f"Not a folder: {path}") from exc
    except OSError as exc:
        raise ListingError(f"Cannot read {path}: {exc}") from exc

    found = [(name, attrs) for name, attrs in found
             if show_hidden or not name.startswith(".")]
    found.sort(key=lambda item: (not item[1].is_dir, item[0].lower()))
    truncated = len(found) > limit
    # The readdir's own attributes carry the date, so nothing is `partial`
    # here; a link `listdir` could not follow is the broken one.
    entries = [{"name": name, "is_dir": attrs.is_dir,
                "size": None if attrs.is_dir else attrs.size,
                "path": url_for(target, _join(path, name)),
                "mtime": attrs.mtime,
                "kind": ("broken" if attrs.is_link
                         else "dir" if attrs.is_dir else "file"),
                "store": dataset_marks.store_by_name(name) if attrs.is_dir else None}
               for name, attrs in found[:limit]]
    parent = None
    if path != "/":
        parent = url_for(target, path.rsplit("/", 1)[0] or "/")
    trail = []
    parts = [p for p in path.split("/") if p]
    trail.append({"label": "/", "path": url_for(target, "/")})
    for index in range(len(parts)):
        trail.append({"label": parts[index],
                      "path": url_for(target, "/" + "/".join(parts[:index + 1]))})
    # `platform` is not knowable over SFTP without running something there,
    # and a files-only server runs nothing of ours; "" is "not said".
    return {"path": url_for(target, path), "parent": parent, "crumbs": trail,
            "entries": entries, "truncated": truncated, "total": len(found),
            "partial": False, "dataset": None, "platform": ""}


# -- whole files -----------------------------------------------------------

#: What `head_copy` brings across: a header and enough rows to read it by.
HEAD_BYTES = 1024 * 1024

#: One read of `download`, written out before the next is asked for.
DOWNLOAD_CHUNK = 16 * 1024 ** 2


def head_copy(url, limit: int = HEAD_BYTES):
    """`(local path, whole)`: the start of the file at `url`, on this machine.

    For a table, which nothing here streams: what it IS is read from its header
    and first rows, and those are a megabyte at most. The copy keeps the file's
    own name, because detection reads names too, and is kept by address, size
    and date, so asking twice reads once. A copy that stops short ends at a
    line break; `whole` says it is the entire file, which a channel list
    always is.
    """
    import hashlib
    import os
    import tempfile
    from pathlib import Path

    path = path_of(url)
    client = require_client(target_of(url))
    attrs = client.stat(path)
    size = int(attrs.size or 0)
    key = hashlib.sha1(f"{url}\0{size}\0{attrs.mtime}".encode()).hexdigest()[:16]
    local = (Path(tempfile.gettempdir()) / "plexora-sftp-heads" / key
             / path.rsplit("/", 1)[-1])
    whole = size <= limit
    if not local.exists():
        data = client.read_range(path, 0, min(size, limit))
        if not whole:
            cut = data.rfind(b"\n")
            data = data[:cut + 1] if cut > 0 else data
        local.parent.mkdir(parents=True, exist_ok=True)
        partial = local.with_name(local.name + ".partial")
        partial.write_bytes(data)
        os.replace(partial, local)
    return local, whole


def download(url, destination, *, progress: Optional[Callable] = None,
             chunk: int = DOWNLOAD_CHUNK):
    """Copy the file at `url` to `destination`; the destination.

    Written to a `.partial` beside it and renamed at the end, so a copy cut
    off half way is never mistaken for the file.

    @param progress - `progress(done, total)` in bytes, after each chunk.
    """
    import os
    from pathlib import Path

    path = path_of(url)
    client = require_client(target_of(url))
    size = client.stat(path).size
    if size is None:
        raise SFTPError(f"{client.label} does not say how long {path} is")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    done = 0
    try:
        with open(partial, "wb") as out:
            while done < size:
                data = client.read_range(path, done, min(size, done + chunk))
                if not data:
                    raise SFTPError(f"{path} ended {size - done} bytes early")
                out.write(data)
                done += len(data)
                if progress:
                    progress(done, size)
        os.replace(partial, destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return destination


# -- the fsspec filesystem ---------------------------------------------------


def filesystem(url):
    """`(SFTPFileSystem, path)` for an `sftp://` store root.

    The path is the address's own, still escaped -- see `SFTPFileSystem`.
    """
    fs = SFTPFileSystem(target_of(url))
    return fs, fs._strip_protocol(url)


try:
    from fsspec.asyn import AsyncFileSystem
except ImportError:  # pragma: no cover -- fsspec is a core dependency
    AsyncFileSystem = object


class SFTPFileSystem(AsyncFileSystem):
    """The async fsspec filesystem zarr reads an `sftp://` store through.

    Not registered with fsspec under "sftp": that name already belongs to
    fsspec's paramiko implementation, and quietly replacing it for every
    library in the process is not this module's call. `remote_store` builds
    one of these by hand for an `sftp://` root instead.

    **Its paths are the address's paths, escapes and all.** `remote_store`
    makes a store key by cutting it out of an address (a TIFF's key is the
    URL's last segment), so a key arrives escaped exactly as the address was;
    unescaping the root but not the key would join two spellings into a path
    that names nothing. Everything here stays in the address's spelling, and
    `_wire` turns it into the server's at the one place it leaves.
    """

    protocol = (SCHEME,)
    root_marker = "/"
    cachable = False
    async_impl = True

    def __init__(self, target, **kwargs):
        kwargs.setdefault("asynchronous", True)
        super().__init__(**kwargs)
        self.target = canonical_target(target)

    @classmethod
    def _strip_protocol(cls, path):
        if isinstance(path, (list, tuple)):
            return [cls._strip_protocol(p) for p in path]
        text = str(path)
        if text.lower().startswith(f"{SCHEME}://"):
            text = urlsplit(text).path
        if len(text) > 1:
            text = text.rstrip("/")
        return text or "/"

    def unstrip_protocol(self, name):
        return f"{SCHEME}://{self.target}{self._strip_protocol(name)}"

    @staticmethod
    def _wire(path):
        """An escaped path as the server spells it."""
        return unquote(path)

    async def _call(self, method, path, *args):
        import asyncio

        client = require_client(self.target)
        return await asyncio.wrap_future(
            client.run(getattr(client, method), self._wire(path), *args))

    def _entry(self, path, attrs):
        return {"name": path, "size": 0 if attrs.is_dir else attrs.size,
                "type": "directory" if attrs.is_dir else "file",
                "mtime": attrs.mtime}

    async def _info(self, path, **kwargs):
        path = self._strip_protocol(path)
        return self._entry(path, await self._call("stat", path))

    async def _ls(self, path, detail=True, **kwargs):
        path = self._strip_protocol(path)
        found = await self._call("listdir", path)
        entries = [self._entry(_join(path, quote(name, safe=_PATH_SAFE)), attrs)
                   for name, attrs in found]
        entries.sort(key=lambda e: e["name"])
        return entries if detail else [e["name"] for e in entries]

    async def _exists(self, path, **kwargs):
        try:
            await self._info(path)
        except FileNotFoundError:
            return False
        return True

    async def _cat_file(self, path, start=None, end=None, **kwargs):
        path = self._strip_protocol(path)
        if start is None and end is None:
            return await self._call("read_all", path)
        start = 0 if start is None else int(start)
        if start < 0 or end is None or end < 0:
            size = (await self._call("stat", path)).size
            if size is None:
                raise SFTPError(f"{self.target} does not say how long {path} is")
            if start < 0:
                start = max(0, size + start)
            end = size if end is None else (size + end if end < 0 else end)
        return await self._call("read_range", path, start, int(end))

    async def _cat_ranges(self, paths, starts, ends, max_gap=None, on_error="return",
                          **kwargs):
        import asyncio

        async def one(path, start, end):
            try:
                return await self._cat_file(path, start, end)
            except Exception as exc:  # noqa: BLE001 -- returned, as asked
                if on_error == "return":
                    return exc
                raise

        return await asyncio.gather(*(one(p, s, e) for p, s, e in zip(paths, starts, ends)))

    async def _pipe_file(self, path, value, **kwargs):
        raise PermissionError(f"{self.target} is opened read-only")

    async def _rm_file(self, path, **kwargs):
        raise PermissionError(f"{self.target} is opened read-only")


__all__ = [
    "Attrs",
    "SCHEME",
    "SFTPClient",
    "SFTPError",
    "SFTPFileSystem",
    "SFTPNotConnected",
    "canonical_target",
    "client_for",
    "connected_targets",
    "filesystem",
    "listing",
    "path_of",
    "register",
    "require_client",
    "target_of",
    "unregister",
    "url_for",
]

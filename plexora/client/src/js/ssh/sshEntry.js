/**
 * The website's SSH client: what starts a data node on a server from the
 * page, with nothing installed on this computer (docs/internal/
 * WEB_REMOTE_RELAY.md). Built into `dist/ssh_bundle.js` by
 * webpack.ssh.config.js and loaded by services/webRemote.js the first time a
 * connection starts.
 *
 * A browser cannot open TCP, so the bytes travel over a WebSocket to the
 * relay (`/v1/ssh`), which opens the TCP connection to the server's SSH port.
 * SSH itself runs here, end to end: the relay sees the server's address and
 * encrypted bytes, never a password, a Duo answer or a command. The library
 * is Microsoft's dev-tunnels-ssh (MIT; the one VS Code for the Web uses).
 *
 * For a data node the connection lives for seconds: it signs in, runs the
 * start script and reads its output until the node has joined its room, then
 * closes. Nothing that outlives it depends on it, so a relay restart can cost
 * only a retry of the start, never a session.
 *
 * For a files-only server (an SFTP server where nothing of ours runs, such as
 * HMS O2's transfer node) it is the whole connection: `sftp()` opens the
 * server's SFTP side on it and reads files a byte range at a time, as the
 * desktop's `server/utils/sftp.py` does over its system ssh.
 *
 *   PlexoraSshLib.open({url, protocols, username, hostKey, ask, log})
 *       -> Promise<{exec(command, {stdin}) -> Process, sftp() -> Sftp, close(), closed}>
 *
 *   hostKey({algorithm, fingerprint}) -> Promise<boolean>   accept the key?
 *   ask({name, instruction, prompts: [{prompt, echo}]}) -> Promise<string[]|null>
 *   Process: {onLine(fn), done (Promise<{code}>), write(text), close()}
 *   Sftp: {stat(path), lstat(path), realpath(path), listdir(path),
 *          read(path, start, end), close(), closed}
 *       attributes are {size, mode, mtime} (null where the server said
 *       nothing); a failure is an Error with `fx`, SFTP's status code, and
 *       `lost` when the connection went away under it.
 */
import {
    SshClientSession,
    SshSessionConfiguration,
    WebSocketStream,
    SshAuthenticationType,
    CommandRequestMessage,
    AuthenticationInfoResponseMessage,
    AuthenticationRequestMessage,
    SshDisconnectReason,
} from "@microsoft/dev-tunnels-ssh";
import { DisconnectMessage } from "@microsoft/dev-tunnels-ssh/messages/transportMessages";

// The library's keyboard-interactive request ends after the method name, but
// RFC 4256 §3.1 gives it two more strings -- a language tag and the
// submethods, both empty here -- and OpenSSH reads both: without them the
// server drops the connection as an incomplete packet (asyncssh says so in
// as many words). Added for that one request, which is the only one written
// by the base class with this method name.
const writeRequest = AuthenticationRequestMessage.prototype.onWrite;
AuthenticationRequestMessage.prototype.onWrite = function onWrite(writer) {
    writeRequest.call(this, writer);
    if (this.constructor === AuthenticationRequestMessage && this.methodName === "keyboard-interactive") {
        writer.writeString("", "ascii");
        writer.writeString("", "utf8");
    }
};

// The same for the disconnect it sends when the page closes a connection:
// RFC 4253 §11.1 ends it with a language tag, which the library writes only
// when one is set, so the server logs the goodbye as an incomplete packet.
const writeDisconnect = DisconnectMessage.prototype.onWrite;
DisconnectMessage.prototype.onWrite = function onWrite(writer) {
    if (!this.language) this.language = "en";
    writeDisconnect.call(this, writer);
};

//: How long the WebSocket to the relay may take to open.
const OPEN_MS = 20000;
//: A keep-alive every this many seconds of quiet: a files connection can sit
//: idle while somebody reads a figure, and an idle path is the one a network
//: box in between forgets.
const KEEPALIVE_S = 30;
//: The relay's own refusals (relay/src/protocol.ts): asking again changes nothing.
const REFUSALS = [4400, 4401, 4403, 4404, 4429];

function base64(bytes) {
    let text = "";
    for (const byte of bytes) text += String.fromCharCode(byte);
    return btoa(text).replace(/=+$/, "");
}

//: `SHA256:<base64>`, as OpenSSH prints a host key's fingerprint, so the one
//: shown here can be checked against the one a site publishes.
async function fingerprint(blob) {
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", blob));
    return `SHA256:${base64(digest)}`;
}

function openSocket(url, protocols) {
    return new Promise((resolve, reject) => {
        const socket = new WebSocket(url, protocols || []);
        socket.binaryType = "arraybuffer";
        const timer = setTimeout(() => {
            socket.close();
            reject(Object.assign(new Error("The relay did not answer."), { transport: true }));
        }, OPEN_MS);
        socket.onopen = () => {
            clearTimeout(timer);
            socket.onopen = null;
            socket.onerror = null;
            resolve(socket);
        };
        socket.onerror = () => { /* the close event below says why */ };
        socket.onclose = (event) => {
            clearTimeout(timer);
            // `transport`: the way there failed, not the relay's own refusal
            // (a bad request, the licence, too many connections), so trying
            // again in a moment can work.
            reject(Object.assign(new Error(event.reason || `The relay refused the connection (${event.code}).`),
                                 { code: event.code, transport: !REFUSALS.includes(event.code) }));
        };
    });
}

//: Lines out of a byte stream, the last partial one held back.
function lineSplitter(emit) {
    const decoder = new TextDecoder();
    let rest = "";
    return {
        push(bytes) {
            rest += decoder.decode(bytes, { stream: true });
            const parts = rest.split(/\r?\n|\r/);
            rest = parts.pop();
            for (const line of parts) emit(line);
        },
        flush() {
            rest += decoder.decode();
            if (rest) emit(rest);
            rest = "";
        },
    };
}

// -- SFTP version 3 (draft-ietf-secsh-filexfer-02): what reading needs ------
//
// The subset, limits and habits of the desktop's server/utils/sftp.py: many
// requests in flight on one channel, reads in the largest size the server
// allows, a file's handle kept open across the many reads of one file.

const FXP = {
    INIT: 1, VERSION: 2, OPEN: 3, CLOSE: 4, READ: 5, LSTAT: 7, OPENDIR: 11, READDIR: 12,
    REALPATH: 16, STAT: 17, EXTENDED: 200,
    STATUS: 101, HANDLE: 102, DATA: 103, NAME: 104, ATTRS: 105, EXTENDED_REPLY: 201,
};
const FX_OK = 0;
const FX_EOF = 1;
const FX_NO_SUCH_FILE = 2;
const FX_PERMISSION_DENIED = 3;
const FX_FAILURE = 4;
const FX_NO_CONNECTION = 6;
const FX_CONNECTION_LOST = 7;
const ATTR = { SIZE: 0x1, UIDGID: 0x2, PERMISSIONS: 0x4, ACMODTIME: 0x8, EXTENDED: 0x80000000 };
//: The read length every server must accept; OpenSSH says it takes more
//: (`limits@openssh.com`), up to the most one READ asks for here.
const SFTP_DEFAULT_READ = 64 * 1024;
const SFTP_MAX_READ = 256 * 1024;
//: A reply longer than this is not a reply: text a shell startup file printed
//: into the channel reads as a length in the gigabytes.
const SFTP_MAX_PACKET = 4 * 1024 * 1024;
const SFTP_IN_FLIGHT = 64;
const SFTP_HANDLE_CAP = 32;
const SFTP_HANDLE_IDLE_MS = 60000;
//: How long one request waits for its reply on a connection that is up: a
//: scratch filesystem can stall a stat for tens of seconds.
const SFTP_CALL_MS = 120000;

const utf8 = new TextEncoder();
const fromUtf8 = new TextDecoder();

function sftpError(message, fx, lost = false) {
    const error = new Error(message);
    error.fx = fx;
    error.lost = lost;
    return error;
}

function statusError(code, message) {
    const text = message || { [FX_NO_SUCH_FILE]: "No such file",
                              [FX_PERMISSION_DENIED]: "Permission denied" }[code]
        || `SFTP failure (status ${code})`;
    return sftpError(text, code, code === FX_NO_CONNECTION || code === FX_CONNECTION_LOST);
}

function isLink(attrs) {
    return attrs.mode !== null && (attrs.mode & 0o170000) === 0o120000;
}

function joinPath(directory, name) {
    return directory === "/" ? `/${name}` : `${directory.replace(/\/+$/, "")}/${name}`;
}

//: A packet's body, a field at a time, big-endian.
class Packer {
    constructor() {
        this.parts = [];
        this.length = 0;
    }

    push(bytes) {
        this.parts.push(bytes);
        this.length += bytes.length;
        return this;
    }

    u8(value) {
        return this.push(Uint8Array.of(value));
    }

    u32(value) {
        const bytes = new Uint8Array(4);
        new DataView(bytes.buffer).setUint32(0, value >>> 0);
        return this.push(bytes);
    }

    u64(value) {
        const bytes = new Uint8Array(8);
        const view = new DataView(bytes.buffer);
        view.setUint32(0, Math.floor(value / 2 ** 32));
        view.setUint32(4, value >>> 0);
        return this.push(bytes);
    }

    string(value) {
        const bytes = typeof value === "string" ? utf8.encode(value) : value;
        this.u32(bytes.length);
        return this.push(bytes);
    }

    bytes() {
        const out = new Uint8Array(this.length);
        let at = 0;
        for (const part of this.parts) {
            out.set(part, at);
            at += part.length;
        }
        return out;
    }
}

//: Fields out of one reply. Reading past its end throws (a RangeError), which
//: is a reply that was not SFTP.
class Unpacker {
    constructor(bytes) {
        this.data = bytes;
        this.view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        this.at = 0;
    }

    get remaining() {
        return this.data.length - this.at;
    }

    u8() {
        const value = this.view.getUint8(this.at);
        this.at += 1;
        return value;
    }

    u32() {
        const value = this.view.getUint32(this.at);
        this.at += 4;
        return value;
    }

    u64() {
        const high = this.u32();
        return high * 2 ** 32 + this.u32();
    }

    string() {
        const length = this.u32();
        if (length > this.remaining) throw new RangeError("a string past the end of its packet");
        const value = this.data.slice(this.at, this.at + length);
        this.at += length;
        return value;
    }

    text() {
        return fromUtf8.decode(this.string());
    }

    attrs() {
        const flags = this.u32();
        let size = null;
        let mode = null;
        let mtime = null;
        if (flags & ATTR.SIZE) size = this.u64();
        if (flags & ATTR.UIDGID) {
            this.u32();
            this.u32();
        }
        if (flags & ATTR.PERMISSIONS) mode = this.u32();
        if (flags & ATTR.ACMODTIME) {
            this.u32();
            mtime = this.u32();
        }
        if (flags & ATTR.EXTENDED) {
            for (let count = this.u32(); count > 0; count -= 1) {
                this.string();
                this.string();
            }
        }
        return { size, mode, mtime };
    }
}

//: One SFTP conversation on one channel. Any number of calls at once, each
//: request tagged with an id and its reply handed back to whoever asked.
class Sftp {
    constructor(channel) {
        this.channel = channel;
        this.maxRead = SFTP_DEFAULT_READ;
        this.extensions = {};
        this.version = null;
        this.nextId = 1;
        this.pending = new Map();
        this.queue = [];
        this.inFlight = 0;
        this.handles = new Map();
        this.opening = new Map();
        this.chunks = [];
        this.buffered = 0;
        this.failure = null;
        this.writing = Promise.resolve();
        this.onVersion = null;
        this.closed = new Promise((resolve) => { this.markClosed = resolve; });
        channel.onDataReceived((data) => {
            this.receive(data);
            channel.adjustWindow(data.length);
        });
        // The server's stderr: nothing reads it, but its window must move.
        channel.onExtendedDataReceived((event) => channel.adjustWindow(event.data.length));
        channel.onClosed(() => this.fail(sftpError("The SFTP connection closed.", FX_CONNECTION_LOST, true)));
    }

    // -- the wire ----------------------------------------------------------

    receive(data) {
        if (this.failure) return;
        this.chunks.push(Uint8Array.from(data));
        this.buffered += data.length;
        try {
            while (this.buffered >= 4) {
                const head = this.peek(4);
                const length = new DataView(head.buffer, head.byteOffset, 4).getUint32(0);
                if (length < 1 || length > SFTP_MAX_PACKET) {
                    throw new RangeError("The server's SFTP side sent something that is not SFTP "
                                         + "(a shell startup file printing text can do that).");
                }
                if (this.buffered < 4 + length) return;
                this.dispatch(this.take(4 + length).subarray(4));
            }
        } catch (error) {
            this.fail(sftpError(error.message, FX_FAILURE, true));
            this.channel.close().catch(() => null);
        }
    }

    take(count) {
        const out = new Uint8Array(count);
        let at = 0;
        while (at < count) {
            const first = this.chunks[0];
            const need = count - at;
            if (first.length <= need) {
                out.set(first, at);
                at += first.length;
                this.chunks.shift();
            } else {
                out.set(first.subarray(0, need), at);
                this.chunks[0] = first.subarray(need);
                at += need;
            }
        }
        this.buffered -= count;
        return out;
    }

    peek(count) {
        if (this.chunks[0].length >= count) return this.chunks[0].subarray(0, count);
        const joined = this.take(count);
        this.chunks.unshift(joined);
        this.buffered += count;
        return joined;
    }

    dispatch(packet) {
        const reader = new Unpacker(packet);
        const type = reader.u8();
        if (type === FXP.VERSION) {
            this.version = reader.u32();
            while (reader.remaining > 0) {
                const name = reader.text();
                this.extensions[name] = reader.text();
            }
            if (this.onVersion) this.onVersion();
            return;
        }
        const id = reader.u32();
        const entry = this.pending.get(id);
        if (!entry) return;  // the reply to a call that already gave up
        this.pending.delete(id);
        clearTimeout(entry.timer);
        this.release();
        entry.resolve({ type, reader });
    }

    write(bytes) {
        this.writing = this.writing
            .then(() => this.channel.send(Buffer.from(bytes)))
            .catch((error) => this.fail(sftpError(`The SFTP connection failed: ${error.message || error}`,
                                                  FX_CONNECTION_LOST, true)));
    }

    slot() {
        if (this.inFlight < SFTP_IN_FLIGHT) {
            this.inFlight += 1;
            return Promise.resolve();
        }
        // The freed slot passes straight to the next in line.
        return new Promise((resolve) => this.queue.push(resolve));
    }

    release() {
        const next = this.queue.shift();
        if (next) next();
        else this.inFlight = Math.max(0, this.inFlight - 1);
    }

    async send(type, fields) {
        if (this.failure) throw this.failure;
        await this.slot();
        if (this.failure) {
            this.release();
            throw this.failure;
        }
        const id = this.nextId;
        this.nextId = this.nextId >= 0xfffffff0 ? 1 : this.nextId + 1;
        const body = fields ? fields.bytes() : new Uint8Array(0);
        const reply = new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                if (!this.pending.delete(id)) return;
                this.release();
                reject(sftpError("The server did not answer within two minutes.", FX_FAILURE));
            }, SFTP_CALL_MS);
            this.pending.set(id, { resolve, reject, timer });
        });
        this.write(new Packer().u32(body.length + 5).u8(type).u32(id).push(body).bytes());
        return reply;
    }

    async expect(request, want) {
        const { type, reader } = await request;
        if (type === FXP.STATUS) {
            const code = reader.u32();
            const message = reader.remaining >= 4 ? reader.text() : "";
            if (code === FX_OK && want === FXP.STATUS) return reader;
            throw statusError(code, message);
        }
        if (type !== want) {
            throw sftpError(`The server sent an unexpected SFTP reply (${type}, wanted ${want}).`, FX_FAILURE);
        }
        return reader;
    }

    fail(error) {
        if (this.failure) return;
        this.failure = error;
        for (const entry of this.pending.values()) {
            clearTimeout(entry.timer);
            entry.reject(error);
        }
        this.pending.clear();
        for (const next of this.queue.splice(0)) next();  // each sees the failure and throws
        this.handles.clear();
        this.markClosed();
    }

    // -- lifecycle ---------------------------------------------------------

    async init() {
        const versioned = new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(sftpError(
                "The server did not start its SFTP side within a minute.", FX_FAILURE, true)), 60000);
            this.onVersion = () => {
                clearTimeout(timer);
                resolve();
            };
            this.closed.then(() => {
                clearTimeout(timer);
                reject(this.failure);
            });
        });
        this.write(new Packer().u32(5).u8(FXP.INIT).u32(3).bytes());
        await versioned;
        if ("limits@openssh.com" in this.extensions) {
            try {
                const reply = await this.expect(
                    this.send(FXP.EXTENDED, new Packer().string("limits@openssh.com")), FXP.EXTENDED_REPLY);
                reply.u64();  // the most one packet may be
                const most = reply.u64();
                if (most) this.maxRead = Math.max(4096, Math.min(most, SFTP_MAX_READ));
            } catch (error) { /* a refinement, never required */ }
        }
    }

    close() {
        this.fail(sftpError("The SFTP connection was closed.", FX_CONNECTION_LOST, true));
        this.channel.close().catch(() => null);
    }

    // -- what is read ------------------------------------------------------

    async stat(path) {
        return (await this.expect(this.send(FXP.STAT, new Packer().string(path)), FXP.ATTRS)).attrs();
    }

    async lstat(path) {
        return (await this.expect(this.send(FXP.LSTAT, new Packer().string(path)), FXP.ATTRS)).attrs();
    }

    async realpath(path) {
        const reply = await this.expect(this.send(FXP.REALPATH, new Packer().string(path)), FXP.NAME);
        if (reply.u32() < 1) throw sftpError(`The server gave no answer for ${path}.`, FX_FAILURE);
        return reply.text();
    }

    //: [[name, attrs]] in `path`, without `.` and `..`, links followed (a
    //: cluster's group and scratch folders are very often links; one listed
    //: as a file would be a folder nobody could open). A broken link stays a link.
    async listdir(path) {
        const handle = (await this.expect(this.send(FXP.OPENDIR, new Packer().string(path)),
                                          FXP.HANDLE)).string();
        const found = [];
        try {
            for (;;) {
                const { type, reader } = await this.send(FXP.READDIR, new Packer().string(handle));
                if (type === FXP.STATUS) {
                    const code = reader.u32();
                    if (code === FX_EOF) break;
                    throw statusError(code, reader.remaining >= 4 ? reader.text() : "");
                }
                if (type !== FXP.NAME) throw sftpError("The server sent an unexpected listing reply.", FX_FAILURE);
                for (let count = reader.u32(); count > 0; count -= 1) {
                    const name = reader.text();
                    reader.string();  // the `ls -l` line, unused
                    const attrs = reader.attrs();
                    if (name !== "." && name !== "..") found.push([name, attrs]);
                }
            }
        } finally {
            this.closeHandle(handle);
        }
        await Promise.all(found.map(async (entry, index) => {
            if (!isLink(entry[1])) return;
            try {
                found[index] = [entry[0], await this.stat(joinPath(path, entry[0]))];
            } catch (error) { /* broken: stays a link */ }
        }));
        return found;
    }

    //: Bytes [start, end) of the file at `path`; short at the end of the file.
    async read(path, start, end) {
        const from = Math.max(0, Math.floor(Number(start) || 0));
        const to = Math.floor(Number(end));
        if (!(to > from)) return new Uint8Array(0);
        const handle = await this.handleFor(path);
        try {
            return await this.readSpan(handle, from, to);
        } catch (error) {
            if (error.lost || error.fx === FX_NO_SUCH_FILE || error.fx === FX_PERMISSION_DENIED) throw error;
            // A handle closed for being idle, or one the server dropped,
            // answers FAILURE: opened again once before that is believed.
            this.forgetHandle(path, handle);
            return this.readSpan(await this.handleFor(path), from, to);
        }
    }

    async readSpan(handle, start, end) {
        const pieces = new Map();
        let wanted = [];
        for (let offset = start; offset < end; offset += this.maxRead) {
            wanted.push([offset, Math.min(this.maxRead, end - offset)]);
        }
        while (wanted.length) {
            const asked = wanted;
            wanted = [];
            const replies = await Promise.all(asked.map(([offset, length]) => this.send(
                FXP.READ, new Packer().string(handle).u64(offset).u32(length))));
            replies.forEach(({ type, reader }, index) => {
                const [offset, length] = asked[index];
                if (type === FXP.STATUS) {
                    const code = reader.u32();
                    if (code === FX_EOF) return;
                    throw statusError(code, reader.remaining >= 4 ? reader.text() : "");
                }
                if (type !== FXP.DATA) throw sftpError("The server sent an unexpected read reply.", FX_FAILURE);
                const data = reader.string();
                if (!data.length) return;
                pieces.set(offset, data);
                // Fewer bytes than asked is not the end of the file (a server
                // may cap a read below what it advertised): only EOF is.
                if (data.length < length) wanted.push([offset + data.length, length - data.length]);
            });
        }
        const ordered = [];
        let size = 0;
        for (let at = start; pieces.has(at);) {
            const piece = pieces.get(at);
            ordered.push(piece);
            size += piece.length;
            at += piece.length;
        }
        const out = new Uint8Array(size);
        let at = 0;
        for (const piece of ordered) {
            out.set(piece, at);
            at += piece.length;
        }
        return out;
    }

    async handleFor(path) {
        const had = this.handles.get(path);
        if (had) {
            had.used = Date.now();
            this.handles.delete(path);
            this.handles.set(path, had);  // most recently used last
            return had.handle;
        }
        if (this.opening.has(path)) return this.opening.get(path);  // two reads of one file open it once
        const opened = (async () => {
            const handle = (await this.expect(this.send(
                FXP.OPEN, new Packer().string(path).u32(0x1).u32(0)), FXP.HANDLE)).string();
            const now = Date.now();
            this.handles.set(path, { handle, used: now });
            for (const [name, entry] of this.handles) {
                if (name === path) continue;
                if (this.handles.size > SFTP_HANDLE_CAP || now - entry.used > SFTP_HANDLE_IDLE_MS) {
                    this.handles.delete(name);
                    this.closeHandle(entry.handle);
                }
            }
            return handle;
        })();
        this.opening.set(path, opened);
        try {
            return await opened;
        } finally {
            this.opening.delete(path);
        }
    }

    forgetHandle(path, handle) {
        const had = this.handles.get(path);
        if (had && had.handle === handle) this.handles.delete(path);
        this.closeHandle(handle);
    }

    closeHandle(handle) {
        this.send(FXP.CLOSE, new Packer().string(handle)).catch(() => null);
    }
}

//: The server's SFTP side, on a channel of this connection.
async function openSftp(session) {
    const channel = await session.openChannel();
    const request = new CommandRequestMessage();
    // A subsystem request has the exec request's fields: a type, then one
    // string, which here names the subsystem.
    request.requestType = "subsystem";
    request.command = "sftp";
    request.wantReply = true;
    if (!(await channel.request(request))) {
        await channel.close().catch(() => null);
        throw sftpError("The server accepted the sign-in but does not offer SFTP, so there is nothing "
                        + "to read files through. Ask its administrators whether SFTP is enabled.",
                        FX_FAILURE);
    }
    const client = new Sftp(channel);
    try {
        await client.init();
    } catch (error) {
        client.close();
        throw error;
    }
    return client;
}

async function open({ url, protocols, username, hostKey, ask, log = () => {} }) {
    const socket = await openSocket(url, protocols);
    const stream = new WebSocketStream(socket);
    const config = new SshSessionConfiguration();
    config.keepAliveTimeoutInSeconds = KEEPALIVE_S;
    const session = new SshClientSession(config);
    // The library's own account of trouble, in the connection's log: what a
    // failed sign-in or a dropped connection needs to be understood.
    session.trace = (level, eventId, message, error) => {
        if (level === "error" || level === "warning") {
            log(`ssh ${level}: ${message}${error && error.message ? ` (${error.message})` : ""}`);
        }
    };
    let hostKeyError = null;
    let interactive = false;

    session.onAuthenticating((event) => {
        if (event.authenticationType === SshAuthenticationType.serverPublicKey) {
            event.authenticationPromise = (async () => {
                const key = event.publicKey;
                const blob = await key.getPublicKeyBytes();
                const shown = { algorithm: key.keyAlgorithmName, fingerprint: await fingerprint(blob) };
                let accepted = false;
                try {
                    accepted = await hostKey(shown);
                } catch (error) {
                    hostKeyError = error;
                }
                if (!accepted && !hostKeyError) hostKeyError = new Error("The server's key was not accepted.");
                return accepted ? { hostKey: shown } : null;
            })();
            return;
        }
        if (event.authenticationType === SshAuthenticationType.clientInteractive && event.infoRequest) {
            interactive = true;
            const request = event.infoRequest;
            event.authenticationPromise = (async () => {
                const prompts = (request.prompts || []).map((item) => ({ prompt: item.prompt, echo: item.echo }));
                const answers = prompts.length || request.instruction || request.name
                    ? await ask({ name: request.name || "", instruction: request.instruction || "", prompts })
                    : [];
                if (answers === null) return null;
                const response = new AuthenticationInfoResponseMessage();
                response.responses = prompts.map((_, index) => String(answers[index] ?? ""));
                event.infoResponse = response;
                return null;
            })();
        }
    });
    session.onClosed((event) => log(`SSH closed: ${event.reason ?? ""} ${event.message ?? ""}`.trim()));

    try {
        await session.connect(stream);
    } catch (error) {
        throw Object.assign(new Error(`Could not start SSH with the server: ${error.message || error}`),
                            { transport: true });
    }
    if (!(await session.authenticateServer())) {
        await session.close(SshDisconnectReason.hostKeyNotVerifiable).catch(() => null);
        throw hostKeyError || new Error("The server's key could not be verified.");
    }
    // Keyboard-interactive first: it is how a password and then Duo are asked
    // on most clusters. Only if that is refused, the plain password method.
    let signedIn = await session.authenticateClient({ username });
    if (!signedIn && session.isConnected) {
        signedIn = await session.authenticateClient({
            username,
            passwordProvider: async () => {
                const answers = await ask({ name: "", instruction: "",
                                            prompts: [{ prompt: `${username}'s password:`, echo: false }] });
                return answers === null ? null : [username, String(answers[0] ?? "")];
            },
        });
    }
    if (!signedIn) {
        await session.close(SshDisconnectReason.noMoreAuthMethodsAvailable).catch(() => null);
        throw new Error(interactive
            ? "The server did not accept what was typed."
            : "The server refused the sign-in: the website signs in with a password or a code, "
              + "and this server may only accept SSH keys.");
    }

    async function exec(command, { stdin } = {}) {
        const channel = await session.openChannel();
        const lines = [];
        const listeners = [];
        const emit = (line) => {
            lines.push(line);
            for (const listener of listeners) listener(line);
        };
        const out = lineSplitter(emit);
        const err = lineSplitter(emit);
        channel.onDataReceived((data) => {
            out.push(data);
            channel.adjustWindow(data.length);
        });
        channel.onExtendedDataReceived((event) => {
            err.push(event.data);
            channel.adjustWindow(event.data.length);
        });
        let exitCode = null;
        channel.onRequest((event) => {
            const request = event.request;
            if (request && request.requestType === "exit-status") {
                // The status is the request's only field; not every server sends it.
                try { exitCode = request.status ?? null; } catch (error) { /* absent */ }
            }
        });
        const done = new Promise((resolve) => {
            channel.onClosed((event) => {
                out.flush();
                err.flush();
                resolve({ code: event.exitStatus ?? exitCode });
            });
        });
        const request = new CommandRequestMessage();
        request.command = command;
        request.wantReply = true;
        if (!(await channel.request(request))) {
            await channel.close().catch(() => null);
            throw new Error("The server would not run the start command.");
        }
        const encoder = new TextEncoder();
        if (stdin !== undefined) await channel.send(Buffer.from(encoder.encode(stdin)));
        return {
            lines,
            onLine(listener) { listeners.push(listener); },
            write: (text) => channel.send(Buffer.from(encoder.encode(text))),
            close: () => channel.close().catch(() => null),
            done,
        };
    }

    const closed = new Promise((resolve) => session.onClosed(() => resolve()));
    return {
        exec,
        sftp: () => openSftp(session),
        closed,
        close: async () => {
            try {
                await session.close(SshDisconnectReason.byApplication);
            } catch (error) { /* already gone */ }
            try { socket.close(); } catch (error) { /* already gone */ }
        },
    };
}

globalThis.PlexoraSshLib = Object.freeze({ open, fingerprint });

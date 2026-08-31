# One file's bytes, between the browser and whichever machine holds them.
#
# The line browse_routes.py deliberately does not cross. That file's contract
# is in its own header -- "Neither returns file bytes. A path, or a list of
# names and sizes" -- and it is worth keeping, so the two routes that DO move
# bytes live here instead of quietly weakening it next door.
#
# What forced the line: a plugin's Upload button on a session whose data lives
# on a cluster. The user picks a file in the listing picker, and then nothing
# can happen -- the browser has a path on a machine it has no route to, no
# address for, and no token. Same in the other direction for Save: the export
# exists as a blob in a tab, and the folder it belongs in is on the far side.
#
# So the primary relays, and only relays. It never keeps a copy, never reads
# what it forwards, and never decides where anything goes: the path came from a
# picker the user walked, and the folder from one they chose.
#
# `node` names the machine, and "" means this server's own filesystem -- the
# same convention `/list_dir` uses, and the reason the "server" place in
# /data_places (which has `node: None`) needs no special case in the browser.
#
# Trust boundary: exactly the one `/upload_data_file` and `/list_dir` already
# sit behind. One user's server, one token, one account whose files that user
# could have read with `cat` over ssh.

from flask import Response, jsonify, request, stream_with_context

from plexora import app
from plexora.server.utils import file_transfer

#: How much is forwarded per step. The primary holds one of these at a time,
#: whatever the size of the file going past it.
CHUNK = 1 << 16


@app.route('/fetch_file', methods=['POST'])
def fetch_file():
    """Send one file to the browser, from here or from a node.

    The answer is the file and nothing else, streamed, so a 2 GB image does not
    become 2 GB of this process. `X-Plexora-File-Name` carries what to call it,
    because the browser has to build a `File` out of these bytes and the path
    it asked with may not be the name it should use.
    """
    payload = request.get_json(silent=True) or {}
    node = (payload.get('node') or '').strip()
    raw = payload.get('path') or ''
    if node:
        return _fetch_from_node(node, raw)

    try:
        path, size, mimetype, name = file_transfer.open_read(raw)
    except file_transfer.TransferError as exc:
        return jsonify(error=str(exc)), 400

    def chunks():
        # Opened inside the generator: Flask returns this response before a
        # byte is read, and an `open` above would hold a handle across every
        # early return the error branches take.
        with open(path, 'rb') as handle:
            while True:
                piece = handle.read(CHUNK)
                if not piece:
                    return
                yield piece

    return Response(stream_with_context(chunks()), mimetype=mimetype, headers={
        'Content-Length': str(size),
        'X-Plexora-File-Name': name,
    })


def _fetch_from_node(name, path):
    """The same file, from the far side, forwarded chunk by chunk.

    Failures are kept apart the way `_list_dir_on_node` keeps them (see
    browse_routes.py): "no such file" and "the node is not answering" are
    different sentences to read and different things to do next, and collapsing
    both into 502 puts a gateway error in front of somebody's typo.
    """
    from plexora import nodes as node_api
    from plexora.server.providers.base import ResourceError, ResourceUnavailable
    from plexora.server.providers.http import FILE_NAME_HEADER

    try:
        upstream = node_api.open_file_on_node(name, path)
    except KeyError as exc:
        return jsonify(error=str(exc).strip("'\"")), 400
    except ResourceUnavailable as exc:
        return jsonify(error=str(exc)), 503
    except ResourceError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 502

    def chunks():
        try:
            for piece in upstream.stream(CHUNK, decode_content=True):
                yield piece
        finally:
            # Guaranteed by `stream_with_context` even when the browser hangs
            # up mid-download, which on a file this size is an ordinary way for
            # one to end. Without it the connection stays checked out of the
            # pool and the next node call waits on it.
            upstream.release_conn()

    headers = {'X-Plexora-File-Name':
               upstream.headers.get(FILE_NAME_HEADER) or _tail(path)}
    length = upstream.headers.get('Content-Length')
    if length:
        headers['Content-Length'] = length
    return Response(
        stream_with_context(chunks()),
        mimetype=upstream.headers.get('Content-Type') or 'application/octet-stream',
        headers=headers)


def _tail(path):
    """The last segment of a path from a machine whose separator is unknown.

    Only a fallback for a node that answered without the name header, which a
    matched build never does. Both separators are tried because the far side
    may be a Windows box and this one may not.
    """
    text = str(path or '').replace('\\', '/').rstrip('/')
    return text.rsplit('/', 1)[-1] or 'download'


@app.route('/put_file', methods=['POST'])
def put_file():
    """Write one file, here or on a node, from a multipart upload.

    Multipart rather than a raw body deliberately: Werkzeug spools a large part
    to a temp file on disk, so a 300 MB export travels through this process
    without ever being 300 MB of it. The alternative -- reading the raw stream
    -- means whatever buffers first.

    An existing file comes back as 409 with `exists`, never replaced. The user
    picked that name, and whether it should overwrite last week's export is
    their answer to give.
    """
    upload = request.files.get('file')
    if upload is None:
        return jsonify(error="No file was sent."), 400

    node = (request.form.get('node') or '').strip()
    directory = request.form.get('dir') or ''
    name = request.form.get('name') or upload.filename or ''
    overwrite = str(request.form.get('overwrite') or '').strip().lower() in (
        '1', 'true', 'yes')

    if node:
        return _put_on_node(node, directory, name, upload, overwrite)

    try:
        path, written = file_transfer.write_file(
            directory, name, upload.stream, overwrite=overwrite)
    except file_transfer.TransferError as exc:
        return jsonify(error=str(exc), exists=exc.exists), 409

    return jsonify(success=True, path=str(path), bytes=written)


def _put_on_node(name, directory, filename, upload, overwrite):
    """The same write, on the far side, with the same refusals."""
    from plexora import nodes as node_api
    from plexora.server.providers.base import ResourceError, ResourceUnavailable

    # Werkzeug knows the length of the spooled part, and sending it spares the
    # node a chunked body -- which every WSGI server handles, but not all of
    # them without buffering it whole first.
    size = getattr(upload, 'content_length', None) or None
    if not size:
        try:
            upload.stream.seek(0, 2)
            size = upload.stream.tell()
            upload.stream.seek(0)
        except (OSError, AttributeError):
            size = None

    try:
        answer = node_api.write_file_on_node(
            name, directory, filename, upload.stream, size=size,
            overwrite=overwrite)
    except KeyError as exc:
        return jsonify(error=str(exc).strip("'\"")), 400
    except ResourceUnavailable as exc:
        return jsonify(error=str(exc)), 503
    except ResourceError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 502

    if not answer.get('success'):
        return jsonify(error=answer.get('error') or "The node refused the write.",
                       exists=bool(answer.get('exists'))), 409
    return jsonify(success=True, path=answer.get('path'),
                   bytes=answer.get('bytes'))

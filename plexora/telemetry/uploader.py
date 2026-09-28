"""The only module that opens a socket, on the only thread allowed to.

One daemon thread wakes a minute after start and then every
`upload_interval_s` (an hour unless the server says otherwise), freezes the
closed hourly windows into a batch, and posts it. Connectivity is never
probed: the POST *is* the probe, with five-second timeouts, which is the right
answer on a compute node where a probe to one host says nothing about another.
A failure keeps the batch and backs off -- 1 min, 5 min, 15 min, 1 h, 4 h,
24 h, with jitter -- and the backoff is persisted, so a restart does not
reset it.

Outcomes:

| response                   | batch         | backoff             |
|----------------------------|---------------|---------------------|
| 2xx                        | delete        | reset; apply config |
| 401                        | keep          | none; re-register   |
| 403 (install mismatch)     | delete        | none; re-register   |
| 400 / 404 / 415 / 422      | delete        | none (3 in a row pauses a day) |
| 413                        | delete        | none; halve batch size |
| 429 / 503                  | keep          | Retry-After, else next step |
| other 5xx, timeout, DNS    | keep          | next step           |

No secret ships with the client. `POST /v1/telemetry/register` exchanges the
random install id for an HMAC install token bound to it; that token is what
every upload carries, and it is not a licence of any kind.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.parse
import urllib.request

from plexora.telemetry import batch, config, identity, redact, schema

REGISTER_PATH = "/v1/telemetry/register"
EVENTS_PATH = "/v1/telemetry/events"

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 5.0
FIRST_ATTEMPT_SECONDS = 60.0
DEFAULT_INTERVAL = 3600.0
MIN_INTERVAL = 300.0
MAX_INTERVAL = 86400.0
BACKOFF_STEPS = (60, 300, 900, 3600, 4 * 3600, 24 * 3600)
MAX_BATCHES_PER_ATTEMPT = 6
MAX_RETRY_AFTER = 24 * 3600

_thread = None
_lock = threading.Lock()
_wake = threading.Event()
_stopping = threading.Event()


# -- the thread ---------------------------------------------------------------


def start(telemetry) -> bool:
    """Start the uploader for a serving process. Idempotent."""
    global _thread
    if not config.endpoint() or config.under_pytest():
        return False
    queue = telemetry.queue
    if queue is None:
        return False
    server = queue.get_state("server", {}) or {}
    try:
        sample = float(server.get("sample", 1.0))
    except (TypeError, ValueError):
        sample = 1.0
    if sample < 1.0 and random.random() >= max(0.0, sample):
        config.debug("this process is not in the upload sample")
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return True
        _stopping.clear()
        _thread = threading.Thread(target=_run, args=(telemetry,),
                                   name="plexora-telemetry-upload", daemon=True)
        _thread.start()
    return True


def stop(timeout=2.0):
    global _thread
    _stopping.set()
    _wake.set()
    thread = _thread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=timeout)
    _thread = None
    _wake.clear()


def wake():
    _wake.set()


def _run(telemetry):
    wait = FIRST_ATTEMPT_SECONDS
    while not _stopping.is_set():
        if _wake.wait(wait):
            _wake.clear()
        if _stopping.is_set():
            return
        try:
            if telemetry.enabled:
                upload_once(telemetry)
        except Exception:
            config.debug("upload attempt raised")
        wait = interval(telemetry)


def interval(telemetry) -> float:
    queue = telemetry.queue
    server = (queue.get_state("server", {}) if queue else {}) or {}
    try:
        value = float(server.get("upload_interval_s") or DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL
    return min(MAX_INTERVAL, max(MIN_INTERVAL, value))


def final_attempt(telemetry, deadline):
    """One upload of everything, open window included, inside `deadline`."""
    remaining = deadline - time.monotonic()
    if remaining <= 0.1 or not config.endpoint():
        return
    stop(timeout=min(0.5, remaining / 4))
    helper = threading.Thread(target=_quiet_upload, args=(telemetry,),
                              name="plexora-telemetry-final", daemon=True)
    helper.start()
    helper.join(timeout=max(0.05, deadline - time.monotonic()))


def _quiet_upload(telemetry):
    try:
        upload_once(telemetry, include_open=True)
    except Exception:
        pass


# -- one attempt --------------------------------------------------------------


def effective_mode(telemetry, queue) -> str:
    if telemetry.enabled:
        return telemetry.mode
    return config.resolve(server=queue.get_state("server", {})).mode


def upload_once(telemetry, *, include_open=False, force=False) -> str:
    """Send what is waiting. Returns a word for what happened: `sent`,
    `nothing`, `backoff`, `off`, `no_endpoint`, `failed`, `unregistered`,
    `rejected`, `paused`."""
    base = config.endpoint()
    if not base:
        return "no_endpoint"
    queue = telemetry.queue or telemetry.open_queue()
    if queue is None:
        return "failed"
    mode = effective_mode(telemetry, queue)
    if mode == config.OFF:
        return "off"
    state = queue.get_state("uploader", {}) or {}
    now = time.time()
    if not force and now < float(state.get("backoff_until") or 0):
        return "backoff"

    client = telemetry.client_block(mode)
    names = redact.personal_names()
    max_events = int(state.get("max_events") or schema.MAX_EVENTS_PER_BATCH)

    def dropped(n):
        telemetry._health["dropped"] += n

    build = batch.builder(client, mode, names, on_dropped=dropped)
    outcome = "nothing"
    for _ in range(MAX_BATCHES_PER_ATTEMPT):
        taken = queue.take_batch(build, include_open=include_open,
                                 max_records=max(8, max_events),
                                 max_rows=max(64, max_events * 10))
        if taken is None:
            break
        batch_id, payload = taken
        token = _token(telemetry, queue, client)
        if token is None:
            queue.unack(batch_id)
            _backoff(queue, state)
            return "unregistered"
        status, answer, headers = _post(base + EVENTS_PATH, payload, token=token, gzip=True)
        outcome = _handle(queue, state, batch_id, status, answer, headers, telemetry)
        state = queue.get_state("uploader", {}) or {}
        if outcome != "sent":
            break
    _record(queue, outcome)
    return outcome


def _handle(queue, state, batch_id, status, answer, headers, telemetry) -> str:
    if status is not None and 200 <= status < 300:
        queue.ack(batch_id)
        _clear_backoff(queue, state)
        if isinstance(answer, dict):
            if answer.get("rotate"):
                queue.set_state("token", None)
            _apply_config(queue, answer.get("config"), telemetry)
        return "sent"
    if status == 401:
        queue.unack(batch_id)
        queue.set_state("token", None)
        return "unregistered"
    if status == 403:
        queue.ack(batch_id)
        queue.set_state("token", None)
        return "rejected"
    if status in (400, 404, 415, 422):
        queue.ack(batch_id)
        streak = int(state.get("rejected_streak") or 0) + 1
        update = {**state, "rejected_streak": streak}
        if streak >= 3:
            server = queue.get_state("server", {}) or {}
            queue.set_state("server", {**server, "disabled_until": time.time() + 86400})
            update["rejected_streak"] = 0
            telemetry.reconfigure()
        queue.set_state("uploader", update)
        return "rejected"
    if status == 413:
        queue.ack(batch_id)
        max_events = int(state.get("max_events") or schema.MAX_EVENTS_PER_BATCH)
        queue.set_state("uploader", {**state, "max_events": max(8, max_events // 2)})
        return "rejected"
    queue.unack(batch_id)
    retry_after = _retry_after(headers)
    _backoff(queue, state, retry_after)
    return "failed"


def _record(queue, outcome):
    state = queue.get_state("uploader", {}) or {}
    state["last_outcome"] = outcome
    state["last_attempt"] = time.time()
    if outcome == "sent":
        state["last_upload"] = time.time()
    state["attempts"] = int(state.get("attempts") or 0) + (outcome not in ("nothing", "backoff"))
    queue.set_state("uploader", state)


def _backoff(queue, state, retry_after=None):
    failures = int(state.get("failures") or 0) + 1
    if retry_after is not None:
        delay = min(MAX_RETRY_AFTER, max(1.0, retry_after))
    else:
        step = BACKOFF_STEPS[min(failures, len(BACKOFF_STEPS)) - 1]
        delay = step * random.uniform(0.8, 1.2)
    queue.set_state("uploader", {**state, "failures": failures,
                                 "backoff_until": time.time() + delay})


def _clear_backoff(queue, state):
    queue.set_state("uploader", {**state, "failures": 0, "backoff_until": 0,
                                 "rejected_streak": 0})


def _retry_after(headers):
    value = (headers or {}).get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime

        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except Exception:
        return None


def _apply_config(queue, config_block, telemetry):
    """Apply what the server just said, now rather than at the next start."""
    if not isinstance(config_block, dict):
        return
    clean = {}
    if config_block.get("level_max") in schema.MODES:
        clean["level_max"] = config_block["level_max"]
    for key in ("upload_interval_s", "sample", "disabled_until"):
        value = config_block.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            clean[key] = float(value)
    queue.set_state("server", clean)
    telemetry.reconfigure()


# -- registration -------------------------------------------------------------


def _token(telemetry, queue, client):
    record = queue.get_state("token", {}) or {}
    token = record.get("token")
    expires = float(record.get("expires") or 0)
    if token and record.get("install_id") == client.get("install_id") and (
            not expires or time.time() < expires - 86400):
        return token
    return register(telemetry, queue, client)


def register(telemetry, queue, client):
    install = client.get("install_id") or identity.install_id(mint=True)
    if not install:
        return None
    body = json.dumps({"schema": schema.SCHEMA_VERSION, "install_id": install,
                       "client": {k: v for k, v in client.items()
                                  if k not in ("install_id", "session_id")}},
                      separators=(",", ":")).encode("utf-8")
    status, answer, _headers = _post(config.endpoint() + REGISTER_PATH, body)
    if status != 200 or not isinstance(answer, dict) or not answer.get("install_token"):
        return None
    queue.set_state("token", {"token": answer["install_token"], "install_id": install,
                              "expires": answer.get("expires") or 0})
    _apply_config(queue, answer.get("config"), telemetry)
    return answer["install_token"]


# -- HTTP ---------------------------------------------------------------------

_pools = {}


def _pool(url):
    import urllib3

    parsed = urllib.parse.urlsplit(url)
    proxies = urllib.request.getproxies()
    proxy = None
    try:
        if not urllib.request.proxy_bypass(parsed.hostname or ""):
            proxy = proxies.get(parsed.scheme)
    except Exception:
        proxy = None
    key = proxy or ""
    pool = _pools.get(key)
    if pool is None:
        timeout = urllib3.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT)
        if proxy:
            pool = urllib3.ProxyManager(proxy, retries=False, timeout=timeout)
        else:
            pool = urllib3.PoolManager(retries=False, timeout=timeout)
        _pools[key] = pool
    return pool


def _user_agent():
    from plexora.telemetry.environment import plexora_version

    return f"plexora-telemetry/{plexora_version()}"


def _post(url, body, *, token=None, gzip=False):
    """`(status or None, parsed JSON or None, lower-cased headers)`."""
    headers = {"Content-Type": "application/json", "User-Agent": _user_agent()}
    if gzip:
        headers["Content-Encoding"] = "gzip"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = _pool(url).request("POST", url, body=body, headers=headers,
                                      redirect=False, preload_content=True)
    except Exception as exc:  # offline is the normal case, not an error
        config.debug(f"POST failed: {type(exc).__name__}")
        return None, None, {}
    answer = None
    try:
        data = response.data[:65536]
        answer = json.loads(data.decode("utf-8")) if data else None
    except Exception:
        answer = None
    lowered = {k.lower(): v for k, v in response.headers.items()}
    return response.status, answer, lowered


# -- status ---------------------------------------------------------------------


def status(queue) -> dict:
    state = (queue.get_state("uploader", {}) if queue else {}) or {}
    server = (queue.get_state("server", {}) if queue else {}) or {}
    token = (queue.get_state("token", {}) if queue else {}) or {}
    return {
        "endpoint": config.endpoint() or None,
        "registered": bool(token.get("token")),
        "last_upload": state.get("last_upload"),
        "last_attempt": state.get("last_attempt"),
        "last_outcome": state.get("last_outcome"),
        "failures": int(state.get("failures") or 0),
        "backoff_until": state.get("backoff_until") or None,
        "server": server,
        "running": bool(_thread is not None and _thread.is_alive()),
    }

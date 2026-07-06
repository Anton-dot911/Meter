"""metered_client() + record() — faithful mirror of packages/meter-ts/src/meter.ts.

A proxy around anthropic.Anthropic that observes every messages.create call
(non-streaming and stream=True) and records it fire-and-forget. The wrapper only
observes: the SDK response, stream events, and thrown errors pass through
unchanged (Hard Rule 1 — meter must NEVER break the host application).
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional

from .pricing import compute_cost
from .transport import default_transport
from .types import MeterRecord, MeterStatus, Transport

if TYPE_CHECKING:  # anthropic is a type-only / peer dependency — never imported at runtime.
    import anthropic


# ---------------------------------------------------------------------------
# Fire-and-forget dispatch (Hard Rule 1). Recording runs on a background daemon
# thread so a slow or failing transport never adds latency to — or breaks — the
# host's call. This mirrors meter-ts, whose dispatch never awaits transport.send.
# ---------------------------------------------------------------------------

_warn_lock = threading.Lock()
_warned = False


def _warn_once(err: object) -> None:
    global _warned
    with _warn_lock:
        if _warned:
            return
        _warned = True
    msg = str(err) or err.__class__.__name__
    print(
        f"meter: failed to record llm call (further transport errors will be silent): {msg}",
        file=sys.stderr,
    )


def _reset_warn_once() -> None:
    """@internal test hook."""
    global _warned
    _warned = False


def _dispatch(transport: Transport, record: MeterRecord) -> None:
    def _run() -> None:
        try:
            transport.send(record)
        except Exception as err:  # noqa: BLE001 - fire-and-forget: swallow everything
            _warn_once(err)

    try:
        threading.Thread(target=_run, name="meter-dispatch", daemon=True).start()
    except Exception as err:  # noqa: BLE001 - even thread creation must not break the host
        _warn_once(err)


_lazy_default: Optional[Transport] = None
_lazy_lock = threading.Lock()


def _get_default_transport() -> Transport:
    global _lazy_default
    if _lazy_default is None:
        with _lazy_lock:
            if _lazy_default is None:
                _lazy_default = default_transport()
    return _lazy_default


def _now_iso() -> str:
    # Matches JS new Date().toISOString(): millisecond precision, trailing "Z".
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Manual record() hook (used by the host llm client for status: "retried").
# ---------------------------------------------------------------------------


def record(**fields: Any) -> None:
    """Write one record manually; ``ts`` is stamped here. Fire-and-forget.

    Mirrors the TS ``record()`` export. An optional ``transport`` keyword selects
    the transport (defaults to SupabaseTransport(env) + JsonlFallback); every
    other keyword is a record field per spec/record.schema.json.
    """
    transport: Optional[Transport] = fields.pop("transport", None)
    payload: MeterRecord = {"ts": _now_iso(), **fields}  # type: ignore[typeddict-item]
    _dispatch(transport or _get_default_transport(), payload)


# ---------------------------------------------------------------------------
# metered_client — proxy around the Anthropic SDK client.
# ---------------------------------------------------------------------------


@dataclass
class _Config:
    project: str
    component: str
    transport: Transport
    trace_id: "str | Callable[[], Optional[str]] | None"


def metered_client(
    client: "anthropic.Anthropic",
    *,
    project: str,
    component: str,
    transport: Optional[Transport] = None,
    trace_id: "str | Callable[[], Optional[str]] | None" = None,
) -> "anthropic.Anthropic":
    """Wrap an Anthropic client so every messages.create call (non-streaming and
    stream=True) is recorded. The wrapper only observes; responses/streams/errors
    pass through unchanged.
    """
    cfg = _Config(
        project=project,
        component=component,
        transport=transport or _get_default_transport(),
        trace_id=trace_id,
    )
    return _ClientProxy(client, cfg)  # type: ignore[return-value]


class _ClientProxy:
    """Delegates everything to the wrapped client except ``messages``."""

    def __init__(self, client: Any, cfg: _Config) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_cfg", cfg)

    def __getattr__(self, name: str) -> Any:
        client = object.__getattribute__(self, "_client")
        if name == "messages":
            return _MessagesProxy(client.messages, object.__getattribute__(self, "_cfg"))
        return getattr(client, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_client"), name, value)


class _MessagesProxy:
    """Intercepts ``create``; delegates every other member to the real resource."""

    def __init__(self, messages: Any, cfg: _Config) -> None:
        object.__setattr__(self, "_messages", messages)
        object.__setattr__(self, "_cfg", cfg)

    def create(self, *args: Any, **kwargs: Any) -> Any:
        return _metered_create(
            object.__getattribute__(self, "_messages"),
            object.__getattribute__(self, "_cfg"),
            args,
            kwargs,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_messages"), name)


def _metered_create(messages: Any, cfg: _Config, args: tuple, kwargs: dict) -> Any:
    ts = _now_iso()
    start = time.monotonic()
    stream_requested = bool(kwargs.get("stream"))
    req_model = kwargs.get("model")

    def emit(
        model: Optional[str],
        tokens_in: Optional[int],
        tokens_out: Optional[int],
        status: MeterStatus,
        error_type: Optional[str],
        request_id: Optional[str],
    ) -> None:
        try:
            resolved = model or req_model or "unknown"
            latency_ms = max(0, round((time.monotonic() - start) * 1000))
            _dispatch(
                cfg.transport,
                {
                    "ts": ts,
                    "project": cfg.project,
                    "component": cfg.component,
                    "model": resolved,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "cost_usd": compute_cost(resolved, tokens_in, tokens_out),
                    "latency_ms": latency_ms,
                    "status": status,
                    "error_type": error_type,
                    "request_id": request_id,
                    "trace_id": _resolve_trace_id(cfg),
                },
            )
        except Exception as err:  # noqa: BLE001 - recording must never break the host
            _warn_once(err)

    try:
        result = messages.create(*args, **kwargs)
    except Exception as err:  # noqa: BLE001 - observe, then rethrow unchanged
        emit(req_model, None, None, "error", _classify_error(err), _request_id_of(err))
        raise

    if stream_requested:
        return _MeteredStream(result, emit)

    usage = _get(result, "usage")
    emit(
        _get(result, "model"),
        _get(usage, "input_tokens"),
        _get(usage, "output_tokens"),
        "ok",
        None,
        _get(result, "_request_id"),
    )
    return result


def _resolve_trace_id(cfg: _Config) -> Optional[str]:
    try:
        value = cfg.trace_id() if callable(cfg.trace_id) else cfg.trace_id
        return value if value is not None else None
    except Exception:  # noqa: BLE001
        return None


def _request_id_of(source: object) -> Optional[str]:
    request_id = getattr(source, "request_id", None)
    return request_id if isinstance(request_id, str) else None


def _classify_error(err: object) -> str:
    """Map SDK/network errors onto the spec's error_type vocabulary."""
    status = getattr(err, "status_code", None)
    if status is None:
        status = getattr(err, "status", None)
    name = err.__class__.__name__
    code = getattr(err, "code", None)
    if "Timeout" in name or code == "ETIMEDOUT" or status == 408:
        return "timeout"
    if status == 429:
        return "rate_limit"
    if status in (400, 422):
        return "validation"
    return "api_error"


# ---------------------------------------------------------------------------
# Streaming: observe events as the consumer iterates; record on stream end.
# ---------------------------------------------------------------------------


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dict (test fakes) or an object (real SDK types)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _coalesce(new: Any, old: Any) -> Any:
    return new if new is not None else old


def _stream_request_id(stream: Any) -> Optional[str]:
    # A raw SDK Stream carries no _request_id; the id lives on the `request-id`
    # response header (stream.response). Fall back to _request_id, which is set on
    # the SDK's higher-level MessageStream helper and on test fakes.
    try:
        response = getattr(stream, "response", None)
        if response is not None:
            rid = response.headers.get("request-id")
            if rid:
                return rid
    except Exception:  # noqa: BLE001
        pass
    rid = _get(stream, "_request_id")
    return rid if isinstance(rid, str) else None


class _MeteredStream:
    """Wraps the SDK stream: passes every event through unchanged, records on end.

    Records exactly once — on normal completion, early break (GeneratorExit),
    exception, or context-manager exit — whichever happens first.
    """

    def __init__(self, stream: Any, emit: Callable[..., None]) -> None:
        object.__setattr__(self, "_stream", stream)
        object.__setattr__(self, "_emit", emit)
        object.__setattr__(self, "_ended", False)
        object.__setattr__(self, "_model", None)
        object.__setattr__(self, "_tokens_in", None)
        object.__setattr__(self, "_tokens_out", None)
        object.__setattr__(self, "_request_id", _stream_request_id(stream))

    def _observe(self, event: Any) -> None:
        try:
            etype = _get(event, "type")
            if etype == "message_start":
                message = _get(event, "message")
                usage = _get(message, "usage")
                self._model = _coalesce(_get(message, "model"), self._model)
                self._tokens_in = _coalesce(_get(usage, "input_tokens"), self._tokens_in)
                self._tokens_out = _coalesce(_get(usage, "output_tokens"), self._tokens_out)
            elif etype == "message_delta":
                # Cumulative output token count; the final message_delta carries the total.
                usage = _get(event, "usage")
                self._tokens_out = _coalesce(_get(usage, "output_tokens"), self._tokens_out)
        except Exception:  # noqa: BLE001 - observation must never affect iteration
            pass

    def _finish(self, err: Optional[BaseException] = None) -> None:
        if self._ended:
            return
        self._ended = True
        self._emit(
            self._model,
            self._tokens_in,
            self._tokens_out,
            "ok" if err is None else "error",
            None if err is None else _classify_error(err),
            self._request_id,
        )

    def __iter__(self):
        stream = object.__getattribute__(self, "_stream")
        try:
            for event in stream:
                self._observe(event)
                yield event
        except GeneratorExit:
            # Consumer stopped early (break): record what we saw, then close the SDK stream.
            self._finish()
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
            raise
        except Exception as err:  # noqa: BLE001 - mid-stream failure: record then rethrow
            self._finish(err)
            raise
        else:
            self._finish()

    def __enter__(self):
        enter = getattr(object.__getattribute__(self, "_stream"), "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, *exc: Any) -> Any:
        stream = object.__getattribute__(self, "_stream")
        try:
            exit_ = getattr(stream, "__exit__", None)
            return exit_(*exc) if callable(exit_) else None
        finally:
            self._finish()

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_stream"), name)

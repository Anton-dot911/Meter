"""metered_client() proxy + record() manual hook — mirror of meter.ts.

The wrapper only observes: the SDK response, stream events and thrown errors
pass through unchanged (Hard Rule 2). Recording is fire-and-forget — a transport
failure never throws or alters the wrapped call (Hard Rule 1).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Iterator, Optional

from .pricing import compute_cost
from .transport import default_transport
from .types import MeterRecord, TraceId, Transport

# ---------------------------------------------------------------------------
# Fire-and-forget dispatch (Hard Rule 1: meter must NEVER break the host app).
#
# The Anthropic Python SDK and supabase-py are synchronous, so we record inline
# after the response is already in hand and swallow every error. The record
# therefore never precedes the response and can never affect it. (meter.ts is
# non-blocking because JS transports are promise-based; here the observable
# contract — never throw, never alter the response — is what we mirror.)
# ---------------------------------------------------------------------------

_warned = False


def _warn_once(err: object) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    print(
        "meter: failed to record llm call (further transport errors will be silent): "
        f"{err}",
        file=sys.stderr,
    )


def _reset_warn_once() -> None:
    """@internal test hook."""
    global _warned
    _warned = False


def _dispatch(transport: Transport, record: MeterRecord) -> None:
    try:
        transport.send(record)
    except BaseException as err:  # noqa: BLE001 - fire-and-forget swallows everything
        _warn_once(err)


_lazy_default: Optional[Transport] = None


def _get_default_transport() -> Transport:
    global _lazy_default
    if _lazy_default is None:
        _lazy_default = default_transport()
    return _lazy_default


def _now_iso() -> str:
    # Millisecond precision + "Z", matching JS Date.toISOString().
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# Manual record() hook (used by the host llm client for status "retried").
# ---------------------------------------------------------------------------


def record(**fields: Any) -> None:
    """Write one record manually; ``ts`` is stamped here. Fire-and-forget.

    Same semantics as the TS ``record()`` export. ``transport`` is an optional
    keyword (defaults to SupabaseTransport(env) + JsonlFallback); every other
    keyword is a record field supplied by the caller.
    """
    transport = fields.pop("transport", None) or _get_default_transport()
    rec: MeterRecord = {"ts": _now_iso(), **fields}  # type: ignore[typeddict-item]
    _dispatch(transport, rec)


# ---------------------------------------------------------------------------
# metered_client — proxy around the Anthropic SDK client.
# ---------------------------------------------------------------------------


class _Config:
    __slots__ = ("project", "component", "transport", "trace_id")

    def __init__(self, project: str, component: str, transport: Transport, trace_id: TraceId):
        self.project = project
        self.component = component
        self.transport = transport
        self.trace_id = trace_id


def metered_client(
    client: Any,
    *,
    project: str,
    component: str,
    transport: Optional[Transport] = None,
    trace_id: TraceId = None,
) -> Any:
    """Wrap an Anthropic client so every ``messages.create`` call (non-streaming
    and ``stream=True``) is recorded. Everything else is delegated untouched.
    """
    cfg = _Config(project, component, transport or _get_default_transport(), trace_id)
    return _ClientProxy(client, cfg)


class _ClientProxy:
    """Proxy that swaps in a metered ``messages`` and delegates all else."""

    def __init__(self, client: Any, cfg: _Config) -> None:
        self.__dict__["_client"] = client
        self.__dict__["_messages"] = _MessagesProxy(client.messages, cfg)

    def __getattr__(self, name: str) -> Any:
        if name == "messages":
            return self.__dict__["_messages"]
        return getattr(self.__dict__["_client"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self.__dict__["_client"], name, value)


class _MessagesProxy:
    """Intercepts ``create``; delegates ``stream``, ``count_tokens``, etc."""

    def __init__(self, messages: Any, cfg: _Config) -> None:
        self.__dict__["_messages"] = messages
        self.__dict__["_cfg"] = cfg

    def create(self, *args: Any, **kwargs: Any) -> Any:
        return _metered_create(self.__dict__["_messages"], args, kwargs, self.__dict__["_cfg"])

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_messages"], name)


def _metered_create(messages: Any, args: tuple, kwargs: dict, cfg: _Config) -> Any:
    ts = _now_iso()
    start = monotonic()
    streaming = kwargs.get("stream") is True
    param_model = kwargs.get("model")

    def emit(
        *,
        model: Optional[str],
        tokens_in: Optional[int],
        tokens_out: Optional[int],
        status: str,
        error_type: Optional[str],
        request_id: Optional[str],
    ) -> None:
        try:
            resolved = model or param_model or "unknown"
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
                    "latency_ms": max(0, round((monotonic() - start) * 1000)),
                    "status": status,
                    "error_type": error_type,
                    "request_id": request_id,
                    "trace_id": _resolve_trace_id(cfg),
                },
            )
        except BaseException as err:  # noqa: BLE001
            _warn_once(err)

    try:
        result = messages.create(*args, **kwargs)
    except BaseException as err:  # observe, classify, re-raise unchanged
        emit(
            model=param_model,
            tokens_in=None,
            tokens_out=None,
            status="error",
            error_type=_classify_error(err),
            request_id=_request_id_of(err),
        )
        raise

    if streaming:
        return _MeteredStream(result, cfg, emit, _stream_request_id(result))

    emit(
        model=getattr(result, "model", None),
        tokens_in=_usage_get(result, "input_tokens"),
        tokens_out=_usage_get(result, "output_tokens"),
        status="ok",
        error_type=None,
        request_id=getattr(result, "_request_id", None),
    )
    return result


def _resolve_trace_id(cfg: _Config) -> Optional[str]:
    try:
        trace = cfg.trace_id() if callable(cfg.trace_id) else cfg.trace_id
        return trace if trace is not None else None
    except Exception:
        return None


def _request_id_of(source: object) -> Optional[str]:
    rid = getattr(source, "request_id", None)
    return rid if isinstance(rid, str) else None


def _usage_get(obj: object, key: str) -> Optional[int]:
    usage = getattr(obj, "usage", None)
    if usage is None:
        return None
    return getattr(usage, key, None)


def _classify_error(err: object) -> str:
    """Map SDK/network errors onto the spec's error_type vocabulary."""
    status = getattr(err, "status_code", None)
    if status is None:
        status = getattr(err, "status", None)
    name = type(err).__name__
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


def _stream_request_id(stream: object) -> Optional[str]:
    """A raw Stream carries no _request_id; the id lives on the ``request-id``
    response header (``stream.response.headers``). Prefer that, then fall back to
    an explicit _request_id (set on the SDK's higher-level MessageStream)."""
    resp = getattr(stream, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is not None:
        try:
            hid = headers.get("request-id")
            if isinstance(hid, str):
                return hid
        except Exception:
            pass
    rid = getattr(stream, "_request_id", None)
    return rid if isinstance(rid, str) else None


class _MeteredStream:
    """Wraps an SDK Stream: yields every event unchanged, records once when the
    stream ends (exhausted, error, or early break). Delegates other attributes
    (``response``, ``close``, ``text_stream``, context-manager use) to the inner
    stream so the host sees the same object surface.
    """

    def __init__(self, inner: Any, cfg: _Config, emit: Any, seed_request_id: Optional[str]) -> None:
        self.__dict__["_inner"] = inner
        self.__dict__["_cfg"] = cfg
        self.__dict__["_emit"] = emit
        self.__dict__["_ended"] = False
        self.__dict__["_usage"] = {
            "model": None,
            "tokens_in": None,
            "tokens_out": None,
            "request_id": seed_request_id,
        }

    # -- observation -------------------------------------------------------
    def _observe(self, event: Any) -> None:
        try:
            etype = getattr(event, "type", None)
            usage = self.__dict__["_usage"]
            if etype == "message_start":
                message = getattr(event, "message", None)
                if message is not None:
                    usage["model"] = getattr(message, "model", None) or usage["model"]
                    mu = getattr(message, "usage", None)
                    if mu is not None:
                        usage["tokens_in"] = _coalesce(
                            getattr(mu, "input_tokens", None), usage["tokens_in"]
                        )
                        usage["tokens_out"] = _coalesce(
                            getattr(mu, "output_tokens", None), usage["tokens_out"]
                        )
            elif etype == "message_delta":
                # Cumulative output token count; the final message_delta carries the total.
                du = getattr(event, "usage", None)
                if du is not None:
                    usage["tokens_out"] = _coalesce(
                        getattr(du, "output_tokens", None), usage["tokens_out"]
                    )
        except Exception:
            # Observation must never affect the host's iteration.
            pass

    def _finish(self, err: Optional[BaseException]) -> None:
        if self.__dict__["_ended"]:
            return
        self.__dict__["_ended"] = True
        usage = self.__dict__["_usage"]
        self.__dict__["_emit"](
            model=usage["model"],
            tokens_in=usage["tokens_in"],
            tokens_out=usage["tokens_out"],
            status="ok" if err is None else "error",
            error_type=None if err is None else _classify_error(err),
            request_id=usage["request_id"],
        )

    # -- iteration ---------------------------------------------------------
    def __iter__(self) -> Iterator[Any]:
        inner = self.__dict__["_inner"]
        try:
            for event in inner:
                self._observe(event)
                yield event
        except GeneratorExit:
            # Consumer stopped early (break / close): record what we saw.
            self._finish(None)
            raise
        except BaseException as err:
            self._finish(err)
            raise
        else:
            self._finish(None)

    # -- delegation & context-manager parity -------------------------------
    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_inner"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self.__dict__["_inner"], name, value)

    def __enter__(self) -> "_MeteredStream":
        enter = getattr(self.__dict__["_inner"], "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *exc: Any) -> Any:
        self._finish(exc[1] if exc and exc[1] is not None else None)
        exit_ = getattr(self.__dict__["_inner"], "__exit__", None)
        if exit_ is not None:
            return exit_(*exc)
        return False

    def close(self) -> Any:
        self._finish(None)
        close = getattr(self.__dict__["_inner"], "close", None)
        if close is not None:
            return close()
        return None


def _coalesce(value: Optional[int], default: Optional[int]) -> Optional[int]:
    return value if value is not None else default

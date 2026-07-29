"""Local Bridge Agent - Hermes-style daemon that owns the desktop session.

The Bridge is a local HTTP/WebSocket service.  It exposes a typed
JSON-RPC-style API to any number of frontends (Telegram bot, CLI,
web UI).  All persistent state is owned by the Bridge; frontends are
thin clients that send requests and receive streamed events.

Modules
-------
  protocol        Typed messages exchanged with the Bridge
  api             In-process client used by frontends in the same Python
                  interpreter (e.g. tests, embedded usage)
  server          The actual HTTP + WebSocket server
"""

from .protocol import (
    ActionInvocation,
    ActionResult,
    Auth,
    ErrorPayload,
    Event,
    Hello,
    Request,
    Response,
    MessageType,
    encode_message,
    decode_message,
)

# Late import to avoid a circular dependency: ``api.client`` references
# ``local_agent.telegram`` which references ``local_agent.core`` which
# may import the bridge protocol for type annotations.
def __getattr__(name):  # PEP 562
    if name in {"BridgeClient", "BridgeConnectionError"}:
        from .api.client import BridgeClient, BridgeConnectionError
        return {"BridgeClient": BridgeClient, "BridgeConnectionError": BridgeConnectionError}[name]
    raise AttributeError(name)


__all__ = [
    "ActionInvocation",
    "ActionResult",
    "Auth",
    "ErrorPayload",
    "Event",
    "Hello",
    "Request",
    "Response",
    "MessageType",
    "encode_message",
    "decode_message",
    "BridgeClient",
    "BridgeConnectionError",
]

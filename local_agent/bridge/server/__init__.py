"""HTTP + WebSocket server for the Bridge.

Two transports are supported:

  * Plain HTTP POST to ``/rpc`` returns a single :class:`Response`.
  * Server-Sent Events on ``/stream`` stream a chat run as a series
    of ``data:`` lines.

Health is exposed on ``/health``.

Authentication uses a bearer token that the Bridge generates on first
run and stores in ``<DATA_DIR>/bridge.token``.  Frontends read the
token from that file (or from the ``LOCAL_AGENT_BRIDGE_TOKEN`` env
variable) and pass it as ``Authorization: Bearer <token>``.
"""

from .server import BridgeServer, ServerConfig

__all__ = ["BridgeServer", "ServerConfig"]

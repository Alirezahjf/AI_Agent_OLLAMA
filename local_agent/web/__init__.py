"""Web UI for the local assistant.

A small FastAPI app that exposes the Bridge over HTTP/WebSocket and
serves a single-page HTML/JS front-end.  Designed to be opened in
``http://127.0.0.1:7824`` from any browser on the same machine.
"""

from .app import run_web, WebServer

__all__ = ["run_web", "WebServer"]

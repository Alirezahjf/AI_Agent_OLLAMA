"""
Local Bridge Agent - Hermes-style architecture
==============================================

The Bridge is a local HTTP/WebSocket service that runs on the user's
Windows machine.  It is the *single source of truth* for:

  * The agent's persistent state (history, preferences, audit)
  * The desktop control surface (mouse, keyboard, windows, processes)
  * The personal Telegram user account
  * Every other capability (file ops, web search, shell, ...)

Any number of *frontends* can connect to it simultaneously:

  +-----------------+      +-----------------+      +-----------------+
  |  Telegram Bot   |      |  Web UI         |      |  CLI            |
  |  (server/cloud) |      |  (browser)      |      |  (terminal)     |
  +--------+--------+      +--------+--------+      +--------+--------+
           |                        |                        |
           |  HTTPS/JSON-RPC        |  WebSocket             |  direct call
           +------------------------+------------------------+
                                    |
                                    v
                      +-----------------------------+
                      |   Bridge (localhost:7823)   |
                      |   - auth token              |
                      |   - HTTP + WebSocket        |
                      |   - agent loop              |
                      |   - tools registry          |
                      |   - shared history          |
                      +-------------+---------------+
                                    |
                                    v
                      +-----------------------------+
                      |  Windows desktop session    |
                      |  (pyautogui, UIA, Telethon) |
                      +-----------------------------+

Why a Bridge?
-------------
1. *State sharing.*  The Telegram bot, the web UI, and the CLI all
   observe the same history.  When the user types a question in the
   web UI, the bot's next reply is aware of it.

2. *One process owns the desktop.*  The Bridge is the *only* process
   that ever talks to the GUI / Telethon session.  Two competing
   processes trying to drive the keyboard at once is a recipe for
   chaos.

3. *Hermes parity.*  Hermes runs a daemon; we do the same.  Frontends
   are thin clients.

4. *Security.*  The Bridge binds to localhost by default and requires
   a token.  Exposing it to the network is opt-in.

This module contains the protocol and the in-process API client that
any frontend (CLI, bot, web) can use.  The actual HTTP/WebSocket
server lives in :mod:`local_agent.bridge.server`.
"""

from __future__ import annotations

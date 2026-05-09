"""Shared server lifecycle state for Blender operators and UI."""

from __future__ import annotations

import bpy

from .core.server import BlenderAgentServer, DEFAULT_HOST


_server: BlenderAgentServer | None = None


def get_server() -> BlenderAgentServer | None:
    return _server


def server_url(context: bpy.types.Context) -> str:
    server = get_server()
    if server and server.running:
        return server.url_for_browser()
    return f"http://localhost:{context.scene.blender_agent_port}"


def start_server(context: bpy.types.Context) -> BlenderAgentServer:
    global _server
    if _server and _server.running:
        return _server

    _server = BlenderAgentServer(
        host=DEFAULT_HOST,
        port=context.scene.blender_agent_port,
        allow_lan=context.scene.blender_agent_allow_lan,
    )
    _server.start()
    return _server


def stop_server() -> None:
    global _server
    if _server:
        _server.stop()
        _server = None

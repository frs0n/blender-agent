"""Blender operator classes for Blender Agent."""

from __future__ import annotations

from .server import (
    BLENDER_AGENT_OT_OpenBrowser,
    BLENDER_AGENT_OT_StartServer,
    BLENDER_AGENT_OT_StopServer,
)


classes = (
    BLENDER_AGENT_OT_StartServer,
    BLENDER_AGENT_OT_StopServer,
    BLENDER_AGENT_OT_OpenBrowser,
)

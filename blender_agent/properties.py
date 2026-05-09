"""Blender scene properties used by the add-on UI."""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty

from .core.server import DEFAULT_ALLOW_LAN, DEFAULT_PORT


def register() -> None:
    bpy.types.Scene.blender_agent_port = IntProperty(
        name="Port",
        description="Local browser server port",
        default=DEFAULT_PORT,
        min=1024,
        max=65535,
    )
    bpy.types.Scene.blender_agent_allow_lan = BoolProperty(
        name="Expose on LAN",
        description="Allow other devices on the local network to reach the Blender Agent server",
        default=DEFAULT_ALLOW_LAN,
    )
    bpy.types.Scene.blender_agent_status = StringProperty(
        name="Status",
        default="Stopped",
    )


def unregister() -> None:
    del bpy.types.Scene.blender_agent_status
    del bpy.types.Scene.blender_agent_port
    del bpy.types.Scene.blender_agent_allow_lan

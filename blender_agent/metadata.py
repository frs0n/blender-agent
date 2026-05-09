"""Add-on metadata shared by Blender and packaging tools."""

from __future__ import annotations


VERSION = (0, 1, 0)


bl_info = {
    "name": "Blender Agent",
    "author": "frs0n",
    "version": VERSION,
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Blender Agent",
    "description": "Start a local AI chat server that controls Blender with natural language.",
    "category": "Interface",
}

"""Blender Agent add-on entry point."""

from __future__ import annotations

import bpy

from . import properties, server_state
from .metadata import bl_info as ADDON_BL_INFO
from .operators import classes as operator_classes
from .ui import classes as ui_classes


bl_info = ADDON_BL_INFO


classes = (*operator_classes, *ui_classes)


def get_server():
    return server_state.get_server()


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)
    properties.register()


def unregister() -> None:
    server_state.stop_server()
    properties.unregister()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()

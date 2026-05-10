# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# pylint: disable=C0114  # See tool doc-string.

__all__ = (
    "register",
)

from blmcp.tools_helpers import (
    toolcode_format_call,
    toolcode_load_from_filepath,
    toolcode_wrap_with_calling_convention,
)
from blmcp.tools_helpers.connection import send_code
from blmcp.tools.snap_objects_toolcode import Params
from mcp.server.fastmcp import FastMCP  # pylint: disable=import-error,no-name-in-module
from mcp.types import ToolAnnotations  # pylint: disable=import-error,no-name-in-module

_TOOL_CALL = toolcode_wrap_with_calling_convention(toolcode_load_from_filepath(__file__))


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Snap Objects",
            readOnlyHint=False,
        )
    )
    def snap_objects(
        object_names: list[str],
        target: str,
        snap_point: str = "center",
        axes: list[str] = ("x", "y", "z"),
    ) -> dict[str, object]:
        """
        Snap objects to a specified target.

        Parameters:
        - object_names: List of object names to snap
        - target: Target to snap to:
            - 'cursor': Snap to the 3D cursor position
            - 'active': Snap to the active object
            - 'grid': Snap to the nearest grid point
            - 'world_origin': Snap to the world origin (0,0,0)
        - snap_point: Which point of the object to snap:
            - 'center': Snap the object's center (default)
            - 'min': Snap the minimum bound (bottom/left/back)
            - 'max': Snap the maximum bound (top/right/front)
        - axes: List of axes to snap ('x', 'y', 'z'). Default is all axes.
        """
        p = Params(
            object_names=object_names,
            target=target,
            snap_point=snap_point,
            axes=axes,
        )
        code = toolcode_format_call(_TOOL_CALL, p)
        return send_code(code, strict_json=True)

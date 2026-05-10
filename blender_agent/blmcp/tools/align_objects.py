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
from blmcp.tools.align_objects_toolcode import Params
from mcp.server.fastmcp import FastMCP  # pylint: disable=import-error,no-name-in-module
from mcp.types import ToolAnnotations  # pylint: disable=import-error,no-name-in-module

_TOOL_CALL = toolcode_wrap_with_calling_convention(toolcode_load_from_filepath(__file__))


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Align Objects",
            readOnlyHint=False,
        )
    )
    def align_objects(
        object_names: list[str],
        axis: str,
        mode: str,
        reference: str = "selection",
    ) -> dict[str, object]:
        """
        Align multiple objects along a specified axis.

        Parameters:
        - object_names: List of object names to align
        - axis: Axis to align along ('x', 'y', or 'z')
        - mode: Alignment mode:
            - 'min': Align to minimum bound (bottom/left/back)
            - 'max': Align to maximum bound (top/right/front)
            - 'center': Align to center
        - reference: Reference for alignment:
            - 'selection': Align relative to the selected objects (default)
            - 'active': Align relative to the active object
            - 'cursor': Align relative to the 3D cursor
            - 'world_origin': Align relative to the world origin (0,0,0)
        """
        p = Params(
            object_names=object_names,
            axis=axis,
            mode=mode,
            reference=reference,
        )
        code = toolcode_format_call(_TOOL_CALL, p)
        return send_code(code, strict_json=True)

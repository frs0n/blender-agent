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
from blmcp.tools.distribute_objects_toolcode import Params
from mcp.server.fastmcp import FastMCP  # pylint: disable=import-error,no-name-in-module
from mcp.types import ToolAnnotations  # pylint: disable=import-error,no-name-in-module

_TOOL_CALL = toolcode_wrap_with_calling_convention(toolcode_load_from_filepath(__file__))


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Distribute Objects",
            readOnlyHint=False,
        )
    )
    def distribute_objects(
        object_names: list[str],
        axis: str,
        mode: str = "even",
        gap_size: float = 0.0,
    ) -> dict[str, object]:
        """
        Distribute multiple objects evenly along a specified axis.

        Parameters:
        - object_names: List of object names to distribute
        - axis: Axis to distribute along ('x', 'y', or 'z')
        - mode: Distribution mode:
            - 'even': Distribute with equal spacing between objects (default)
            - 'gap': Distribute with a specified gap size between objects
        - gap_size: Gap size in Blender units (only used when mode='gap')
        """
        p = Params(
            object_names=object_names,
            axis=axis,
            mode=mode,
            gap_size=gap_size,
        )
        code = toolcode_format_call(_TOOL_CALL, p)
        return send_code(code, strict_json=True)

# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for distributing multiple objects evenly along an axis.
"""

__all__ = (
    "Params",
    "Result",
    "main",
)

from typing import NamedTuple


class Params(NamedTuple):
    object_names: list[str]
    axis: str  # 'x', 'y', 'z'
    mode: str  # 'even' (equal spacing), 'gap' (specified gap size)
    gap_size: float = 0.0  # Only used when mode='gap'


class Result(NamedTuple):
    status: str
    distributed_count: int = 0
    message: str | None = None


def _get_aabb(obj):
    """Returns the world-space axis-aligned bounding box (AABB) of an object."""
    import mathutils  # pylint: disable=import-error,no-name-in-module

    if obj.type != 'MESH':
        return None

    local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]
    world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]
    min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
    max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))
    return min_corner, max_corner


def _get_object_bounds(obj, axis_idx):
    """Returns the min and max bounds of an object along a specific axis."""
    import mathutils  # pylint: disable=import-error,no-name-in-module

    if obj.type == 'MESH':
        min_corner, max_corner = _get_aabb(obj)
        return min_corner[axis_idx], max_corner[axis_idx]
    else:
        loc = obj.matrix_world.translation[axis_idx]
        return loc, loc


def main(params: Params) -> Result:
    import bpy  # pylint: disable=import-error,no-name-in-module

    axis_map = {'x': 0, 'y': 1, 'z': 2}
    axis_idx = axis_map.get(params.axis.lower())
    if axis_idx is None:
        return Result(
            status="error",
            message="Invalid axis: {!r}. Must be 'x', 'y', or 'z'.".format(params.axis),
        )

    mode = params.mode.lower()
    if mode not in ('even', 'gap'):
        return Result(
            status="error",
            message="Invalid mode: {!r}. Must be 'even' or 'gap'.".format(params.mode),
        )

    # Get objects
    objects = []
    for name in params.object_names:
        obj = bpy.data.objects.get(name)
        if obj is None:
            return Result(
                status="error",
                message="Object {!r} not found.".format(name),
            )
        objects.append(obj)

    if len(objects) < 3:
        return Result(
            status="error",
            message="At least 3 objects are required for distribution.",
        )

    # Sort objects by their current position along the axis
    objects.sort(key=lambda obj: _get_object_bounds(obj, axis_idx)[0])

    # Get the bounds of the first and last objects
    first_min = _get_object_bounds(objects[0], axis_idx)[0]
    last_max = _get_object_bounds(objects[-1], axis_idx)[1]

    if mode == 'even':
        # Calculate total available space
        total_space = last_max - first_min

        # Calculate total size of all objects
        total_object_size = sum(
            _get_object_bounds(obj, axis_idx)[1] - _get_object_bounds(obj, axis_idx)[0]
            for obj in objects
        )

        # Calculate equal spacing between objects
        remaining_space = total_space - total_object_size
        gap = remaining_space / (len(objects) - 1)

    else:  # mode == 'gap'
        gap = params.gap_size

    # Distribute objects
    current_pos = first_min
    for obj in objects:
        obj_min, obj_max = _get_object_bounds(obj, axis_idx)
        obj_size = obj_max - obj_min

        # Move object to current position
        offset = current_pos - obj_min
        obj.location[axis_idx] += offset

        # Move to next position
        current_pos += obj_size + gap

    bpy.context.view_layer.update()

    return Result(
        status="ok",
        distributed_count=len(objects),
        message="Distributed {} objects along {} axis (mode: {}).".format(
            len(objects), params.axis, mode
        ),
    )

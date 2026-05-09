"""View3D sidebar panel for Blender Agent."""

from __future__ import annotations

import bpy

from .. import server_state


class BLENDER_AGENT_PT_Panel(bpy.types.Panel):
    bl_label = "Blender Agent"
    bl_idname = "BLENDER_AGENT_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blender Agent"

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        scene = context.scene
        server = server_state.get_server()
        running = bool(server and server.running)

        if running:
            layout.operator("blender_agent.stop_server", text="Stop Blender Agent", icon="PAUSE")
        else:
            layout.operator("blender_agent.start_server", text="Start Blender Agent", icon="PLAY")

        row = layout.row(align=True)
        row.enabled = not running
        row.prop(scene, "blender_agent_port")
        row.prop(scene, "blender_agent_allow_lan", text="", icon="WORLD")

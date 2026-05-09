"""Operators that control the local Blender Agent server."""

from __future__ import annotations

import webbrowser

import bpy

from .. import server_state


def open_server_browser(context: bpy.types.Context) -> None:
    webbrowser.open(server_state.server_url(context))


class BLENDER_AGENT_OT_StartServer(bpy.types.Operator):
    bl_idname = "blender_agent.start_server"
    bl_label = "Start Blender Agent"
    bl_description = "Start the local Blender Agent web server"

    def execute(self, context: bpy.types.Context):
        server = server_state.get_server()
        if server and server.running:
            self.report({"INFO"}, "Blender Agent is already running")
            return {"FINISHED"}

        try:
            server = server_state.start_server(context)
            context.scene.blender_agent_status = f"Running at {server_state.server_url(context)}"
            if server.allow_lan:
                lan_url = server.lan_url()
                if lan_url:
                    context.scene.blender_agent_status += f" | LAN: {lan_url}"
            open_server_browser(context)
            self.report({"INFO"}, context.scene.blender_agent_status)
        except Exception as exc:
            server_state.stop_server()
            context.scene.blender_agent_status = f"Failed to start: {exc}"
            self.report({"ERROR"}, context.scene.blender_agent_status)
            return {"CANCELLED"}

        return {"FINISHED"}


class BLENDER_AGENT_OT_StopServer(bpy.types.Operator):
    bl_idname = "blender_agent.stop_server"
    bl_label = "Stop Blender Agent"
    bl_description = "Stop the local Blender Agent web server"

    def execute(self, context: bpy.types.Context):
        server_state.stop_server()
        context.scene.blender_agent_status = "Stopped"
        self.report({"INFO"}, "Blender Agent stopped")
        return {"FINISHED"}


class BLENDER_AGENT_OT_OpenBrowser(bpy.types.Operator):
    bl_idname = "blender_agent.open_browser"
    bl_label = "Open Browser"
    bl_description = "Open the Blender Agent web UI in your default browser"

    def execute(self, context: bpy.types.Context):
        open_server_browser(context)
        return {"FINISHED"}

# Blender Agent

Blender Agent is a Blender add-on that starts a local browser workspace at
`http://localhost:6789`. The browser UI talks to an OpenAI-compatible model
through a built-in zero-dependency tool-calling runtime and lets the model
inspect and mutate Blender through a trusted local tool surface adapted from
Blender Lab's official [`blender_mcp`](https://projects.blender.org/lab/blender_mcp).

## Features

- View3D sidebar panel for starting and stopping the local server
- Local browser UI with multiple conversations, live run status, tool timeline, and SSE streaming
- Built-in smolagents-inspired tool-calling run loop for OpenAI-compatible Chat Completions models
- Blender main-thread scheduling through `bpy.app.timers`
- Official Blender Lab MCP prompts, bundled API/manual docs, scene summaries, screenshots, navigation helpers, render-to-path tools, and trusted Python execution
- Localhost-only default binding with explicit opt-in LAN exposure

## Project Layout

- `blender_agent/__init__.py`: Blender add-on entry point and class registration
- `blender_agent/metadata.py`: shared add-on metadata
- `blender_agent/operators/`: Blender operators for server actions
- `blender_agent/ui/`: View3D sidebar UI
- `blender_agent/core/server.py`: local HTTP API, run store, SSE, and server lifecycle
- `blender_agent/web/`: browser UI assets served by the add-on
- `blender_agent/agent_runtime.py`: zero-dependency OpenAI-compatible tool-calling runtime
- `blender_agent/blender_tools.py`: Blender tools and OpenAI tool schemas
- `blender_agent/blmcp/`: packaged Blender Lab MCP tools, prompts, and bundled docs used at runtime

## Install

Install the published release zip from GitHub in Blender via `Extensions >
Install from Disk`, or load the `blender_agent/` package directly during local
development.

## Local Development

Symlink the add-on package into Blender's add-ons directory:

```bash
ln -sfn "$PWD/blender_agent" "$HOME/Library/Application Support/Blender/5.1/scripts/addons/blender_agent"
```

Change the `5.1` folder for your Blender version. Then open the 3D viewport
sidebar, choose the `Blender Agent` tab, click `Start Blender Agent`, and open
`http://localhost:6789`.

## Blender Extensions

The add-on includes `blender_agent/blender_manifest.toml` for Blender 4.2+
extension metadata. Validate and build it from the add-on directory with
Blender's extension commands:

```bash
cd blender_agent
blender --command extension validate
blender --command extension build
```

The add-on runtime uses only Python's standard library and Blender's bundled
Python modules. No external model client package needs to be installed into
Blender's Python environment for development.

## Model Configuration

The web UI stores these settings in browser local storage:

- OpenAI-compatible base URL, for example `https://api.openai.com/v1`
- API key
- Model name
- Maximum tool rounds

The add-on does not persist API keys in Blender. Networked model calls respect
Blender's online access setting when that setting is available.

## Local API

- `GET /api/health`: server health
- `GET /api/tools`: OpenAI-compatible tool schema
- `POST /api/chat/stream`: starts an agent run and streams SSE events
- `POST /api/runs`: starts an async run for polling-based clients
- `GET /api/runs/{id}`: returns current run status, steps, events, and output

## Security

Blender Agent is a trusted-user local tool. The server binds to `127.0.0.1` by
default and uses a per-session browser token for the built-in UI. LAN exposure
is opt-in. The AI tool surface intentionally includes arbitrary Blender Python
execution for parity with Blender Lab's official MCP server, so only run it with
model endpoints and prompts you trust.

## Third-party Reuse

This project includes Blender Lab's official `blender_mcp` prompt, tool-code,
and bundled documentation from `https://projects.blender.org/lab/blender_mcp`.
Those files are GPL-3.0-or-later and live under `blender_agent/blmcp/` for
packaged runtime use.

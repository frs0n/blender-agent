# Repository Guidelines

## Project Structure & Module Organization
`blender_agent/` contains the Blender add-on package. `__init__.py` is the thin registration entry point, `operators/` contains server-control operators, `ui/` contains the View3D sidebar, `core/server.py` serves `http://localhost:6789`, `web/` contains the browser UI asset, and `blender_tools.py` holds Blender actions and OpenAI tool schemas. `third_party/blender-mcp/` is vendored reference material kept for attribution and reuse.

## Build, Test, and Development Commands
Use Python 3.

For iterative development on macOS, the README also shows a symlink into Blender’s add-ons directory so Blender loads the checkout directly.

## Coding Style & Naming Conventions
Follow standard Python style: 4-space indentation, ASCII unless a file already uses Unicode, and explicit names for operators, panels, and tools. Blender classes use `BLENDER_AGENT_*` prefixes, and operator IDs follow `blender_agent.*`. Keep command names stable when they mirror `blender-mcp` behavior. Prefer small, direct functions over deep abstraction.

## Testing Guidelines
There is no separate unit-test suite yet. For release packaging, use Blender's extension tooling from the `blender_agent/` directory.

## Commit & Pull Request Guidelines
History is currently minimal, so use short imperative commit messages that describe the change, for example `add localhost chat server`. Keep PRs focused. Include a summary, the Blender version tested, and the exact commands you ran. Add screenshots or a short screen recording when the sidebar UI or browser flow changes.

## Security & Configuration Tips
The local server binds to `127.0.0.1` by default and can expose LAN access only when explicitly enabled. The browser UI stores API settings in local storage, not in Blender. Arbitrary Blender Python execution is intentionally supported for parity with `blender-mcp`; treat the tool surface as trusted-user only.

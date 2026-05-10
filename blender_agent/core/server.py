"""Local HTTP server and agent run loop for the Blender Agent add-on."""

from __future__ import annotations

import asyncio
import json
import queue
import secrets
import threading
import time
import traceback
import socket
from urllib.parse import parse_qs, urlparse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import bpy

from .. import agent_runtime, blender_tools


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6789
DEFAULT_ALLOW_LAN = False


INDEX_HTML_PATH = Path(__file__).resolve().parents[1] / "web" / "index.html"


def render_index_html(token: str) -> bytes:
    html = INDEX_HTML_PATH.read_text(encoding="utf-8").replace("__TOKEN__", token)
    return html.encode("utf-8")




@dataclass
class AgentStep:
    id: str
    kind: str
    title: str
    status: str = "running"
    detail: str = ""
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class AgentRun:
    id: str
    session_id: str
    status: str = "queued"
    content: str = ""
    error: str | None = None
    steps: list[AgentStep] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "status": self.status,
            "content": self.content,
            "error": self.error,
            "steps": [step.to_dict() for step in self.steps],
            "events": self.events[-100:],
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "ended_at": self.ended_at,
        }


@dataclass
class RunControl:
    """Cooperative loop control — Swarm-style pause/resume/stop.

    Uses simple flags checked cooperatively by the agent loop at every yield point
    (between SSE chunks, before/after tool calls, at the top of each step).
    A ``paused`` flag causes :func:`check` to raise :class:`agent_runtime.RunPausedError`
    which propagates up to :func:`run_agent` where the loop state is preserved for resume.
    """

    paused: bool = False
    stopped: bool = False
    resume_state: dict | None = None  # {messages, step_number, final_answer}

    def check(self) -> None:
        """Cooperative check called at every yield point in the agent loop.

        Raises:
            RunPausedError: Pause was requested — save state and exit the loop.
            RunStoppedError: Stop was requested — exit permanently.
        """
        if self.stopped:
            raise agent_runtime.RunStoppedError()
        if self.paused:
            raise agent_runtime.RunPausedError()

    def request_pause(self) -> None:
        self.paused = True

    def request_resume(self) -> None:
        self.paused = False
        self.stopped = False

    def request_stop(self) -> None:
        self.stopped = True
        self.paused = True  # unblock any pending pause check


class RunStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._runs: dict[str, AgentRun] = {}

    def create(self, session_id: str) -> AgentRun:
        run = AgentRun(id=secrets.token_hex(6), session_id=session_id)
        with self._lock:
            self._runs[run.id] = run
        return run

    def get(self, run_id: str) -> AgentRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def update(self, run: AgentRun) -> None:
        run.updated_at = time.time()
        with self._lock:
            self._runs[run.id] = run


def _stored_event(event: dict[str, Any]) -> dict[str, Any]:
    stored = json.loads(json.dumps({k: v for k, v in event.items() if k != "run"}))
    if "result" in stored:
        stored["result"] = agent_runtime.client_tool_result(stored["result"])
    return stored


def _launch_agent_run(
    server: BlenderAgentHTTPServer,
    payload: dict[str, Any],
    event_queue: queue.Queue[dict[str, Any]] | None = None,
    control: RunControl | None = None,
    existing_run: AgentRun | None = None,
) -> AgentRun:
    if existing_run is not None:
        run = existing_run
    else:
        run = server.store.create(payload.get("session_id") or "default")
    if control is None:
        control = RunControl()
    control.payload = payload
    server.controls[run.id] = control

    def run_wrapper() -> None:
        try:
            run_agent(payload, server.executor, server.store, run, event_queue, control)
        finally:
            if run.status not in ("paused",):
                server.controls.pop(run.id, None)

    threading.Thread(target=run_wrapper, daemon=True).start()
    return run


class EventSink:
    def __init__(self, run: AgentRun, store: RunStore, event_queue: queue.Queue[dict[str, Any]] | None = None):
        self.run = run
        self.store = store
        self.event_queue = event_queue

    def emit(self, event: dict[str, Any]) -> None:
        event["run"] = self.run.to_dict()
        self.run.events.append({"time": time.time(), **_stored_event(event)})
        self.store.update(self.run)
        if self.event_queue:
            self.event_queue.put(event)

    def step(self, kind: str, title: str, detail: str = "") -> AgentStep:
        step = AgentStep(id=secrets.token_hex(4), kind=kind, title=title, detail=detail)
        self.run.steps.append(step)
        self.emit({"type": "step", "summary": title})
        return step

    def finish_step(self, step: AgentStep, status: str = "completed", detail: str | None = None) -> None:
        step.status = status
        step.ended_at = time.time()
        if detail is not None:
            step.detail = detail
        self.emit({"type": "step", "summary": f"{step.title}: {status}"})


class BlenderExecutor:
    """Schedules command execution on Blender's main thread."""

    def __init__(self, timeout_seconds: float = 180.0):
        self.timeout_seconds = timeout_seconds

    def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        event = threading.Event()
        holder: dict[str, Any] = {}

        def run_on_main_thread():
            try:
                holder["response"] = blender_tools.dispatch_command(command)
            except Exception as exc:
                holder["response"] = {"status": "error", "message": str(exc)}
            finally:
                event.set()
            return None

        bpy.app.timers.register(run_on_main_thread, first_interval=0.0)
        if not event.wait(self.timeout_seconds):
            return {"status": "error", "message": "Timed out waiting for Blender"}
        return holder.get("response", {"status": "error", "message": "No Blender response"})


class BlenderAgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, executor: BlenderExecutor, token: str, store: RunStore):
        super().__init__(server_address, RequestHandlerClass)
        self.executor = executor
        self.token = token
        self.store = store
        self.controls: dict[str, RunControl] = {}


class RequestHandler(BaseHTTPRequestHandler):
    server: BlenderAgentHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        print("[Blender Agent]", format % args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_event(self, payload: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def _authorized(self) -> bool:
        return self.headers.get("X-Blender-Agent-Token") == self.server.token

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Blender-Agent-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            html = render_index_html(self.server.token)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/api/tools":
            query = parse_qs(parsed_path.query)
            mode = "ask" if (query.get("mode") or ["agent"])[0] == "ask" else "agent"
            self._send_json(200, {"mode": mode, "tools": blender_tools.tools_for_mode(mode)})
            return
        if parsed_path.path == "/api/scene/objects":
            if not self._authorized():
                self._send_json(401, {"error": "Unauthorized"})
                return
            result = self.server.executor.execute({"type": "list_scene_objects", "params": {}})
            if result.get("status") != "success":
                self._send_json(500, {"error": result.get("message") or "Unable to list scene objects"})
                return
            self._send_json(200, result.get("result") or {"objects": []})
            return
        if self.path == "/api/health":
            self._send_json(200, {"status": "ok", "time": time.time()})
            return
        if self.path.startswith("/api/runs/"):
            if not self._authorized():
                self._send_json(401, {"error": "Unauthorized"})
                return
            run_id = self.path.rsplit("/", 1)[-1]
            run = self.server.store.get(run_id)
            if not run:
                self._send_json(404, {"error": "Run not found"})
                return
            self._send_json(200, run.to_dict())
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(401, {"error": "Unauthorized"})
            return
        try:
            if self.path.startswith("/api/runs/") and self.path.endswith("/pause"):
                run_id = self.path.rsplit("/", 2)[-2]
                control = self.server.controls.get(run_id)
                if not control:
                    self._send_json(404, {"error": "Run not found"})
                    return
                control.request_pause()
                run = self.server.store.get(run_id)
                if run:
                    run.status = "paused"
                    run.ended_at = time.time()
                    self.server.store.update(run)
                self._send_json(200, {"status": "paused", "run_id": run_id})
                return
            if self.path.startswith("/api/runs/") and self.path.endswith("/resume"):
                run_id = self.path.rsplit("/", 2)[-2]
                control = self.server.controls.get(run_id)
                run = self.server.store.get(run_id)
                if not control or not run:
                    self._send_json(404, {"error": "Run not found"})
                    return
                if not control.resume_state:
                    self._send_json(400, {"error": "Run has no saved state to resume from"})
                    return
                control.request_resume()
                run.status = "running"
                run.ended_at = None
                self.server.store.update(run)
                event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self._send_sse_event({"type": "run", "run": run.to_dict()})
                _launch_agent_run(self.server, getattr(control, "payload", {}), event_queue, control, existing_run=run)
                while True:
                    event = event_queue.get()
                    self._send_sse_event(event)
                    if event.get("type") in {"done", "error", "paused", "stopped"}:
                        break
                return
            if self.path == "/api/chat":
                payload = self._read_json()
                result = run_chat_completion(payload, self.server.executor)
                self._send_json(200, result)
                return
            if self.path == "/api/runs":
                payload = self._read_json()
                run = _launch_agent_run(self.server, payload)
                self._send_json(202, run.to_dict())
                return
            if self.path == "/api/chat/stream":
                payload = self._read_json()
                event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
                run = _launch_agent_run(self.server, payload, event_queue)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self._send_sse_event({"type": "run", "run": run.to_dict()})
                while True:
                    event = event_queue.get()
                    self._send_sse_event(event)
                    if event.get("type") in {"done", "error", "paused", "stopped"}:
                        break
                return
            self._send_json(404, {"error": "Not found"})
        except Exception as exc:
            traceback.print_exc()
            if self.path == "/api/chat/stream":
                self._send_sse_event({"type": "error", "error": str(exc)})
            else:
                self._send_json(500, {"error": str(exc)})


def run_agent(
    payload: dict[str, Any],
    executor: BlenderExecutor,
    store: RunStore,
    run: AgentRun,
    event_queue: queue.Queue[dict[str, Any]] | None,
    control: RunControl | None = None,
) -> dict[str, Any]:
    sink = EventSink(run, store, event_queue)
    api_key = payload.get("api_key") or ""
    if not api_key:
        run.status = "failed"
        run.error = "API key is required"
        run.ended_at = time.time()
        sink.emit({"type": "error", "error": run.error})
        return run.to_dict()
    if hasattr(bpy.app, "online_access") and not bpy.app.online_access:
        run.status = "failed"
        run.error = "Blender online access is disabled"
        run.ended_at = time.time()
        sink.emit({"type": "error", "error": run.error})
        return run.to_dict()

    run.status = "running"
    run.ended_at = None
    sink.emit({"type": "step", "summary": "Run started with tool-calling runtime"})
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        check_fn = control.check if control else None
        resume_state = control.resume_state if control else None

        def save_state_cb(state: dict) -> None:
            if control:
                control.resume_state = state

        task = loop.create_task(
            agent_runtime.run_tool_calling_async(
                payload, executor, sink, check=check_fn, resume_state=resume_state, save_state=save_state_cb
            )
        )
        result = loop.run_until_complete(task)
        return result
    except agent_runtime.RunPausedError:
        run.status = "paused"
        run.error = None
        run.ended_at = time.time()
        sink.emit({"type": "paused", "content": run.content})
        return run.to_dict()
    except agent_runtime.RunStoppedError:
        run.status = "stopped"
        run.error = None
        run.ended_at = time.time()
        sink.emit({"type": "stopped", "content": run.content})
        return run.to_dict()
    except Exception as exc:
        traceback.print_exc()
        run.status = "failed"
        run.error = str(exc)
        run.ended_at = time.time()
        sink.emit({"type": "error", "error": str(exc)})
        return run.to_dict()
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def run_chat_completion(payload: dict[str, Any], executor: BlenderExecutor) -> dict[str, Any]:
    store = RunStore()
    run = store.create(payload.get("session_id") or "sync")
    return run_agent(payload, executor, store, run, None)


class BlenderAgentServer:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, allow_lan: bool = DEFAULT_ALLOW_LAN):
        self.host = host
        self.port = port
        self.allow_lan = allow_lan
        self.token = secrets.token_urlsafe(24)
        self.store = RunStore()
        self.httpd: BlenderAgentHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        bind_host = "0.0.0.0" if self.allow_lan else self.host
        self.httpd = BlenderAgentHTTPServer(
            (bind_host, self.port),
            RequestHandler,
            executor=BlenderExecutor(),
            token=self.token,
            store=self.store,
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url_for_browser(self) -> str:
        host = self.host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        return f"http://{host}:{self.port}"

    def lan_url(self) -> str | None:
        if not self.allow_lan or not self.running:
            return None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                ip = sock.getsockname()[0]
        except OSError:
            return None
        return f"http://{ip}:{self.port}"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread:
            self.thread.join(timeout=2.0)
            self.thread = None

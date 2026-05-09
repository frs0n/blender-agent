"""Zero-dependency OpenAI-compatible tool-calling runtime for Blender Agent."""

from __future__ import annotations

import asyncio
import errno
import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol, TypeVar

from . import blender_tools


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"
REQUEST_TIMEOUT_SECONDS = 180
REQUEST_RETRY_COUNT = 3
REQUEST_TOTAL_ATTEMPTS = REQUEST_RETRY_COUNT + 1
REQUEST_RETRY_BASE_DELAY_SECONDS = 0.75
RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
T = TypeVar("T")


class Executor(Protocol):
    def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        ...


class Store(Protocol):
    def update(self, run: Any) -> None:
        ...


class Sink(Protocol):
    run: Any
    store: Store

    def emit(self, event: dict[str, Any]) -> None:
        ...

    def step(self, kind: str, title: str, detail: str = "") -> Any:
        ...

    def finish_step(self, step: Any, status: str = "completed", detail: str | None = None) -> None:
        ...


@dataclass
class ToolCall:
    name: str
    arguments: Any
    id: str

    def to_message_tool_call(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments) if not isinstance(self.arguments, str) else self.arguments,
            },
        }


@dataclass
class ActionStep:
    step_number: int
    model_input_messages: list[dict[str, Any]]
    model_output: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    is_final_answer: bool = False


@dataclass
class ChatCompletion:
    content: str
    tool_calls: list[ToolCall]
    finish_reason: str | None = None


def _normalise_base_url(base_url: str) -> str:
    base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _is_connection_refused_error(exc: BaseException) -> bool:
    if isinstance(exc, ConnectionRefusedError):
        return True
    if isinstance(exc, OSError) and exc.errno == errno.ECONNREFUSED:
        return True
    reason = getattr(exc, "reason", None)
    return bool(reason and reason is not exc and _is_connection_refused_error(reason))


def _urlopen_with_proxy_fallback(
    request: urllib.request.Request,
    timeout: int,
) -> Any:
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.URLError as exc:
        if not _is_connection_refused_error(exc):
            raise
        print("[Blender Agent] Proxy connection refused; retrying model request without proxy")
        return _NO_PROXY_OPENER.open(request, timeout=timeout)


def _retry_delay_seconds(attempt: int) -> float:
    return REQUEST_RETRY_BASE_DELAY_SECONDS * (2 ** max(0, attempt - 1))


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, json.JSONDecodeError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_HTTP_STATUSES
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            return _is_retryable_exception(reason)
        text = str(reason or exc).lower()
        return any(
            marker in text
            for marker in (
                "timed out",
                "timeout",
                "temporary failure",
                "unexpected eof",
                "eof occurred in violation of protocol",
                "connection reset",
                "connection aborted",
                "connection closed",
                "remote end closed",
            )
        )
    if isinstance(exc, ssl.SSLError):
        text = str(exc).lower()
        return "unexpected eof" in text or "eof occurred in violation of protocol" in text
    if isinstance(exc, (TimeoutError, ConnectionError, BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in {
            errno.ECONNRESET,
            errno.ECONNABORTED,
            errno.ETIMEDOUT,
            errno.EPIPE,
            errno.ECONNREFUSED,
        }
    return False


def _with_request_retries(
    request_name: str,
    operation: Callable[[], T],
    on_retry: Callable[[int, Exception], None] | None = None,
) -> T:
    last_exc: BaseException | None = None
    for attempt in range(1, REQUEST_TOTAL_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - we re-raise non-retryable failures below
            last_exc = exc
            if attempt >= REQUEST_TOTAL_ATTEMPTS or not _is_retryable_exception(exc):
                raise
            if on_retry is not None:
                on_retry(attempt, exc)
            print(
                f"[Blender Agent] {request_name} failed on attempt {attempt}/{REQUEST_TOTAL_ATTEMPTS}; "
                f"retrying in {_retry_delay_seconds(attempt):.2f}s: {exc}"
            )
            time.sleep(_retry_delay_seconds(attempt))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{request_name} failed without a specific error")


def _message_has_image_input(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for part in content
    )


def _strip_image_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [message for message in messages if not _message_has_image_input(message)]


def _is_image_input_unsupported_error(exc: Exception) -> bool:
    text = str(exc).lower()
    image_markers = ("image", "vision", "multimodal")
    unsupported_markers = ("unsupported", "not support", "does not support", "invalid content")
    return any(marker in text for marker in image_markers) and any(
        marker in text for marker in unsupported_markers
    )


def normalise_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalised = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role in {"user", "assistant"} and content:
            normalised.append({"role": role, "content": content})
    return normalised


def tool_result_content(result: dict[str, Any]) -> str:
    return json.dumps(_sanitise_tool_result(result, include_preview=False))


def screenshot_preview_result(screenshot: dict[str, Any]) -> dict[str, Any]:
    return {
        key: screenshot[key]
        for key in (
            "preview_data_url",
            "preview_width",
            "preview_height",
            "width",
            "height",
            "model_width",
            "model_height",
            "capture_method",
            "format",
            "mime_type",
            "preview_size",
            "success",
        )
        if key in screenshot
    }


def _sanitise_tool_result(result: dict[str, Any], include_preview: bool) -> dict[str, Any]:
    clean = json.loads(json.dumps(result))
    image_result = clean.get("result") if isinstance(clean.get("result"), dict) else None
    if not isinstance(image_result, dict):
        return clean
    if include_preview:
        clean["result"] = screenshot_preview_result(image_result)
        return clean
    if image_result.get("model_data_url"):
        image_result["model_data_url"] = "[attached model image]"
    if image_result.get("preview_data_url"):
        image_result["preview_data_url"] = "[attached image preview]"
    return clean


def client_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return _sanitise_tool_result(result, include_preview=True)


def _tool_result_visual_message(tool_name: str, result: dict[str, Any]) -> dict[str, Any] | None:
    image_tools = {
        "get_viewport_screenshot",
        "render_scene_image",
        "get_screenshot_of_window_as_image",
        "get_screenshot_of_area_as_image",
    }
    if tool_name not in image_tools or result.get("status") != "success":
        return None
    screenshot = result.get("result") if isinstance(result.get("result"), dict) else None
    image_url = screenshot.get("model_data_url") if screenshot else None
    if not screenshot or not image_url:
        return None
    width = screenshot.get("model_width") or screenshot.get("width")
    height = screenshot.get("model_height") or screenshot.get("height")
    method = screenshot.get("capture_method") or "screenshot"
    label = "Blender render attached" if tool_name == "render_scene_image" else "Blender viewport screenshot attached"
    return {
        "role": "user",
        "_generated_visual_input": True,
        "content": [
            {
                "type": "text",
                "text": f"{label} ({width}x{height}, {method}). Use it to visually verify the scene.",
            },
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
    }


def _final_answer_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "final_answer",
            "description": "Return the final answer to the user and end the run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "A concise final answer describing the completed Blender work or result.",
                    }
                },
                "required": ["answer"],
                "additionalProperties": False,
            },
        },
    }


def _runtime_tools() -> list[dict[str, Any]]:
    return [*blender_tools.OPENAI_TOOLS, _final_answer_tool_schema()]


def _system_prompt() -> str:
    return (
        blender_tools.SYSTEM_PROMPT
        + "\n## Tool Calling Runtime\n"
        + "- You are running in a ReAct-style tool-calling loop: action, observation, repeat.\n"
        + "- Always call a tool when scene state or Blender changes are needed.\n"
        + "- When the task is complete, call final_answer with a short summary for the user.\n"
        + "- Do not call final_answer together with any other tool in the same model turn.\n"
    )


def _json_request(url: str, payload: dict[str, Any], api_key: str, timeout: int = REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with _urlopen_with_proxy_fallback(request, timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        return _with_request_retries("LLM request", operation)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"LLM request failed: {exc}") from exc


def _stream_request(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> Iterable[dict[str, Any]]:
    yield from _stream_request_once(url, payload, api_key, timeout)


def _stream_request_once(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> Iterable[dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with _urlopen_with_proxy_fallback(request, timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data_line = line[5:].strip()
            if data_line == "[DONE]":
                break
            if data_line:
                yield json.loads(data_line)


def _parse_arguments(raw_arguments: Any) -> Any:
    if isinstance(raw_arguments, str):
        try:
            return json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return raw_arguments
    return raw_arguments if raw_arguments is not None else {}


def _choice_message_to_completion(message: dict[str, Any], finish_reason: str | None = None) -> ChatCompletion:
    content = message.get("content") or ""
    tool_calls = []
    for index, raw_call in enumerate(message.get("tool_calls") or []):
        function = raw_call.get("function") or {}
        tool_calls.append(
            ToolCall(
                name=function.get("name") or "",
                arguments=_parse_arguments(function.get("arguments") or "{}"),
                id=raw_call.get("id") or f"call_{index}",
            )
        )
    return ChatCompletion(content=content, tool_calls=tool_calls, finish_reason=finish_reason)


def _merge_tool_call_deltas(existing: dict[int, dict[str, Any]], deltas: list[dict[str, Any]]) -> None:
    for delta in deltas:
        index = int(delta.get("index") or 0)
        target = existing.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        if delta.get("id"):
            target["id"] = delta["id"]
        if delta.get("type"):
            target["type"] = delta["type"]
        function_delta = delta.get("function") or {}
        if function_delta.get("name"):
            target["function"]["name"] += function_delta["name"]
        if function_delta.get("arguments"):
            target["function"]["arguments"] += function_delta["arguments"]


def _stream_events_to_completion(events: list[dict[str, Any]]) -> ChatCompletion:
    content = ""
    tool_calls_by_index: dict[int, dict[str, Any]] = {}
    finish_reason = None
    for event in events:
        choices = event.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        finish_reason = choice.get("finish_reason") or finish_reason
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content += delta["content"]
        if delta.get("tool_calls"):
            _merge_tool_call_deltas(tool_calls_by_index, delta["tool_calls"])

    tool_calls = []
    for index in sorted(tool_calls_by_index):
        raw_call = tool_calls_by_index[index]
        function = raw_call.get("function") or {}
        tool_calls.append(
            ToolCall(
                name=function.get("name") or "",
                arguments=_parse_arguments(function.get("arguments") or "{}"),
                id=raw_call.get("id") or f"call_{index}",
            )
        )
    return ChatCompletion(content=content, tool_calls=tool_calls, finish_reason=finish_reason)


class OpenAICompatibleModel:
    def __init__(self, model: str, api_key: str, base_url: str):
        self.model = model
        self.api_key = api_key
        self.url = _normalise_base_url(base_url)

    def completion_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    def generate(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatCompletion:
        payload = self.completion_payload(messages, tools, stream=False)
        response = _json_request(self.url, payload, self.api_key)
        choices = response.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM returned no choices: {response}")
        choice = choices[0]
        return _choice_message_to_completion(choice.get("message") or {}, choice.get("finish_reason"))

    def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        sink: Sink,
    ) -> ChatCompletion:
        payload = self.completion_payload(messages, tools, stream=True)
        last_exc: Exception | None = None
        for attempt in range(1, REQUEST_TOTAL_ATTEMPTS + 1):
            attempt_start_content = sink.run.content
            events = []
            try:
                for event in _stream_request_once(self.url, payload, self.api_key):
                    events.append(event)
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        sink.run.content += delta
                        sink.emit({"type": "delta", "content": delta})
                return _stream_events_to_completion(events)
            except Exception as exc:  # noqa: BLE001 - retryable network failures are handled below
                last_exc = exc
                if attempt >= REQUEST_TOTAL_ATTEMPTS or not _is_retryable_exception(exc):
                    raise RuntimeError(f"LLM stream failed: {exc}") from exc
                sink.run.content = attempt_start_content
                sink.emit(
                    {
                        "type": "retry",
                        "attempt": attempt,
                        "max_attempts": REQUEST_TOTAL_ATTEMPTS,
                        "content": attempt_start_content,
                        "error": str(exc),
                    }
                )
                print(
                    f"[Blender Agent] LLM stream failed on attempt {attempt}/{REQUEST_TOTAL_ATTEMPTS}; "
                    f"retrying in {_retry_delay_seconds(attempt):.2f}s: {exc}"
                )
                time.sleep(_retry_delay_seconds(attempt))
        raise RuntimeError(f"LLM stream failed after retries: {last_exc}") from last_exc


class ToolCallingAgent:
    """Small smolagents-inspired ToolCallingAgent specialized for Blender."""

    def __init__(self, model: OpenAICompatibleModel, executor: Executor, sink: Sink, max_steps: int):
        self.model = model
        self.executor = executor
        self.sink = sink
        self.max_steps = max(1, max_steps)
        self.memory: list[ActionStep] = []
        self.accepts_image_input = True

    def initial_messages(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        messages = [{"role": "system", "content": _system_prompt()}]
        messages.extend(normalise_messages(payload.get("messages") or []))
        return messages

    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages = self.initial_messages(payload)
        final_answer = None
        for step_number in range(1, self.max_steps + 1):
            llm_step = self.sink.step("llm", f"Model turn {step_number}", f"model={self.model.model}")
            self.sink.emit({"type": "thinking", "status": "start", "turn": step_number})
            action_step = ActionStep(step_number=step_number, model_input_messages=json.loads(json.dumps(messages)))
            try:
                completion = self.model.generate_stream(messages, _runtime_tools(), self.sink)
            except Exception as exc:
                if self._retry_without_images(messages, exc):
                    completion = self.model.generate_stream(messages, _runtime_tools(), self.sink)
                else:
                    self.sink.finish_step(llm_step, "failed", "Model request failed")
                    raise
            self.sink.finish_step(llm_step, "completed", "Model response received")

            action_step.model_output = completion.content
            if completion.tool_calls:
                action_step.tool_calls = completion.tool_calls
                messages.append(
                    {
                        "role": "assistant",
                        "content": completion.content or "",
                        "tool_calls": [tool_call.to_message_tool_call() for tool_call in completion.tool_calls],
                    }
                )
                final_answer = await self.process_tool_calls(completion.tool_calls, messages, action_step)
                action_step.ended_at = time.time()
                self.memory.append(action_step)
                if final_answer is not None:
                    action_step.is_final_answer = True
                    break
            else:
                final_answer = completion.content
                action_step.ended_at = time.time()
                action_step.is_final_answer = True
                self.memory.append(action_step)
                break

        if final_answer is None:
            final_answer = self.generate_final_answer(messages)

        self.sink.run.content = _final_output_text(final_answer)
        self.sink.run.status = "completed"
        self.sink.run.ended_at = time.time()
        self.sink.emit({"type": "done", "content": self.sink.run.content})
        return self.sink.run.to_dict()

    async def process_tool_calls(
        self,
        tool_calls: list[ToolCall],
        messages: list[dict[str, Any]],
        action_step: ActionStep,
    ) -> str | None:
        final_answer = None
        for tool_call in tool_calls:
            if tool_call.name == "final_answer":
                if len(tool_calls) > 1:
                    raise RuntimeError("final_answer must be the only tool call in a model turn")
                final_answer = _final_answer_from_arguments(tool_call.arguments)
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": final_answer})
                continue

            result = await self.execute_tool_call(tool_call)
            observation = tool_result_content(result)
            action_step.observations.append(observation)
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": observation})
            visual_message = _tool_result_visual_message(tool_call.name, result)
            if visual_message and self.accepts_image_input:
                messages.append(visual_message)
        return final_answer

    async def execute_tool_call(self, tool_call: ToolCall) -> dict[str, Any]:
        tool_name = tool_call.name
        tool_args = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        tool_step = self.sink.step("tool", f"Tool: {tool_name}", json.dumps(tool_args, indent=2))
        self.sink.emit(
            {
                "type": "tool_call",
                "summary": f"Calling {tool_name}",
                "tool_call_id": tool_call.id,
                "step_id": tool_step.id,
                "tool_name": tool_name,
                "arguments": tool_args,
            }
        )
        result = await asyncio.to_thread(
            self.executor.execute,
            {"type": tool_name, "params": tool_args or {}},
        )
        status = "completed" if result.get("status") == "success" else "failed"
        self.sink.finish_step(tool_step, status, json.dumps(client_tool_result(result), indent=2))
        self.sink.emit(
            {
                "type": "tool_result",
                "summary": f"{tool_name}: {status}",
                "tool_call_id": tool_call.id,
                "step_id": tool_step.id,
                "tool_name": tool_name,
                "arguments": tool_args,
                "result": client_tool_result(result),
                "status": status,
            }
        )
        return result

    def generate_final_answer(self, messages: list[dict[str, Any]]) -> str:
        messages = [
            *messages,
            {
                "role": "user",
                "content": (
                    "You have reached the maximum number of tool rounds. "
                    "Return the best concise final answer from the observations so far."
                ),
            },
        ]
        completion = self.model.generate(messages, tools=None)
        return completion.content

    def _retry_without_images(self, messages: list[dict[str, Any]], exc: Exception) -> bool:
        if not _is_image_input_unsupported_error(exc):
            return False
        if not any(_message_has_image_input(message) for message in messages):
            return False
        messages[:] = _strip_image_messages(messages)
        self.accepts_image_input = False
        return True


def _final_answer_from_arguments(arguments: Any) -> str:
    if isinstance(arguments, dict):
        return _final_output_text(arguments.get("answer", ""))
    return _final_output_text(arguments)


def _final_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value) if isinstance(value, (dict, list)) else str(value)


def run_tool_calling(payload: dict[str, Any], executor: Executor, sink: Sink) -> dict[str, Any]:
    return asyncio.run(run_tool_calling_async(payload, executor, sink))


async def run_tool_calling_async(payload: dict[str, Any], executor: Executor, sink: Sink) -> dict[str, Any]:
    api_key = payload.get("api_key") or ""
    if not api_key:
        raise RuntimeError("API key is required")

    model = OpenAICompatibleModel(
        model=payload.get("model") or DEFAULT_MODEL,
        api_key=api_key,
        base_url=payload.get("base_url") or DEFAULT_BASE_URL,
    )
    agent = ToolCallingAgent(
        model=model,
        executor=executor,
        sink=sink,
        max_steps=int(payload.get("max_tool_rounds") or 8),
    )
    return await agent.run(payload)

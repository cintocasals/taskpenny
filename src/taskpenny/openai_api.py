"""OpenAI-compatible chat completions on top of Taskpenny, so any tool can use it by changing only the base URL.

Point the tool at http://127.0.0.1:8765/v1 and use the model "taskpenny" (all providers) or "taskpenny/<profile>"
(for example "taskpenny/anthropic"). Each request is one Taskpenny run: Jev decides, cheap models work, Jev checks.

How a run's status reaches the client: "done" and "unverified" answer with finish_reason "stop" (Responses:
"completed"); "partial" with "length" (Responses: "incomplete"); a run with no answer is an HTTP 502 error. The
`taskpenny` object always carries the status and, for anything but "done", the warnings that explain it; the
X-Taskpenny-Status header carries the status too, except on a stream, whose headers go out before the run ends.
A simulated run (dry run) says so in `taskpenny.simulated` and the X-Taskpenny-Simulated header. Taskpenny decides the models and their limits itself, so sampling parameters are
ignored, and listed in `taskpenny.ignored`.
"""

from __future__ import annotations

import json
import time
from typing import Any


class BadRequest(ValueError):
    pass


IGNORED = ("max_tokens", "max_completion_tokens", "max_output_tokens", "temperature", "top_p", "stop", "seed",
           "presence_penalty", "frequency_penalty", "logit_bias", "logprobs", "top_logprobs", "user", "store",
           "metadata", "reasoning_effort", "reasoning", "parallel_tool_calls", "service_tier")
ANSWERED = ("done", "unverified")  # a complete answer; "unverified" says a check did not pass or could not run


def ignored_params(body: dict) -> list[str]:
    """Parameters Taskpenny does not apply (it picks the models and their limits), so the client can see them.
    Asking for several answers is refused: it would silently get one."""
    n = body.get("n")
    if n not in (None, 1, "1"):
        raise BadRequest("Taskpenny returns one answer per request: n must be 1")
    return [k for k in IGNORED if body.get(k) not in (None, "", [], {})]


def wants_stream(body: dict) -> bool:
    """stream: true (a boolean, or the text "true" some tools send). Anything else is a normal answer."""
    v = body.get("stream")
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):  # one part on its own
        content = [content]
    if isinstance(content, list):  # content parts: text only; images and files are refused, not silently dropped
        texts = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
                continue
            if not isinstance(part, dict):
                raise BadRequest("message content parts must be text or objects")
            kind = part.get("type")
            if kind in (None, "text", "input_text", "output_text") and "text" in part:
                texts.append(str(part.get("text") or ""))
            elif kind == "refusal":  # an earlier assistant refusal: nothing to pass on
                continue
            else:
                raise BadRequest(f"Taskpenny does not support {kind or 'this kind of'} content yet: send text")
        return "\n".join(texts)
    return str(content)


def request_from_messages(messages: Any) -> str:
    """Turn a chat history into the one request Taskpenny works on.

    One user message is passed as it is. With system instructions or earlier turns, they come first as
    context, and the last user message is marked as the request to answer.
    """
    if not isinstance(messages, list) or not messages:
        raise BadRequest("messages must be a non-empty list")
    system = [_text(m.get("content")) for m in messages if isinstance(m, dict) and m.get("role") in ("system",
                                                                                                    "developer")]
    turns = [m for m in messages if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    if not turns or turns[-1].get("role") != "user":
        raise BadRequest("the last message must come from the user")
    last = _text(turns[-1].get("content")).strip()
    if not last:
        raise BadRequest("the last user message is empty")
    history = turns[:-1]
    if not system and not history:
        return last
    parts = []
    if any(s.strip() for s in system):
        parts.append("Instructions to follow:\n" + "\n\n".join(s.strip() for s in system if s.strip()))
    if history:
        lines = [f"{'User' if m['role'] == 'user' else 'Assistant'}: {_text(m.get('content')).strip()}"
                 for m in history]
        parts.append("Conversation so far:\n" + "\n\n".join(lines))
    parts.append("Answer this new message from the user:\n" + last)
    return "\n\n".join(parts)


def profile_from_model(model: str, profiles: list[str]) -> str:
    """'taskpenny' -> 'all'; 'taskpenny/anthropic' -> 'anthropic'. Anything else is refused, so a typo does not
    spend."""
    name = (model or "taskpenny").strip()
    if name in ("taskpenny", "taskpenny/auto"):
        return "all"
    prefix = "taskpenny/"
    if name.startswith(prefix) and name[len(prefix):] in profiles:
        return name[len(prefix):]
    raise BadRequest(f"unknown model {name!r}: use 'taskpenny' or one of " +
                     ", ".join(f"'taskpenny/{p}'" for p in profiles))


def model_list(profiles: list[str]) -> dict:
    now = int(time.time())
    ids = ["taskpenny"] + [f"taskpenny/{p}" for p in profiles if p != "all"]
    return {"object": "list", "data": [{"id": i, "object": "model", "created": now, "owned_by": "taskpenny"} for i in ids]}


def completion(run: dict, model: str, ignored: list[str] | None = None) -> dict:
    r = run.get("receipt") or {}
    return {
        "id": f"chatcmpl-taskpenny-{run.get('id', '')}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": run.get("answer", "")},
                     "finish_reason": "stop" if run.get("status") in ANSWERED else "length"}],
        "usage": {"prompt_tokens": r.get("tokens_in", 0), "completion_tokens": r.get("tokens_out", 0),
                  "total_tokens": r.get("tokens_in", 0) + r.get("tokens_out", 0)},
        "taskpenny": extra(run, ignored),
    }


def extra(run: dict, ignored: list[str] | None = None) -> dict:
    r = run.get("receipt") or {}
    out = {"run_id": run.get("id"), "status": run.get("status"), "simulated": bool(r.get("simulated")),
           "warnings": list(run.get("warnings") or []),
           "cost_usd": r.get("total_cost"), "baseline_estimate_usd": (r.get("baseline") or {}).get("estimated_cost"),
           "saving_pct": r.get("saving_pct"), "error": run.get("error") or None}
    if ignored:
        out["ignored"] = list(ignored)
    return out


def stream_chunks(run: dict, model: str, ignored: list[str] | None = None) -> list[bytes]:
    """Server-sent events for stream=true. Taskpenny works on the whole request before answering, so the answer
    arrives in one piece at the end (keep-alive comments go out while it works); the format is the one OpenAI
    clients expect."""
    base = {"id": f"chatcmpl-taskpenny-{run.get('id', '')}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model}
    done = completion(run, model, ignored)

    def ev(obj: dict) -> bytes:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
    return [
        ev(base | {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}),
        ev(base | {"choices": [{"index": 0, "delta": {"content": run.get("answer", "")}, "finish_reason": None}]}),
        ev(base | {"choices": [{"index": 0, "delta": {}, "finish_reason": done["choices"][0]["finish_reason"]}],
                   "usage": done["usage"], "taskpenny": done["taskpenny"]}),
        b"data: [DONE]\n\n",
    ]


def error(message: str, kind: str = "invalid_request_error", code: str | None = None) -> dict:
    return {"error": {"message": message, "type": kind, "code": code}}


def stream_error(message: str, kind: str, responses: bool) -> list[bytes]:
    """An error after the stream has started: OpenAI clients raise it as an API error."""
    if responses:
        body = {"type": "error", "code": kind, "message": message, "param": None}
        return [f"event: error\ndata: {json.dumps(body, ensure_ascii=False)}\n\n".encode("utf-8")]
    return [f"data: {json.dumps(error(message, kind), ensure_ascii=False)}\n\n".encode("utf-8"), b"data: [DONE]\n\n"]


# --------------------------------------------------------------- Responses API
def request_from_responses(body: dict) -> str:
    """The Responses API (`/v1/responses`): `input` is a string or a list of message items; `instructions`
    plays the part of a system message. Both become the chat form Taskpenny already understands."""
    messages: list[dict] = []
    if str(body.get("instructions") or "").strip():
        messages.append({"role": "system", "content": str(body["instructions"])})
    inp = body.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in (None, "message"):
                raise BadRequest(f"input items of type {item.get('type')!r} are not supported yet")
            messages.append({"role": item.get("role") or "user", "content": _text(item.get("content"))})
    else:
        raise BadRequest("input must be a string or a list of messages")
    return request_from_messages(messages)


def response(run: dict, model: str, ignored: list[str] | None = None) -> dict:
    r = run.get("receipt") or {}
    text = run.get("answer", "")
    rid = f"resp_taskpenny_{run.get('id', '')}"
    complete = run.get("status") in ANSWERED
    return {
        "id": rid, "object": "response", "created_at": int(time.time()), "model": model,
        "status": "completed" if complete else "incomplete",
        "incomplete_details": None if complete else {"reason": "max_output_tokens"},
        "output": [{"type": "message", "id": f"msg_{rid}", "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        "output_text": text,
        "usage": {"input_tokens": r.get("tokens_in", 0), "output_tokens": r.get("tokens_out", 0),
                  "total_tokens": r.get("tokens_in", 0) + r.get("tokens_out", 0)},
        "taskpenny": extra(run, ignored),
    }


def response_events(run: dict, model: str, ignored: list[str] | None = None) -> list[bytes]:
    """Server-sent events for a streamed Responses API call, with the whole answer in one delta."""
    full = response(run, model, ignored)
    item = full["output"][0]
    text = full["output_text"]
    started = full | {"status": "in_progress", "output": []}

    def ev(kind: str, obj: dict) -> bytes:
        return f"event: {kind}\ndata: {json.dumps({'type': kind} | obj, ensure_ascii=False)}\n\n".encode("utf-8")
    return [
        ev("response.created", {"response": started}),
        ev("response.output_item.added", {"output_index": 0, "item": item | {"status": "in_progress", "content": []}}),
        ev("response.content_part.added", {"item_id": item["id"], "output_index": 0, "content_index": 0,
                                           "part": {"type": "output_text", "text": "", "annotations": []}}),
        ev("response.output_text.delta", {"item_id": item["id"], "output_index": 0, "content_index": 0, "delta": text}),
        ev("response.output_text.done", {"item_id": item["id"], "output_index": 0, "content_index": 0, "text": text}),
        ev("response.content_part.done", {"item_id": item["id"], "output_index": 0, "content_index": 0,
                                          "part": item["content"][0]}),
        ev("response.output_item.done", {"output_index": 0, "item": item}),
        ev("response.completed" if full["status"] == "completed" else "response.incomplete", {"response": full}),
    ]

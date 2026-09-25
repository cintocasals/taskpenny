"""OpenAI-compatible chat completions on top of SIAC, so any tool can use it by changing only the base URL.

Point the tool at http://127.0.0.1:8765/v1 and use the model "siac" (all providers) or "siac/<profile>"
(for example "siac/anthropic"). Each request is one SIAC run: Jev decides, cheap models work, Jev checks.
"""

from __future__ import annotations

import json
import time
from typing import Any


class BadRequest(ValueError):
    pass


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content parts: keep the text, images are not supported yet
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content)


def request_from_messages(messages: Any) -> str:
    """Turn a chat history into the one request SIAC works on.

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
    """'siac' -> 'all'; 'siac/anthropic' -> 'anthropic'. Anything else is refused, so a typo does not spend."""
    name = (model or "siac").strip()
    if name in ("siac", "siac/auto"):
        return "all"
    if name.startswith("siac/") and name[5:] in profiles:
        return name[5:]
    raise BadRequest(f"unknown model {name!r}: use 'siac' or one of " +
                     ", ".join(f"'siac/{p}'" for p in profiles))


def model_list(profiles: list[str]) -> dict:
    now = int(time.time())
    ids = ["siac"] + [f"siac/{p}" for p in profiles if p != "all"]
    return {"object": "list", "data": [{"id": i, "object": "model", "created": now, "owned_by": "siac"} for i in ids]}


def completion(run: dict, model: str) -> dict:
    r = run.get("receipt") or {}
    return {
        "id": f"chatcmpl-siac-{run.get('id', '')}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": run.get("answer", "")},
                     "finish_reason": "stop" if run.get("status") == "done" else "length"}],
        "usage": {"prompt_tokens": r.get("tokens_in", 0), "completion_tokens": r.get("tokens_out", 0),
                  "total_tokens": r.get("tokens_in", 0) + r.get("tokens_out", 0)},
        "siac": extra(run),
    }


def extra(run: dict) -> dict:
    r = run.get("receipt") or {}
    return {"run_id": run.get("id"), "status": run.get("status"), "cost_usd": r.get("total_cost"),
            "baseline_estimate_usd": (r.get("baseline") or {}).get("estimated_cost"),
            "saving_pct": r.get("saving_pct"), "error": run.get("error") or None}


def stream_chunks(run: dict, model: str) -> list[bytes]:
    """Server-sent events for stream=true. SIAC works on the whole request before answering, so the answer
    arrives in one piece at the end; the format is the one OpenAI clients expect."""
    base = {"id": f"chatcmpl-siac-{run.get('id', '')}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model}
    done = completion(run, model)

    def ev(obj: dict) -> bytes:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
    return [
        ev(base | {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}),
        ev(base | {"choices": [{"index": 0, "delta": {"content": run.get("answer", "")}, "finish_reason": None}]}),
        ev(base | {"choices": [{"index": 0, "delta": {}, "finish_reason": done["choices"][0]["finish_reason"]}],
                   "usage": done["usage"], "siac": done["siac"]}),
        b"data: [DONE]\n\n",
    ]


def error(message: str, kind: str = "invalid_request_error", code: str | None = None) -> dict:
    return {"error": {"message": message, "type": kind, "code": code}}


# --------------------------------------------------------------- Responses API
def request_from_responses(body: dict) -> str:
    """The Responses API (`/v1/responses`): `input` is a string or a list of message items; `instructions`
    plays the part of a system message. Both become the chat form SIAC already understands."""
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
            content = item.get("content")
            if isinstance(content, list):  # parts: input_text / output_text / text
                content = "\n".join(str(p.get("text", "")) for p in content
                                    if isinstance(p, dict) and p.get("type") in ("input_text", "output_text", "text"))
            messages.append({"role": item.get("role") or "user", "content": content or ""})
    else:
        raise BadRequest("input must be a string or a list of messages")
    return request_from_messages(messages)


def response(run: dict, model: str) -> dict:
    r = run.get("receipt") or {}
    text = run.get("answer", "")
    rid = f"resp_siac_{run.get('id', '')}"
    return {
        "id": rid, "object": "response", "created_at": int(time.time()), "model": model,
        "status": "completed" if run.get("status") == "done" else "incomplete",
        "output": [{"type": "message", "id": f"msg_{rid}", "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        "output_text": text,
        "usage": {"input_tokens": r.get("tokens_in", 0), "output_tokens": r.get("tokens_out", 0),
                  "total_tokens": r.get("tokens_in", 0) + r.get("tokens_out", 0)},
        "siac": extra(run),
    }


def response_events(run: dict, model: str) -> list[bytes]:
    """Server-sent events for a streamed Responses API call, with the whole answer in one delta."""
    full = response(run, model)
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
        ev("response.completed", {"response": full}),
    ]

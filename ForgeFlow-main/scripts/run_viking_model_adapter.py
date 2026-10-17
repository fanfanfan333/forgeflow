"""Authenticated local OpenAI bridge with native Ollama thinking and budget controls."""

from __future__ import annotations

import asyncio
import heapq
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException

CONFIG = Path(__file__).resolve().parents[1] / ".runtime/openviking/ov.conf"
pending: list = []
ready = asyncio.Event()
sequence = 0


async def native_worker():
    while True:
        await ready.wait()
        if not pending:
            ready.clear()
            continue
        _, _, payload, future = heapq.heappop(pending)
        if future.cancelled():
            continue
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=600) as client:
                response = await client.post("http://127.0.0.1:11434/api/chat", json=payload)
            if response.is_error:
                raise HTTPException(502, "Local model request failed")
            if not future.done():
                future.set_result(response.json())
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(native_worker())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "service": "forgeflow-viking-model-adapter",
        "healthy": True,
        "queued": len(pending),
        "learning_queued": sum(entry[0] == 0 for entry in pending),
    }


@app.post("/v1/chat/completions")
async def completion(body: dict, authorization: str = Header(default="")):
    global sequence
    config = json.loads(CONFIG.read_text(encoding="utf-8"))["vlm"]
    if not secrets.compare_digest(authorization, "Bearer " + config["api_key"]):
        raise HTTPException(401, "Invalid adapter credential")
    if body.get("model") != config["model"] or body.get("stream"):
        raise HTTPException(400, "Unsupported model or streaming")
    if len(pending) >= 128:
        raise HTTPException(429, "Local model queue is full")
    messages = []
    for message in body.get("messages", []):
        message = dict(message)
        if message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                function = dict(call["function"])
                if isinstance(function.get("arguments"), str):
                    function["arguments"] = json.loads(function["arguments"])
                calls.append({"function": function})
            message["tool_calls"] = calls
        messages.append(message)
    has_tools = bool(body.get("tools")) or any(
        message.get("role") == "tool" for message in messages
    )
    actual_model = config["model"] if has_tools else "qwen2.5vl:3b"
    payload = {
        "model": actual_model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {
            "num_ctx": 16384 if has_tools else 8192,
            "temperature": 0,
            "num_predict": max(1, min(int(body.get("max_tokens") or 2048), 2048)),
        },
    }
    if body.get("tools"):
        payload["tools"] = body["tools"]
    future = asyncio.get_running_loop().create_future()
    priority = 0 if body.get("tools") or any(m.get("role") == "tool" for m in messages) else 10
    if priority == 10 and any(
        term in str(messages).lower() for term in ("privacy", "sensitive", "敏感")
    ):
        priority = 1
    sequence += 1
    heapq.heappush(pending, (priority, sequence, payload, future))
    ready.set()
    result = await future
    message = result["message"]
    output_calls = [
        {
            "id": "call_" + uuid4().hex,
            "type": "function",
            "function": {
                "name": call["function"]["name"],
                "arguments": json.dumps(call["function"].get("arguments", {}), ensure_ascii=False),
            },
        }
        for call in message.get("tool_calls", [])
    ]
    assistant = {"role": "assistant", "content": message.get("content", "")}
    if output_calls:
        assistant["tool_calls"] = output_calls
    prompt_tokens, output_tokens = result.get("prompt_eval_count"), result.get("eval_count")
    return {
        "id": "chatcmpl-" + uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.get("model") or actual_model,
        "requested_model": config["model"],
        "choices": [
            {
                "index": 0,
                "message": assistant,
                "finish_reason": "tool_calls" if output_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": prompt_tokens + output_tokens
            if prompt_tokens is not None and output_tokens is not None
            else None,
        },
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=1934, log_level="warning")

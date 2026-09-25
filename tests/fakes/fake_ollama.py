"""A scripted stand-in for the Ollama HTTP API, for tests and offline end-to-end runs.

Implements the endpoints `citadel_gateway.ollama.OllamaProvider` uses -- /api/version,
/api/tags, /api/ps, /api/show, /api/chat (plain, schema-constrained, streaming),
/api/generate, /api/embed and /api/pull -- over real HTTP on a real socket, so the
provider's actual request/response code is what gets exercised.

What it does NOT do is think. Replies come from a pluggable `brain(model, messages,
schema) -> str`. The default brain produces the smallest value satisfying the requested
JSON schema; end-to-end tests install a scripted brain that recognises Citadel's own
prompts. None of this is reachable from product code: it lives under tests/, and the
structural detectors never scan tests/.

Embeddings are deterministic feature hashes of word tokens, so texts sharing words are
genuinely closer in cosine distance -- enough to exercise real pgvector ranking.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

Brain = Callable[[str, Sequence[Mapping[str, Any]], Optional[Mapping[str, Any]]], str]

EMBED_DIMENSIONS = 768
_WORD = re.compile(r"[a-z0-9]+(?:[-./][a-z0-9]+)*")
_GO_DURATION = re.compile(r"[-+]?(?:(?:\d+(?:\.\d*)?|\.\d+)(?:ns|us|µs|μs|ms|s|m|h))+")
_BARE_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")


def keep_alive_error(value: Any) -> Optional[str]:
    """How Ollama reads `keep_alive` (api.Duration), mirrored so a test fails where Ollama
    would: a JSON number is seconds; a string is a Go duration, which needs a unit unless it
    is "0". The real runtime answers 400 before it looks the model up -- found when the
    string "-1" failed every call on the demonstration machine."""
    if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return None
    if isinstance(value, str):
        if value == "0" or _GO_DURATION.fullmatch(value):
            return None
        if _BARE_NUMBER.fullmatch(value):
            return f'time: missing unit in duration "{value}"'
        return f'time: invalid duration "{value}"'
    return f"Unsupported type: '{type(value).__name__}'"


def hash_embedding(text: str, dimensions: int = EMBED_DIMENSIONS) -> list[float]:
    vector = [0.0] * dimensions
    for token in _WORD.findall(text.lower()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "little") % dimensions
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def minimal_instance(schema: Mapping[str, Any]) -> Any:
    """The smallest value satisfying a (simple) JSON schema."""
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for combinator in ("anyOf", "oneOf"):
        if combinator in schema and schema[combinator]:
            return minimal_instance(schema[combinator][0])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), kind[0] if kind else "null")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        return {key: minimal_instance(props.get(key, {})) for key in required}
    if kind == "array":
        count = int(schema.get("minItems") or 0)
        return [minimal_instance(schema.get("items") or {}) for _ in range(count)]
    if kind == "integer":
        return int(schema.get("minimum") or 0)
    if kind == "number":
        return float(schema.get("minimum") or 0)
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    length = int(schema.get("minLength") or 1)
    return "x" * length


def default_brain(model: str, messages: Sequence[Mapping[str, Any]], schema: Optional[Mapping[str, Any]]) -> str:
    if schema is not None:
        return json.dumps(minimal_instance(schema))
    last = messages[-1]["content"] if messages else ""
    return f"[{model}] {str(last)[:80]}"


class FakeOllama:
    """Start with `.start()`; `.url` is the base URL; `.stop()` shuts it down."""

    def __init__(
        self,
        installed: Iterable[str],
        *,
        brain: Brain = default_brain,
        vision_tags: Iterable[str] = (),
        thinking_tags: Iterable[str] = (),
        port: int = 0,
        latency_s: float = 0.0,
    ) -> None:
        self.installed: set[str] = {self._norm(t) for t in installed}
        self.loaded: dict[str, float] = {}
        self.brain = brain
        self.vision_tags = {self._norm(t) for t in vision_tags}
        self.thinking_tags = {self._norm(t) for t in thinking_tags}
        self.latency_s = latency_s
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.fail_next: dict[str, str] = {}  # tag -> error message, consumed once
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def _norm(tag: str) -> str:
        return tag if ":" in tag.split("/")[-1] else f"{tag}:latest"

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"http://{host_text}:{port}"

    def start(self) -> "FakeOllama":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _touch(self, tag: str, keep_alive: Any) -> None:
        with self._lock:
            if keep_alive in (0, "0"):
                self.loaded.pop(self._norm(tag), None)
            else:
                self.loaded[self._norm(tag)] = time.time()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # quiet
                return

            def _json(self, status: int, body: Any) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, lines: Iterable[Mapping[str, Any]]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                for line in lines:
                    self.wfile.write((json.dumps(line) + "\n").encode("utf-8"))
                    self.wfile.flush()

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                parsed = json.loads(raw or b"{}")
                return parsed if isinstance(parsed, dict) else {}

            def do_GET(self) -> None:
                if self.path == "/api/version":
                    self._json(200, {"version": "0.0.0-fake"})
                elif self.path == "/api/tags":
                    self._json(200, {"models": [{"name": t, "size": 1_000_000, "digest": "sha256:fake"} for t in sorted(fake.installed)]})
                elif self.path == "/api/ps":
                    with fake._lock:
                        loaded = sorted(fake.loaded)
                    self._json(200, {"models": [{"name": t, "size": 1_000_000, "size_vram": 900_000} for t in loaded]})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                body = self._body()
                fake.requests.append((self.path, body))
                tag = fake._norm(str(body.get("model") or ""))
                if self.path == "/api/pull":
                    def progress() -> Iterable[Mapping[str, Any]]:
                        yield {"status": "pulling manifest"}
                        for done in (0, 50, 100):
                            yield {"status": "downloading", "total": 100, "completed": done}
                        with fake._lock:
                            fake.installed.add(tag)
                        yield {"status": "success"}
                    self._stream(progress())
                    return
                if self.path in ("/api/chat", "/api/generate", "/api/embed") and "keep_alive" in body:
                    problem = keep_alive_error(body["keep_alive"])
                    if problem:
                        self._json(400, {"error": problem})
                        return
                if tag not in fake.installed:
                    self._json(404, {"error": f"model '{body.get('model')}' not found, try pulling it first"})
                    return
                if self.path == "/api/show":
                    caps = ["completion"]
                    if tag in fake.vision_tags:
                        caps.append("vision")
                    if tag in fake.thinking_tags:
                        caps.append("thinking")
                    self._json(200, {"capabilities": caps})
                    return
                if self.path == "/api/generate":
                    fake._touch(tag, body.get("keep_alive"))
                    self._json(200, {"model": tag, "response": "", "done": True})
                    return
                if self.path == "/api/embed":
                    fake._touch(tag, body.get("keep_alive"))
                    inputs = body.get("input") or []
                    if isinstance(inputs, str):
                        inputs = [inputs]
                    self._json(200, {"model": tag, "embeddings": [hash_embedding(str(t)) for t in inputs], "prompt_eval_count": len(inputs)})
                    return
                if self.path == "/api/chat":
                    if tag in fake.fail_next:
                        self._json(500, {"error": fake.fail_next.pop(tag)})
                        return
                    fake._touch(tag, body.get("keep_alive"))
                    if fake.latency_s:
                        time.sleep(fake.latency_s)
                    schema = body.get("format") if isinstance(body.get("format"), dict) else None
                    messages = body.get("messages") or []
                    content = fake.brain(tag, messages, schema)
                    prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in messages)
                    completion_tokens = len(content.split())
                    if body.get("stream"):
                        words = content.split(" ")
                        def chunks() -> Iterable[Mapping[str, Any]]:
                            for i, word in enumerate(words):
                                yield {"model": tag, "message": {"role": "assistant", "content": word if i == 0 else " " + word}, "done": False}
                            yield {"model": tag, "message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": prompt_tokens, "eval_count": completion_tokens}
                        self._stream(chunks())
                        return
                    self._json(200, {"model": tag, "message": {"role": "assistant", "content": content}, "done": True, "prompt_eval_count": prompt_tokens, "eval_count": completion_tokens})
                    return
                self._json(404, {"error": "not found"})

        return Handler


def main() -> None:
    """A stand-in runtime for trying Citadel without a GPU.

        python tests/fakes/fake_ollama.py --scripted --port 11435

    `--scripted` answers Citadel's own prompts (tests/fakes/scripted_brain.py) and
    installs every model the demo-local registry names; otherwise it serves the given
    `--tags` with the minimal-schema brain."""
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=11435)
    parser.add_argument("--tags", nargs="*", default=[])
    parser.add_argument("--vision", nargs="*", default=[])
    parser.add_argument("--scripted", action="store_true")
    args = parser.parse_args()
    brain: Brain = default_brain
    tags, vision = list(args.tags), list(args.vision)
    if args.scripted:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from scripted_brain import citadel_brain
        from stack_fixtures import enabled_registry, image_tags

        registry = enabled_registry()
        brain = citadel_brain
        tags = tags or [m.tag for m in registry.models]
        vision = vision or image_tags(registry)
    server = FakeOllama(tags, vision_tags=vision, port=args.port, brain=brain).start()
    print(f"fake ollama on {server.url} serving {', '.join(sorted(server.installed))}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()

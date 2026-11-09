"""Minimal Ollama client for ladder rung 2. Stdlib only (urllib), same
pattern as MeetingTruth/ollama_client.py. No pip, no cloud."""
import json
import os
import urllib.request

HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")


def _post(path, payload, timeout):
    req = urllib.request.Request(
        HOST + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ping(timeout=5):
    """Fast connectivity check, no inference. Returns list of model names."""
    req = urllib.request.Request(HOST + "/api/tags", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return [m.get("name", "?") for m in data.get("models", [])]


def chat(model, messages, timeout=120, num_ctx=8192):
    resp = _post("/api/chat", {
        "model": model, "messages": messages, "stream": False,
        "options": {"num_ctx": num_ctx}}, timeout)
    return resp.get("message", {}).get("content", "").strip()

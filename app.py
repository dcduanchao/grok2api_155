"""Small, dependency-free media generation gateway and web UI server.

The gateway deliberately keeps upstream-specific details configurable.  Set
``upstream_base_url`` and ``upstream_api_key`` in config.json (or environment
variables) to connect it to an existing Grok-compatible gateway.
"""
from __future__ import annotations

import base64
import copy
import io
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("GROK2API_CONFIG", ROOT / "config.json"))
DB_PATH = Path(os.getenv("GROK2API_DB", ROOT / "data" / "media.db"))
INDEX_PATH = ROOT / "index.html"
LOG_PATH = Path(os.getenv("GROK2API_LOG", ROOT / "grok2api.log"))
DB_LOCK = threading.Lock()


def load_config() -> dict:
    defaults = {
        "host": "127.0.0.1",
        "port": 8000,
        "api_key": "",
        "upstream_base_url": "",
        "upstream_api_key": "",
        "imgbb_api_key": "",
        "request_timeout": 120,
        "comfyui_base_url": "http://192.168.90.65:8188",
        "comfyui_client_id": "grok2api-comfyui",
        "comfyui_request_timeout": 30,
        "comfyui_poll_timeout": 1800,
        "comfyui_workflows": {
            "text_to_image": "workflows/z_image_turbo_16.json",
            "image_edit": "workflows/qwen_edit_all.json",
        },
        "comfyui_workflow_mapping": {
            "text_to_image": {
                "positive_prompt": {"node_id": "2", "input": "text"},
                "negative_prompt": {"node_id": "12", "input": "text"},
                "seed": {"node_id": "17", "input": "seed"},
                "width": {"node_id": "14", "input": "width"},
                "height": {"node_id": "14", "input": "height"},
            },
            "image_edit": {
                "positive_prompt": {"node_id": "18", "input": "prompt"},
                "negative_prompt": {"node_id": "5", "input": "text"},
                "input_image": {"node_id": "14", "input": "image"},
                "seed": {"node_id": "19", "input": "seed"},
            },
        },
    }
    try:
        values = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            values = {}
    except (FileNotFoundError, json.JSONDecodeError):
        values = {}
    defaults.update(values)
    # Environment variables are useful for containers and do not replace the
    # documented config-file workflow.
    for key, env_name in {
        "api_key": "GROK2API_API_KEY",
        "upstream_base_url": "GROK2API_UPSTREAM_URL",
        "upstream_api_key": "GROK2API_UPSTREAM_KEY",
        "imgbb_api_key": "IMGBB_API_KEY",
    }.items():
        if os.getenv(env_name):
            defaults[key] = os.environ[env_name]
    return defaults


CONFIG = load_config()


def _log_value(value: object, key: str = "") -> object:
    """Return a short, secret-safe value for request/response tracing."""
    lowered = key.lower()
    if any(word in lowered for word in ("key", "token", "authorization", "secret", "password")):
        return "***"
    if isinstance(value, dict):
        return {str(k): _log_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_log_value(v, key) for v in value[:20]] + (["…"] if len(value) > 20 else [])
    if isinstance(value, (bytes, bytearray)):
        return f"<binary {len(value)} bytes>"
    text = str(value)
    return text if len(text) <= 1200 else text[:1200] + "…"


def trace(label: str, **fields: object) -> None:
    details = " ".join(f"{name}={json.dumps(_log_value(value, name), ensure_ascii=False)}" for name, value in fields.items())
    line = f"[trace] {label} {details}".rstrip()
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(line + "\n")
    except OSError:
        pass


def upstream_auth_header() -> str | None:
    key = str(CONFIG.get("upstream_api_key") or CONFIG.get("api_key") or "").strip()
    return "Bearer " + key if key else None


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT,
                data_uri TEXT, title TEXT, prompt TEXT, model TEXT,
                status TEXT, metadata TEXT, created_at INTEGER NOT NULL
            )"""
        )
        db.commit()


def save_media(kind: str, *, url: str | None = None, data_uri: str | None = None,
               title: str = "", prompt: str = "", model: str = "",
               status: str = "done", metadata: dict | None = None,
               media_id: str | None = None) -> dict:
    item = {
        "id": media_id or uuid.uuid4().hex,
        "kind": kind,
        "url": url,
        "data_uri": data_uri,
        "title": title,
        "prompt": prompt,
        "model": model,
        "status": status,
        "metadata": metadata or {},
        "created_at": int(time.time()),
    }
    with DB_LOCK, sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT OR REPLACE INTO media(id,kind,url,data_uri,title,prompt,model,status,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (item["id"], item["kind"], item["url"], item["data_uri"], item["title"],
             item["prompt"], item["model"], item["status"], json.dumps(item["metadata"]), item["created_at"]),
        )
        db.commit()
    return item


def update_media(media_id: str, **changes) -> dict | None:
    with DB_LOCK, sqlite3.connect(DB_PATH) as db:
        columns = [k for k in changes if k in {"url", "data_uri", "status", "metadata", "title", "prompt", "model"}]
        if not columns:
            return get_media(media_id)
        values = [json.dumps(changes[k]) if k == "metadata" else changes[k] for k in columns]
        db.execute(f"UPDATE media SET {', '.join(c + '=?' for c in columns)} WHERE id=?", values + [media_id])
        db.commit()
    return get_media(media_id)


def get_media(media_id: str) -> dict | None:
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["metadata"] = json.loads(item["metadata"] or "{}")
    return item


def delete_media(media_id: str) -> bool:
    with DB_LOCK, sqlite3.connect(DB_PATH) as db:
        cursor = db.execute("DELETE FROM media WHERE id=?", (media_id,))
        db.commit()
        return cursor.rowcount > 0


def delete_media_many(media_ids: list[str]) -> int:
    ids = [str(value).strip() for value in media_ids if str(value).strip()]
    if not ids:
        return 0
    with DB_LOCK, sqlite3.connect(DB_PATH) as db:
        placeholders = ",".join("?" for _ in ids)
        cursor = db.execute(f"DELETE FROM media WHERE id IN ({placeholders})", ids)
        db.commit()
        return cursor.rowcount


def delete_media_kind(kind: str | None = None) -> int:
    with DB_LOCK, sqlite3.connect(DB_PATH) as db:
        if kind in {"image", "video", "upload"}:
            cursor = db.execute("DELETE FROM media WHERE kind=?", (kind,))
        else:
            cursor = db.execute("DELETE FROM media")
        db.commit()
        return cursor.rowcount


def list_media(kind: str | None = None, limit: int = 100) -> list[dict]:
    try:
        limit = int(limit or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        if kind in {"image", "video", "upload"}:
            rows = db.execute("SELECT * FROM media WHERE kind=? ORDER BY created_at DESC LIMIT ?", (kind, limit)).fetchall()
        else:
            rows = db.execute("SELECT * FROM media ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item["metadata"] or "{}")
        result.append(item)
    return result


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def upstream_request(method: str, path: str, payload: dict | None = None, authorization: str | None = None) -> tuple[int, dict]:
    base = str(CONFIG.get("upstream_base_url", "")).strip().rstrip("/")
    if not base:
        trace("upstream.skip", method=method, url=path, params=payload or {}, status=503)
        return 503, {"error": {"message": "upstream_base_url is not configured in config.json", "type": "configuration_error"}}
    target = urljoin(base + "/", path.lstrip("/"))
    trace("upstream.request", method=method, url=target, params=payload or {})
    headers = {"Accept": "application/json"}
    configured_auth = upstream_auth_header()
    if configured_auth:
        headers["Authorization"] = configured_auth
    elif authorization:
        headers["Authorization"] = authorization
    body = None
    if payload is not None:
        body = json_bytes(payload)
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(target, data=body, headers=headers, method=method), timeout=float(CONFIG.get("request_timeout", 120))) as response:
            raw = response.read()
            try:
                result = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                result = {"raw": raw.decode("utf-8", "replace")}
            result = result if isinstance(result, dict) else {"data": result}
            trace("upstream.response", method=method, url=target, status=response.status, result=result)
            return response.status, result
    except HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8"))
        except Exception:
            result = {"error": {"message": str(exc)}}
        trace("upstream.response", method=method, url=target, status=exc.code, result=result)
        return exc.code, result
    except (URLError, TimeoutError, OSError) as exc:
        result = {"error": {"message": f"upstream request failed: {exc}", "type": "upstream_error"}}
        trace("upstream.response", method=method, url=target, status=502, result=result)
        return 502, result


def extract_url(value: object) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://", "data:")):
        return value
    if isinstance(value, dict):
        for key in ("url", "uri", "src"):
            found = extract_url(value.get(key))
            if found:
                return found
        for child in value.values():
            found = extract_url(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = extract_url(child)
            if found:
                return found
    return None


def persist_generation(response: dict, kind: str, payload: dict, request_id: str | None = None) -> None:
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            url = extract_url(entry.get("url"))
            b64 = entry.get("b64_json")
            data_uri = None
            if b64:
                data_uri = b64 if str(b64).startswith("data:") else "data:image/png;base64," + str(b64)
            if url or data_uri:
                save_media(kind, url=url, data_uri=data_uri, prompt=str(payload.get("prompt", "")), model=str(payload.get("model", "")), metadata=entry)
    url = extract_url(response.get("video")) if kind == "video" else None
    if url:
        save_media(kind, url=url, prompt=str(payload.get("prompt", "")), model=str(payload.get("model", "")), metadata=response)


def comfyui_base_url() -> str:
    return str(CONFIG.get("comfyui_base_url", "")).strip().rstrip("/")


def comfyui_error(message: str, error_type: str = "upstream_error") -> dict:
    return {"error": {"message": message, "type": error_type}}


def comfyui_request(method: str, path: str, payload: object | None = None) -> tuple[int, object]:
    base = comfyui_base_url()
    if not base:
        return 503, comfyui_error("comfyui_base_url is not configured in config.json", "configuration_error")
    target = urljoin(base + "/", path.lstrip("/"))
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json_bytes(payload)
        headers["Content-Type"] = "application/json"
    trace("comfyui.request", method=method, url=target, params=payload or {})
    try:
        with urlopen(Request(target, data=body, headers=headers, method=method), timeout=float(CONFIG.get("comfyui_request_timeout", 30))) as response:
            raw = response.read()
            try:
                result = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                result = {"raw": raw.decode("utf-8", "replace")}
            trace("comfyui.response", method=method, url=target, status=response.status, result=result)
            return response.status, result
    except HTTPError as exc:
        raw = exc.read()
        try:
            result = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            result = comfyui_error(raw.decode("utf-8", "replace") or str(exc))
        trace("comfyui.response", method=method, url=target, status=exc.code, result=result)
        return exc.code, result
    except (URLError, TimeoutError, OSError) as exc:
        result = comfyui_error(f"ComfyUI request failed: {exc}")
        trace("comfyui.response", method=method, url=target, status=502, result=result)
        return 502, result


def comfyui_upload_image(image: bytes, filename: str) -> tuple[int, object]:
    base = comfyui_base_url()
    if not base:
        return 503, comfyui_error("comfyui_base_url is not configured in config.json", "configuration_error")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "upload.png").strip("._") or "upload.png"
    content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    boundary = "----grok2api-" + uuid.uuid4().hex
    fields = [
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8"),
        image,
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(fields)
    target = urljoin(base + "/", "upload/image")
    trace(
        "comfyui.upload.request",
        method="POST",
        url=target,
        params={
            "image": {"filename": safe_name, "content_type": content_type, "bytes": len(image)},
            "overwrite": True,
        },
    )
    try:
        with urlopen(Request(target, data=body, headers={"Content-Type": "multipart/form-data; boundary=" + boundary, "Accept": "application/json"}, method="POST"), timeout=float(CONFIG.get("comfyui_request_timeout", 30))) as response:
            raw = response.read()
            try:
                result = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                result = {"name": safe_name, "raw": raw.decode("utf-8", "replace")}
            trace("comfyui.upload.response", method="POST", url=target, status=response.status, result=result)
            return response.status, result if isinstance(result, dict) else {"data": result}
    except HTTPError as exc:
        raw = exc.read()
        try:
            result = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            result = comfyui_error(raw.decode("utf-8", "replace") or str(exc))
        trace("comfyui.upload.response", method="POST", url=target, status=exc.code, result=result)
        return exc.code, result
    except (URLError, TimeoutError, OSError) as exc:
        result = comfyui_error(f"ComfyUI upload failed: {exc}")
        trace("comfyui.upload.response", method="POST", url=target, status=502, result=result)
        return 502, result


def comfyui_workflow_config(workflow_name: str) -> tuple[dict | None, dict | None]:
    workflows = CONFIG.get("comfyui_workflows") or {}
    mapping = CONFIG.get("comfyui_workflow_mapping") or {}
    relative_path = workflows.get(workflow_name)
    if not relative_path:
        return None, comfyui_error(f"unknown ComfyUI workflow: {workflow_name}", "invalid_request")
    workflow_path = (ROOT / str(relative_path)).resolve()
    try:
        workflow_path.relative_to(ROOT)
    except ValueError:
        return None, comfyui_error("ComfyUI workflow path must stay inside the project", "configuration_error")
    try:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, comfyui_error(f"workflow file not found: {relative_path}", "configuration_error")
    except json.JSONDecodeError as exc:
        return None, comfyui_error(f"workflow JSON is invalid: {exc}", "configuration_error")
    if not isinstance(workflow, dict) or not workflow:
        return None, comfyui_error("workflow JSON must be a non-empty object", "configuration_error")
    return {"prompt": workflow, "mapping": mapping.get(workflow_name) or {}}, None


def comfyui_set_input(prompt: dict, mapping: dict, name: str, value: object) -> None:
    spec = mapping.get(name)
    if not isinstance(spec, dict) or value is None:
        return
    node_id = str(spec.get("node_id", ""))
    input_name = str(spec.get("input", ""))
    if node_id in prompt and input_name:
        inputs = prompt[node_id].setdefault("inputs", {})
        inputs[input_name] = value


def comfyui_outputs(history: object, prompt_id: str) -> list[dict]:
    record = history.get(prompt_id) if isinstance(history, dict) else None
    if not isinstance(record, dict):
        return []
    outputs = record.get("outputs") or {}
    result = []
    if not isinstance(outputs, dict):
        return result
    for node_id, node_output in outputs.items():
        if not isinstance(node_output, dict):
            continue
        for media_type, output_type in (("images", "image"), ("videos", "video"), ("gifs", "video")):
            for item in node_output.get(media_type) or []:
                if isinstance(item, dict) and item.get("filename"):
                    result.append({
                        "type": output_type,
                        "filename": str(item.get("filename")),
                        "subfolder": str(item.get("subfolder") or ""),
                        "comfy_type": str(item.get("type") or "output"),
                        "node_id": str(node_id),
                    })
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "GrokMedia/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def send_json(self, value: object, status: int = 200) -> None:
        body = json_bytes(value)
        trace("response", method=self.command, url=self.path, status=status, result=value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str, error_type: str = "invalid_request") -> None:
        self.send_json({"error": {"message": message, "type": error_type}}, status)

    def authorized(self) -> bool:
        expected = str(CONFIG.get("api_key", ""))
        if not expected:
            return True
        return self.headers.get("Authorization", "") == "Bearer " + expected

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 10 * 1024 * 1024:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        trace("request", method=self.command, url=self.path, params=value)
        return value

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            body = INDEX_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/health":
            self.send_json({"ok": True, "configured": bool(CONFIG.get("upstream_base_url")), "time": int(time.time())})
            return
        if path == "/api/comfyui/health":
            status, result = comfyui_request("GET", "/system_stats")
            if status >= 400:
                self.send_json(result, status)
                return
            queue_status, queue = comfyui_request("GET", "/queue")
            running = len(queue.get("queue_running", [])) if isinstance(queue, dict) else None
            pending = len(queue.get("queue_pending", [])) if isinstance(queue, dict) else None
            self.send_json({"ok": True, "base_url": comfyui_base_url(), "queue_running": running, "queue_pending": pending, "system": result})
            return
        if path == "/api/comfyui/workflows":
            workflows = CONFIG.get("comfyui_workflows") or {}
            data = []
            for name, relative_path in workflows.items():
                workflow_path = (ROOT / str(relative_path)).resolve()
                available = False
                error = ""
                try:
                    workflow_path.relative_to(ROOT)
                    json.loads(workflow_path.read_text(encoding="utf-8"))
                    available = True
                except FileNotFoundError:
                    error = "workflow file not found"
                except (ValueError, json.JSONDecodeError) as exc:
                    error = str(exc)
                data.append({"name": name, "display_name": {"text_to_image": "文生图", "image_edit": "图片编辑"}.get(name, name), "available": available, "error": error})
            self.send_json({"data": data, "base_url": comfyui_base_url()})
            return
        if path.startswith("/api/comfyui/tasks/") and path.count("/") == 4:
            task_id = path.rsplit("/", 1)[-1]
            row = get_media(task_id)
            if not row or row.get("metadata", {}).get("source") != "comfyui":
                self.send_error_json(404, "ComfyUI task not found", "not_found")
                return
            metadata = row.get("metadata") or {}
            prompt_id = str(metadata.get("prompt_id") or "")
            status, history = comfyui_request("GET", "/history/" + quote(prompt_id, safe="")) if prompt_id else (400, {})
            if status < 400:
                outputs = comfyui_outputs(history, prompt_id)
                if outputs:
                    output_items = []
                    for index, output in enumerate(outputs):
                        if index == 0:
                            media_id = task_id
                            media_url = "/api/comfyui/media/" + media_id
                            update_media(media_id, url=media_url, model="ComfyUI · " + str(metadata.get("workflow", "")), metadata={**metadata, **output})
                        else:
                            media = save_media("image", prompt=row.get("prompt", ""), model="ComfyUI · " + str(metadata.get("workflow", "")), metadata={**metadata, **output})
                            media_id = media["id"]
                            media_url = "/api/comfyui/media/" + media_id
                            update_media(media_id, url=media_url)
                        output_items.append({**output, "media_id": media_id, "url": media_url})
                    metadata["outputs"] = output_items
                    update_media(task_id, status="done", metadata=metadata)
                    self.send_json({"task_id": task_id, "prompt_id": prompt_id, "workflow": metadata.get("workflow"), "status": "done", "progress": 100, "outputs": output_items})
                    return
                status_info = history.get(prompt_id) if isinstance(history, dict) else None
                if isinstance(status_info, dict) and status_info.get("status", {}).get("status_str") == "error":
                    metadata["error"] = status_info.get("status")
                    update_media(task_id, status="failed", metadata=metadata)
                    self.send_json({"task_id": task_id, "prompt_id": prompt_id, "status": "failed", "progress": 0, "error": metadata["error"]})
                    return
            self.send_json({"task_id": task_id, "prompt_id": prompt_id, "workflow": metadata.get("workflow"), "status": row.get("status") or "pending", "progress": 0})
            return
        if path.startswith("/api/comfyui/media/") and path.count("/") == 4:
            media_id = path.rsplit("/", 1)[-1]
            item = get_media(media_id)
            metadata = item.get("metadata", {}) if item else {}
            if metadata.get("source") == "comfyui" and not metadata.get("filename"):
                for output in metadata.get("outputs") or []:
                    if isinstance(output, dict) and str(output.get("media_id")) == media_id:
                        metadata = {**metadata, **output}
                        break
            if not item or metadata.get("source") != "comfyui" or not metadata.get("filename"):
                self.send_error_json(404, "ComfyUI media not found", "not_found")
                return
            query = urlencode({"filename": metadata["filename"], "subfolder": metadata.get("subfolder", ""), "type": metadata.get("comfy_type", "output")})
            target = urljoin(comfyui_base_url() + "/", "view?" + query)
            trace(
                "comfyui.media.request",
                method="GET",
                url=target,
                params={
                    "filename": metadata["filename"],
                    "subfolder": metadata.get("subfolder", ""),
                    "type": metadata.get("comfy_type", "output"),
                },
            )
            try:
                with urlopen(Request(target, headers={"Accept": "image/*,video/*,*/*"}, method="GET"), timeout=float(CONFIG.get("comfyui_request_timeout", 30))) as upstream:
                    self.send_response(upstream.status)
                    self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/octet-stream"))
                    if upstream.headers.get("Content-Length"):
                        self.send_header("Content-Length", upstream.headers["Content-Length"])
                    self.end_headers()
                    while True:
                        chunk = upstream.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                self.send_error_json(502, f"ComfyUI media fetch failed: {exc}", "upstream_error")
            return
        if path in {"/v1/media/history", "/api/history"}:
            query = parse_qs(parsed.query)
            self.send_json({"data": list_media(query.get("kind", [None])[0], query.get("limit", [100])[0])})
            return
        if (path.startswith("/v1/videos/") or path.startswith("/api/videos/")) and path.count("/") == 3:
            if not self.authorized():
                # /api/videos is a same-origin browser alias; /v1/videos keeps
                # the normal client authentication requirement.
                if not path.startswith("/api/videos/"):
                    self.send_error_json(401, "invalid API key", "authentication_error")
                    return
            request_id = path.rsplit("/", 1)[-1]
            upstream_path = "/v1/videos/" + request_id
            status, result = upstream_request("GET", upstream_path, authorization=self.headers.get("Authorization"))
            if status < 400:
                url = extract_url(result.get("video"))
                row = next((x for x in list_media("video", 500) if x["metadata"].get("request_id") == request_id), None)
                if row:
                    changes = {"status": str(result.get("status") or row.get("status") or "pending"), "metadata": result}
                    if url:
                        changes["url"] = url
                    update_media(row["id"], **changes)
                    result.setdefault("media_id", row["id"])
                elif url:
                    saved = save_media("video", url=url, status="done", metadata={**result, "request_id": request_id})
                    result.setdefault("media_id", saved["id"])
            self.send_json(result, status)
            return
        if (path.startswith("/api/media/videos/") or path.startswith("/v1/media/videos/")) and path.count("/") == 4:
            if path.startswith("/v1/") and not self.authorized():
                self.send_error_json(401, "invalid API key", "authentication_error")
                return
            media = get_media(path.rsplit("/", 1)[-1])
            if not media or media.get("kind") != "video" or not media.get("url"):
                self.send_error_json(404, "video media not found", "not_found")
                return
            auth = upstream_auth_header()
            headers = {"Accept": "video/*,*/*"}
            if auth:
                headers["Authorization"] = auth
            if self.headers.get("Range"):
                headers["Range"] = self.headers["Range"]
            trace("media.request", method="GET", url=media["url"], params={"media_id": media["id"], "range": self.headers.get("Range") or ""})
            try:
                with urlopen(Request(media["url"], headers=headers, method="GET"), timeout=float(CONFIG.get("request_timeout", 120))) as upstream:
                    self.send_response(upstream.status)
                    self.send_header("Content-Type", upstream.headers.get("Content-Type", "video/mp4"))
                    if upstream.headers.get("Content-Length"):
                        self.send_header("Content-Length", upstream.headers["Content-Length"])
                    for name in ("Content-Range", "Accept-Ranges", "Cache-Control", "Last-Modified", "ETag"):
                        if upstream.headers.get(name):
                            self.send_header(name, upstream.headers[name])
                    self.end_headers()
                    while True:
                        chunk = upstream.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                trace("media.response", method="GET", url=media["url"], status=upstream.status, result={"media_id": media["id"]})
            except HTTPError as exc:
                trace("media.response", method="GET", url=media["url"], status=exc.code, result={"media_id": media["id"]})
                self.send_error_json(exc.code, "video fetch failed", "upstream_error")
            except (URLError, TimeoutError, OSError) as exc:
                trace("media.response", method="GET", url=media["url"], status=502, result={"error": str(exc)})
                self.send_error_json(502, f"video fetch failed: {exc}", "upstream_error")
            return
        if path.startswith("/v1/media/images/"):
            item = get_media(path.rsplit("/", 1)[-1])
            if item and item.get("url"):
                self.send_response(302)
                self.send_header("Location", item["url"])
                self.end_headers()
                return
            self.send_error_json(404, "media not found", "not_found")
            return
        self.send_error_json(404, "not found", "not_found")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        trace("request", method="DELETE", url=self.path, params={})
        if path.startswith("/v1/") and not self.authorized():
            self.send_error_json(401, "invalid API key", "authentication_error")
            return
        if path in {"/api/history", "/v1/media/history"}:
            kind = parse_qs(parsed.query).get("kind", [None])[0]
            if kind not in {None, "image", "video", "upload"}:
                self.send_error_json(400, "kind must be image, video, or upload")
                return
            count = delete_media_kind(kind)
            self.send_json({"deleted": count, "kind": kind or "all"})
            return
        prefix = "/api/history/"
        v1_prefix = "/v1/media/history/"
        if path.startswith(prefix) or path.startswith(v1_prefix):
            media_id = path[len(prefix):] if path.startswith(prefix) else path[len(v1_prefix):]
            if not media_id or "/" in media_id:
                self.send_error_json(400, "media id is required")
                return
            if not delete_media(media_id):
                self.send_error_json(404, "media not found", "not_found")
                return
            self.send_json({"deleted": [media_id]})
            return
        self.send_error_json(404, "not found", "not_found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/v1/") and not self.authorized():
            self.send_error_json(401, "invalid API key", "authentication_error")
            return
        if path == "/api/comfyui/generations":
            try:
                payload = self.read_json()
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_error_json(400, str(exc))
                return
            workflow_name = str(payload.get("workflow") or "text_to_image").strip()
            prompt_text = str(payload.get("prompt") or "").strip()
            if workflow_name not in {"text_to_image", "image_edit"}:
                self.send_error_json(400, "workflow must be text_to_image or image_edit", "invalid_request")
                return
            if not prompt_text:
                self.send_error_json(400, "prompt is required", "invalid_request")
                return
            workflow_config, error = comfyui_workflow_config(workflow_name)
            if error:
                self.send_json(error, 503 if error["error"]["type"] == "configuration_error" else 400)
                return
            prompt_graph = copy.deepcopy(workflow_config["prompt"])
            mapping = workflow_config["mapping"]
            comfyui_set_input(prompt_graph, mapping, "positive_prompt", prompt_text)
            comfyui_set_input(prompt_graph, mapping, "negative_prompt", str(payload.get("negative_prompt") or ""))
            uploaded_image_name = ""
            if workflow_name == "image_edit":
                image_payload = payload.get("image")
                if not isinstance(image_payload, dict):
                    self.send_error_json(400, "image is required for image_edit", "invalid_request")
                    return
                try:
                    image, filename = self.parse_image_payload(image_payload)
                except ValueError as exc:
                    self.send_error_json(400, str(exc), "invalid_request")
                    return
                status, upload_result = comfyui_upload_image(image, filename)
                if status >= 400 or not isinstance(upload_result, dict):
                    self.send_json(upload_result, status if status >= 400 else 502)
                    return
                uploaded_image_name = str(upload_result.get("name") or upload_result.get("filename") or filename)
                comfyui_set_input(prompt_graph, mapping, "input_image", uploaded_image_name)
            if payload.get("seed") is not None:
                try:
                    seed = int(payload["seed"])
                except (TypeError, ValueError):
                    self.send_error_json(400, "seed must be an integer", "invalid_request")
                    return
                comfyui_set_input(prompt_graph, mapping, "seed", seed)
            for field in ("width", "height"):
                if payload.get(field) is not None:
                    try:
                        value = int(payload[field])
                    except (TypeError, ValueError):
                        self.send_error_json(400, f"{field} must be an integer", "invalid_request")
                        return
                    if value <= 0 or value > 8192:
                        self.send_error_json(400, f"{field} must be between 1 and 8192", "invalid_request")
                        return
                    comfyui_set_input(prompt_graph, mapping, field, value)
            request = {"prompt": prompt_graph, "client_id": str(CONFIG.get("comfyui_client_id") or "grok2api-comfyui")}
            trace(
                "comfyui.prompt.resolved",
                workflow=workflow_name,
                url=urljoin(comfyui_base_url() + "/", "prompt"),
                replacements={
                    "positive_prompt": prompt_text,
                    "negative_prompt": str(payload.get("negative_prompt") or ""),
                    "seed": payload.get("seed"),
                    "width": payload.get("width"),
                    "height": payload.get("height"),
                    "input_image": uploaded_image_name,
                },
                request=request,
            )
            status, result = comfyui_request("POST", "/prompt", request)
            if status >= 400 or not isinstance(result, dict) or not result.get("prompt_id"):
                self.send_json(result, status if status >= 400 else 502)
                return
            task_id = uuid.uuid4().hex
            metadata = {
                "source": "comfyui",
                "workflow": workflow_name,
                "prompt_id": str(result["prompt_id"]),
                "comfyui_base_url": comfyui_base_url(),
                "request": {"prompt": prompt_text, "negative_prompt": str(payload.get("negative_prompt") or ""), "seed": payload.get("seed"), "width": payload.get("width"), "height": payload.get("height"), "input_image": uploaded_image_name},
            }
            model_name = "ComfyUI · 图片编辑" if workflow_name == "image_edit" else "ComfyUI · 文生图"
            save_media("image", status="pending", prompt=prompt_text, model=model_name, metadata=metadata, media_id=task_id)
            self.send_json({"task_id": task_id, "prompt_id": str(result["prompt_id"]), "workflow": workflow_name, "status": "pending"})
            return
        if path == "/v1/media/uploads" or path == "/api/upload":
            self.handle_upload()
            return
        if path in {"/api/history/delete", "/v1/media/history/delete"}:
            try:
                payload = self.read_json()
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_error_json(400, str(exc))
                return
            ids = payload.get("ids")
            if not isinstance(ids, list):
                self.send_error_json(400, "ids must be an array")
                return
            count = delete_media_many(ids)
            self.send_json({"deleted": count})
            return
        image_aliases = {
            "/api/images/generations": "/v1/images/generations",
            "/api/images/edits": "/v1/images/edits",
        }
        upstream_path = image_aliases.get(path, path)
        if upstream_path in {"/v1/images/generations", "/v1/images/edits", "/v1/videos/generations"}:
            try:
                payload = self.read_json()
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_error_json(400, str(exc))
                return
            if not str(payload.get("model", "")).strip():
                self.send_error_json(400, "model is required")
                return
            if not str(payload.get("prompt", "")).strip():
                self.send_error_json(400, "prompt is required")
                return
            if upstream_path == "/v1/images/edits":
                image = payload.get("image")
                if not isinstance(image, dict) or not extract_url(image.get("url")):
                    self.send_error_json(400, "image.url is required and must be a public URL")
                    return
            status, result = upstream_request("POST", upstream_path, payload, authorization=self.headers.get("Authorization"))
            kind = "video" if upstream_path.startswith("/v1/videos") else "image"
            if kind == "video":
                if status < 400:
                    request_id = str(result.get("request_id") or result.get("id") or uuid.uuid4().hex)
                    save_media("video", status="pending", prompt=str(payload.get("prompt", "")), model=str(payload.get("model", "")), metadata={**result, "request_id": request_id})
                    if "request_id" not in result:
                        result["request_id"] = request_id
            elif status < 400:
                persist_generation(result, kind, payload)
            self.send_json(result, status)
            return
        self.send_error_json(404, "not found", "not_found")

    def handle_upload(self):
        key = str(CONFIG.get("imgbb_api_key", "")).strip()
        if not key:
            self.send_error_json(503, "imgbb_api_key is not configured in config.json", "configuration_error")
            return
        try:
            image, filename = self.parse_upload()
        except ValueError as exc:
            self.send_error_json(400, str(exc))
            return
        trace("imgbb.request", method="POST", url="https://api.imgbb.com/1/upload", params={"filename": filename, "bytes": len(image)})
        # ImgBB accepts base64 in an ordinary form request, which avoids any
        # dependency on a multipart client.
        form = urlencode({"key": key, "image": base64.b64encode(image).decode("ascii"), "name": filename or "upload"}).encode()
        try:
            with urlopen(Request("https://api.imgbb.com/1/upload", data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"), timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
                trace("imgbb.response", method="POST", url="https://api.imgbb.com/1/upload", status=response.status, result=result)
        except Exception as exc:
            trace("imgbb.response", method="POST", url="https://api.imgbb.com/1/upload", status=502, result={"error": str(exc)})
            self.send_error_json(502, f"ImgBB upload failed: {exc}", "upstream_error")
            return
        if not result.get("success") or not isinstance(result.get("data"), dict):
            self.send_json(result, 502)
            return
        data = result["data"]
        url = data.get("url") or data.get("display_url")
        item = save_media("upload", url=url, title=filename or data.get("title", ""), metadata=data)
        self.send_json({"success": True, "url": url, "data": data, "record": item})

    def parse_upload(self) -> tuple[bytes, str]:
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 32 * 1024 * 1024:
            raise ValueError("image is required and must be 32 MB or smaller")
        raw = self.rfile.read(length)
        if content_type.startswith("application/json"):
            body = json.loads(raw.decode("utf-8"))
            source = body.get("image", "")
            if str(source).startswith(("http://", "https://")):
                with urlopen(str(source), timeout=60) as response:
                    return response.read(), str(body.get("name", "upload"))
            return base64.b64decode(str(source).split(",", 1)[-1]), str(body.get("name", "upload"))
        match = re.search(r"boundary=([^;]+)", content_type)
        if not match:
            return raw, "upload"
        boundary = ("--" + match.group(1).strip('"')).encode()
        for part in raw.split(boundary):
            if b"Content-Disposition" not in part or b"name=\"image\"" not in part:
                continue
            head, _, content = part.partition(b"\r\n\r\n")
            filename_match = re.search(rb"filename=\"([^\"]*)", head)
            content = content.rstrip(b"\r\n-")
            if content:
                return content, (filename_match.group(1).decode("utf-8", "ignore") if filename_match else "upload")
        raise ValueError("multipart field image is required")

    def parse_image_payload(self, image_payload: dict) -> tuple[bytes, str]:
        source = str(image_payload.get("data") or image_payload.get("b64_json") or "").strip()
        filename = str(image_payload.get("name") or "comfyui-edit.png").strip() or "comfyui-edit.png"
        if source:
            try:
                return base64.b64decode(source.split(",", 1)[-1]), filename
            except Exception as exc:
                raise ValueError(f"image data is invalid: {exc}") from exc
        url = str(image_payload.get("url") or "").strip()
        if not url:
            raise ValueError("image.data or image.url is required")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("image.url must be http or https")
        try:
            with urlopen(Request(url, headers={"Accept": "image/*,*/*"}, method="GET"), timeout=float(CONFIG.get("request_timeout", 120))) as response:
                content = response.read(32 * 1024 * 1024 + 1)
                if len(content) > 32 * 1024 * 1024:
                    raise ValueError("image is larger than 32 MB")
                return content, Path(parsed.path).name or filename
        except ValueError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ValueError(f"image.url fetch failed: {exc}") from exc


def main() -> None:
    init_db()
    host = str(CONFIG.get("host", "127.0.0.1"))
    port = int(CONFIG.get("port", 6000))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Grok media UI: http://{host}:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()

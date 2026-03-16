import base64
import http.server
import json
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True, slots=True)
class _Config:
    host: str = "127.0.0.1"
    port: int = 1236
    vlm_url: str = "http://127.0.0.1:1235/v1/chat/completions"
    annotate_timeout: float = 19.0
    vlm_timeout: float = 360.0
    sse_keepalive_interval: float = 70.0
    max_sse_queue_size: int = 256
    log_file: str = "panel_log.jsonl"
    sentinel: str = "NONE"
    b64_prefix: str = ";base64,"
    image_dir: str = "panel_images"
    max_replay_delay: float = 5.0
    default_replay_speed: float = 1.0


SYNC_RECIPIENTS: frozenset[str] = frozenset({"win32_capture", "annotate", "vlm", "win32_device"})

CFG: _Config = _Config()
WIN32_PATH: Path = Path(__file__).resolve().parent / "win32.py"
PANEL_HTML: Path = Path(__file__).resolve().parent / "panel.html"
HERE: Path = Path(__file__).resolve().parent


def _ensure_image_dir() -> Path:
    img_dir: Path = HERE / CFG.image_dir
    img_dir.mkdir(exist_ok=True)
    return img_dir


def _save_sidecar(request_id: str, tag: str, b64_data: str) -> str:
    if b64_data == CFG.sentinel or len(b64_data) < 20:
        return CFG.sentinel
    img_dir: Path = _ensure_image_dir()
    sidecar_id: str = f"{request_id}_{tag}"
    sidecar_path: Path = img_dir / f"{sidecar_id}.b64"
    sidecar_path.write_text(b64_data, encoding="ascii")
    return sidecar_id


def _load_sidecar(sidecar_id: str) -> str:
    if sidecar_id == CFG.sentinel or not sidecar_id:
        return CFG.sentinel
    path: Path = (HERE / CFG.image_dir) / f"{sidecar_id}.b64"
    if path.exists():
        return path.read_text(encoding="ascii")
    return CFG.sentinel


_log_file_lock: threading.Lock = threading.Lock()
_log_file_path: Path = HERE / CFG.log_file

_pending: dict[str, dict[str, Any]] = {}
_pending_lock: threading.Lock = threading.Lock()

_agent_sse_lock: threading.Lock = threading.Lock()
_agent_sse_queues: dict[str, list[queue.Queue[bytes | None]]] = {}

_startup_region: str = CFG.sentinel
_startup_scale: float = 1.0

_brain_procs: dict[str, subprocess.Popen[bytes]] = {}
_brain_procs_lock: threading.Lock = threading.Lock()

_agent_ui_state: dict[str, dict[str, str]] = {}
_agent_ui_lock: threading.Lock = threading.Lock()

_replay_mode: bool = False
_replay_events: list[dict[str, Any]] = []
_replay_lock: threading.Lock = threading.Lock()
_replay_index: int = 0
_replay_speed: float = CFG.default_replay_speed
_replay_stop: threading.Event = threading.Event()
_replay_pause: threading.Event = threading.Event()
_replay_thread: threading.Thread | None = None

_DEFAULT_AGENT_STATE: dict[str, str] = {
    "raw_image_b64": CFG.sentinel,
    "vlm_image_b64": CFG.sentinel,
    "system_prompt": CFG.sentinel,
    "user_message": CFG.sentinel,
    "vlm_reply": CFG.sentinel,
    "finish_reason": CFG.sentinel,
    "status": CFG.sentinel,
}


def _make_default_state() -> dict[str, str]:
    return dict(_DEFAULT_AGENT_STATE)


def _log(
    event: str,
    from_comp: str = "",
    to_comp: str = "",
    agent: str = "",
    request_id: str = "",
    label: str = "",
    size: int = 0,
    error: bool = False,
    finish_reason: str = "",
    **fields: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ts": time.time(),
        "event": event,
        "from": from_comp,
        "to": to_comp,
        "agent": agent,
        "request_id": request_id,
        "label": label,
        "size": size,
        "error": error,
        "finish_reason": finish_reason,
        "fields": _sanitize_fields(fields),
    }
    with _log_file_lock:
        with _log_file_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
    _push_diagram_flow(entry)
    return entry


def _sanitize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for k, v in fields.items():
        if isinstance(v, str) and len(v) > 500:
            prefix_idx: int = v.find(CFG.b64_prefix)
            if prefix_idx != -1:
                sanitized[k] = "<IMG_SIDECAR>"
                continue
        sanitized[k] = v
    return sanitized


def _push_to_queues(
    queues_copy: list[queue.Queue[bytes | None]],
    event: str,
    data: dict[str, Any],
) -> None:
    chunk: bytes = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
    for q in queues_copy:
        try:
            q.put_nowait(chunk)
        except queue.Full:
            pass


def _agent_sse_push(agent: str, event: str, data: dict[str, Any]) -> None:
    with _agent_sse_lock:
        queues: list[queue.Queue[bytes | None]] = list(_agent_sse_queues.get(agent, []))
    if queues:
        _push_to_queues(queues, event, data)


def _push_diagram_flow(entry: dict[str, Any]) -> None:
    diagram_data: dict[str, Any] = {
        "ts": entry["ts"],
        "event": entry["event"],
        "from": entry["from"],
        "to": entry["to"],
        "agent": entry["agent"],
        "request_id": entry["request_id"],
        "label": entry["label"],
        "size": entry["size"],
        "error": entry["error"],
        "finish_reason": entry["finish_reason"],
    }
    _agent_sse_push("ui", "diagram_flow", diagram_data)


def _update_agent_state(agent: str, rid: str = "", **updates: str) -> None:
    with _agent_ui_lock:
        if agent not in _agent_ui_state:
            _agent_ui_state[agent] = _make_default_state()
        _agent_ui_state[agent].update(updates)
        state: dict[str, str] = dict(_agent_ui_state[agent])
    state["agent"] = agent
    _agent_sse_push("ui", "agent_state", state)
    sidecar_refs: dict[str, str] = {}
    for field in ("raw_image_b64", "vlm_image_b64"):
        val: str = updates.get(field, CFG.sentinel)
        if val != CFG.sentinel and len(val) > 100:
            sid: str = _save_sidecar(rid or str(uuid.uuid4()), field, val)
            if sid != CFG.sentinel:
                sidecar_refs[field] = sid
    _log(
        "agent_state_push",
        from_comp="panel",
        to_comp="browser",
        agent=agent,
        request_id=rid,
        label=f"state {agent}",
        fields_updated=list(updates.keys()),
        sidecar=sidecar_refs if sidecar_refs else CFG.sentinel,
    )


def _extract_vlm_fields(vlm_request: dict[str, Any]) -> tuple[str, str, str]:
    messages: list[dict[str, Any]] = vlm_request.get("messages", [])
    system_prompt: str = CFG.sentinel
    user_message: str = CFG.sentinel
    vlm_image_b64: str = CFG.sentinel
    for msg in messages:
        role: str = msg.get("role", "")
        content: Any = msg.get("content", "")
        match role:
            case "system":
                if isinstance(content, str):
                    system_prompt = content
            case "user":
                if isinstance(content, str):
                    user_message = content
                elif isinstance(content, list):
                    text_parts: list[str] = []
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        match part.get("type", ""):
                            case "text":
                                text_parts.append(part.get("text", ""))
                            case "image_url":
                                url: str = part.get("image_url", {}).get("url", "")
                                prefix_idx: int = url.find(CFG.b64_prefix)
                                if prefix_idx != -1:
                                    vlm_image_b64 = url[prefix_idx + len(CFG.b64_prefix):]
                    user_message = "\n".join(text_parts) if text_parts else CFG.sentinel
    return system_prompt, user_message, vlm_image_b64


def _extract_vlm_reply(resp_obj: dict[str, Any]) -> str:
    choices: list[Any] = resp_obj.get("choices", [])
    if not choices:
        return CFG.sentinel
    return choices[0].get("message", {}).get("content", CFG.sentinel)


def _extract_finish_reason(resp_obj: dict[str, Any]) -> str:
    choices: list[Any] = resp_obj.get("choices", [])
    if not choices:
        return "none"
    return choices[0].get("finish_reason", "unknown")


def _ensure_brain_running(name: str) -> None:
    with _brain_procs_lock:
        if name in _brain_procs:
            proc: subprocess.Popen[bytes] = _brain_procs[name]
            if proc.poll() is None:
                return
        brain_file: Path = HERE / f"{name}.py"
        if not brain_file.exists():
            return
        if name in ("panel", "win32", "brain_util"):
            _log("brain_launch_blocked", from_comp="panel", to_comp="panel", agent=name, label=f"blocked {name}")
            return
        proc = subprocess.Popen(
            [sys.executable, str(brain_file), "--region", _startup_region, "--scale", str(_startup_scale)],
        )
        _brain_procs[name] = proc
        _log("brain_launched", from_comp="panel", to_comp="brain", label=f"launch {brain_file.name}", pid=proc.pid)


def _win32(args: list[str], request_id: str, agent: str) -> subprocess.CompletedProcess[bytes]:
    cmd: list[str] = [sys.executable, str(WIN32_PATH)] + args
    proc: subprocess.CompletedProcess[bytes] = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        _log(
            "win32_action_failed",
            from_comp="win32", to_comp="panel",
            agent=agent, request_id=request_id,
            label=f"failed {args[0] if args else '?'}",
            error=True,
            args=args,
            returncode=proc.returncode,
            stderr=proc.stderr.decode(errors="replace"),
        )
    return proc


def _handle_win32_capture(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    region: str = body.get("region", CFG.sentinel)
    capture_scale: float = body.get("capture_scale", 0.0)
    capture_size: list[int] = body.get("capture_size", [0, 0])
    cmd: list[str] = [sys.executable, str(WIN32_PATH), "capture", "--region", region]
    if capture_scale > 0.0:
        cmd.extend(["--scale", str(capture_scale)])
    elif capture_size[0] > 0 and capture_size[1] > 0:
        cmd.extend(["--width", str(capture_size[0]), "--height", str(capture_size[1])])
    else:
        return {"error": "capture requires either capture_scale or capture_size"}
    proc: subprocess.CompletedProcess[bytes] = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        _log(
            "capture_failed",
            from_comp="win32", to_comp="panel",
            agent=agent, request_id=rid,
            label=f"capture failed {agent}",
            error=True,
            returncode=proc.returncode,
            stderr=proc.stderr.decode(errors="replace"),
        )
        return {"error": f"capture failed: rc={proc.returncode}"}
    if not proc.stdout:
        _log(
            "capture_empty",
            from_comp="win32", to_comp="panel",
            agent=agent, request_id=rid,
            label=f"capture empty {agent}",
            error=True,
        )
        return {"error": "capture returned empty"}
    image_b64: str = base64.b64encode(proc.stdout).decode("ascii")
    _log(
        "capture_done",
        from_comp="win32", to_comp="panel",
        agent=agent, request_id=rid,
        label=f"capture {agent}",
        size=len(image_b64),
    )
    _update_agent_state(agent, rid=rid, raw_image_b64=image_b64)
    return {"image_b64": image_b64}


def _handle_annotate(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    image_b64: str = body.get("image_b64", CFG.sentinel)
    overlays: list[dict[str, Any]] = body.get("overlays", [])
    slot_ref: dict[str, Any] = {"event": threading.Event(), "result": CFG.sentinel}
    with _pending_lock:
        _pending[rid] = slot_ref
    data: dict[str, Any] = {
        "request_id": rid,
        "agent": agent,
        "image_b64": image_b64,
        "overlays": overlays,
    }
    _save_sidecar(rid, "annotate_input", image_b64)
    _agent_sse_push("ui", "annotate", data)
    _log(
        "annotate_sent",
        from_comp="panel", to_comp="browser",
        agent=agent, request_id=rid,
        label=f"annotate {agent} ({len(overlays)} ov)",
        size=len(image_b64),
        sidecar=f"{rid}_annotate_input",
    )
    got_result: bool = slot_ref["event"].wait(timeout=CFG.annotate_timeout)
    if not got_result:
        _log(
            "annotate_timeout",
            from_comp="browser", to_comp="panel",
            agent=agent, request_id=rid,
            label=f"TIMEOUT {agent}",
            error=True,
        )
        with _pending_lock:
            _pending.pop(rid, None)
        return {"error": "annotate timeout"}
    result_b64: str = slot_ref["result"]
    _save_sidecar(rid, "annotate_output", result_b64)
    _log(
        "annotate_received",
        from_comp="browser", to_comp="panel",
        agent=agent, request_id=rid,
        label=f"annotated {agent}",
        size=len(result_b64),
        sidecar=f"{rid}_annotate_output",
    )
    return {"image_b64": result_b64}


def _handle_vlm(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    vlm_request: dict[str, Any] = body.get("vlm_request", {})
    system_prompt, user_message, vlm_image_b64 = _extract_vlm_fields(vlm_request)
    vlm_img_sidecar: str = CFG.sentinel
    if vlm_image_b64 != CFG.sentinel:
        vlm_img_sidecar = _save_sidecar(rid, "vlm_image_b64", vlm_image_b64)
    _update_agent_state(
        agent, rid=rid,
        system_prompt=system_prompt,
        user_message=user_message,
        vlm_image_b64=vlm_image_b64 if vlm_image_b64 != CFG.sentinel else CFG.sentinel,
        vlm_reply=CFG.sentinel,
        finish_reason=CFG.sentinel,
    )
    _log(
        "vlm_forward",
        from_comp="panel", to_comp="vlm",
        agent=agent, request_id=rid,
        label=f"vlm {agent}",
        system_prompt=system_prompt,
        user_message=user_message,
        vlm_image_sidecar=vlm_img_sidecar,
    )
    fwd_body: bytes = json.dumps(vlm_request, separators=(",", ":")).encode()
    fwd_req: urllib.request.Request = urllib.request.Request(
        CFG.vlm_url, data=fwd_body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(fwd_req, timeout=CFG.vlm_timeout) as resp:
            resp_bytes: bytes = resp.read()
        resp_obj: dict[str, Any] = json.loads(resp_bytes)
        finish_reason: str = _extract_finish_reason(resp_obj)
        vlm_reply: str = _extract_vlm_reply(resp_obj)
        _log(
            "vlm_response",
            from_comp="vlm", to_comp="panel",
            agent=agent, request_id=rid,
            label=f"reply {agent} ({finish_reason})",
            finish_reason=finish_reason,
            vlm_reply=vlm_reply,
        )
        _update_agent_state(agent, rid=rid, vlm_reply=vlm_reply, finish_reason=finish_reason)
        return resp_obj
    except urllib.error.HTTPError as exc:
        error_body: str = ""
        try:
            error_body = exc.read().decode(errors="replace")
        except Exception:
            pass
        _log(
            "vlm_error",
            from_comp="vlm", to_comp="panel",
            agent=agent, request_id=rid,
            label=f"ERROR {agent} HTTP {exc.code}",
            error=True,
            status=exc.code,
            error_body=error_body,
        )
        _update_agent_state(agent, rid=rid, vlm_reply=f"ERROR HTTP {exc.code}: {error_body}", finish_reason="error")
        return {"error": f"HTTP {exc.code}: {error_body}"}
    except Exception as exc:
        _log(
            "vlm_error",
            from_comp="vlm", to_comp="panel",
            agent=agent, request_id=rid,
            label=f"ERROR {agent}",
            error=True,
            error_text=str(exc),
        )
        _update_agent_state(agent, rid=rid, vlm_reply=f"ERROR: {exc}", finish_reason="error")
        return {"error": str(exc)}


def _handle_win32_device(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    actions: list[dict[str, Any]] = body.get("actions", [])
    region: str = body.get("region", CFG.sentinel)
    results: list[dict[str, Any]] = []
    for act in actions:
        t: str = act.get("type", "")
        _log(
            "action_dispatch",
            from_comp="panel", to_comp="win32",
            agent=agent, request_id=rid,
            label=f"{t} {agent}",
            action_type=t,
        )
        proc: subprocess.CompletedProcess[bytes]
        match t:
            case "drag":
                proc = _win32(["drag",
                        "--from_pos", f"{act['x1']},{act['y1']}",
                        "--to_pos", f"{act['x2']},{act['y2']}",
                        "--region", region], rid, agent)
                results.append({"type": t, "ok": proc.returncode == 0})
            case "click":
                proc = _win32(["click", "--pos", f"{act['x']},{act['y']}",
                        "--region", region], rid, agent)
                results.append({"type": t, "ok": proc.returncode == 0})
            case "double_click":
                proc = _win32(["double_click", "--pos", f"{act['x']},{act['y']}",
                        "--region", region], rid, agent)
                results.append({"type": t, "ok": proc.returncode == 0})
            case "right_click":
                proc = _win32(["right_click", "--pos", f"{act['x']},{act['y']}",
                        "--region", region], rid, agent)
                results.append({"type": t, "ok": proc.returncode == 0})
            case "type_text":
                proc = _win32(["type_text", "--text", act["text"]], rid, agent)
                results.append({"type": t, "ok": proc.returncode == 0})
            case "press_key":
                proc = _win32(["press_key", "--key", act["key"]], rid, agent)
                results.append({"type": t, "ok": proc.returncode == 0})
            case "hotkey":
                proc = _win32(["hotkey", "--keys", act["keys"]], rid, agent)
                results.append({"type": t, "ok": proc.returncode == 0})
            case "scroll_up":
                proc = _win32(["scroll_up", "--pos", f"{act['x']},{act['y']}",
                        "--region", region, "--clicks", str(act["clicks"])],
                       rid, agent)
                results.append({"type": t, "ok": proc.returncode == 0})
            case "scroll_down":
                proc = _win32(["scroll_down", "--pos", f"{act['x']},{act['y']}",
                        "--region", region, "--clicks", str(act["clicks"])],
                       rid, agent)
                results.append({"type": t, "ok": proc.returncode == 0})
            case "cursor_pos":
                proc = _win32(["cursor_pos", "--region", region], rid, agent)
                stdout_text: str = proc.stdout.decode(errors="replace").strip() if proc.stdout else CFG.sentinel
                results.append({"type": t, "ok": proc.returncode == 0, "pos": stdout_text})
            case _:
                results.append({"type": t, "ok": False, "error": f"unknown action type: {t}"})
    ok_all: bool = all(r.get("ok", False) for r in results)
    _log(
        "device_done",
        from_comp="win32", to_comp="panel",
        agent=agent, request_id=rid,
        label=f"device {agent} {'ok' if ok_all else 'FAIL'}",
        error=not ok_all,
        action_count=len(results),
    )
    _update_agent_state(agent, rid=rid, status=f"device {'ok' if ok_all else 'FAIL'}")
    return {"ok": ok_all, "results": results}


def _handle_async_push(recipient: str, body: dict[str, Any], rid: str, agent: str) -> None:
    _ensure_brain_running(recipient)
    data: dict[str, Any] = dict(body)
    data["request_id"] = rid
    data["sender"] = agent
    _agent_sse_push(recipient, "message", data)
    target: str = "browser" if recipient == "ui" else "brain"
    _log(
        "routed",
        from_comp=f"brain.{agent}" if agent else "panel",
        to_comp=target,
        agent=agent, request_id=rid,
        label=f"{agent}->{recipient}",
        sender=agent,
        recipient=recipient,
        event_type=body.get("event_type", ""),
        status=body.get("status", ""),
        text=body.get("text", ""),
    )


def _select_region() -> str:
    proc: subprocess.CompletedProcess[bytes] = subprocess.run(
        [sys.executable, str(WIN32_PATH), "select_region"], capture_output=True,
    )
    if proc.returncode != 0:
        _log(
            "select_region_failed",
            from_comp="win32", to_comp="panel",
            label="select_region failed",
            error=True,
            returncode=proc.returncode,
            stderr=proc.stderr.decode(errors="replace"),
        )
        return CFG.sentinel
    return proc.stdout.decode().strip()


def _tandem_select() -> tuple[str, float]:
    print("Select capture region...")
    _log("select_region_prompt", from_comp="panel", to_comp="panel", label="select region prompt")
    region: str = _select_region()
    if region == CFG.sentinel:
        _log("select_region_empty", from_comp="panel", to_comp="panel", label="no region selected")
        return CFG.sentinel, 1.0
    print(f"Region: {region}")
    _log("select_region_done", from_comp="panel", to_comp="panel", label=f"region {region}", region=region)
    print("Select horizontal scale reference...")
    _log("select_scale_prompt", from_comp="panel", to_comp="panel", label="select scale prompt")
    scale_region: str = _select_region()
    if scale_region == CFG.sentinel:
        _log("select_scale_empty", from_comp="panel", to_comp="panel", label="no scale selected")
        return region, 1.0
    parts: list[str] = scale_region.split(",")
    if len(parts) != 4:
        _log("select_scale_invalid", from_comp="panel", to_comp="panel", label="invalid scale region", error=True, raw=scale_region)
        return region, 1.0
    x1: int = int(parts[0])
    x2: int = int(parts[2])
    scale: float = abs(x2 - x1) / 1000.0
    print(f"Scale: {scale:.4f}")
    _log("select_scale_done", from_comp="panel", to_comp="panel", label=f"scale {scale:.4f}", scale=scale)
    return region, scale


def _export_html_base64() -> None:
    for html_path in HERE.glob("*.html"):
        txt_path: Path = html_path.with_name(html_path.stem + "_base64.txt")
        txt_path.write_text(base64.b64encode(html_path.read_bytes()).decode("ascii"), encoding="ascii")


def _load_replay_log(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
    return events


def _replay_rebuild_state(agent: str, fields: dict[str, Any], sidecar_updates: dict[str, str]) -> None:
    updates: dict[str, str] = dict(sidecar_updates)
    with _agent_ui_lock:
        if agent not in _agent_ui_state:
            _agent_ui_state[agent] = _make_default_state()
        if updates:
            _agent_ui_state[agent].update(updates)
        state: dict[str, str] = dict(_agent_ui_state[agent])
    state["agent"] = agent
    _agent_sse_push("ui", "agent_state", state)


def _replay_process_event(entry: dict[str, Any]) -> None:
    event: str = entry.get("event", "")
    agent: str = entry.get("agent", "")
    fields: dict[str, Any] = entry.get("fields", {})

    _push_diagram_flow(entry)

    match event:
        case "agent_state_push":
            sidecar_raw: Any = fields.get("sidecar", CFG.sentinel)
            updates: dict[str, str] = {}
            if isinstance(sidecar_raw, dict):
                for field_name, sid in sidecar_raw.items():
                    b64: str = _load_sidecar(str(sid))
                    if b64 != CFG.sentinel:
                        updates[field_name] = b64
            _replay_rebuild_state(agent, fields, updates)

        case "vlm_forward":
            system_prompt: str = fields.get("system_prompt", CFG.sentinel)
            user_message: str = fields.get("user_message", CFG.sentinel)
            vlm_sidecar: str = fields.get("vlm_image_sidecar", CFG.sentinel)
            vlm_img: str = _load_sidecar(vlm_sidecar) if vlm_sidecar != CFG.sentinel else CFG.sentinel
            updates_vlm: dict[str, str] = {
                "system_prompt": system_prompt if isinstance(system_prompt, str) else CFG.sentinel,
                "user_message": user_message if isinstance(user_message, str) else CFG.sentinel,
                "vlm_reply": CFG.sentinel,
                "finish_reason": CFG.sentinel,
            }
            if vlm_img != CFG.sentinel:
                updates_vlm["vlm_image_b64"] = vlm_img
            _replay_rebuild_state(agent, fields, updates_vlm)

        case "vlm_response":
            vlm_reply: str = fields.get("vlm_reply", CFG.sentinel)
            fr: str = entry.get("finish_reason", "unknown")
            updates_resp: dict[str, str] = {}
            if isinstance(vlm_reply, str) and vlm_reply != CFG.sentinel:
                updates_resp["vlm_reply"] = vlm_reply
            updates_resp["finish_reason"] = fr
            _replay_rebuild_state(agent, fields, updates_resp)

        case "routed":
            recipient: str = fields.get("recipient", "")
            if recipient == "ui":
                sender: str = fields.get("sender", "")
                msg_data: dict[str, Any] = {
                    "sender": sender,
                    "request_id": entry.get("request_id", ""),
                }
                event_type_val: str = fields.get("event_type", "")
                if event_type_val:
                    msg_data["event_type"] = event_type_val
                status_val: str = fields.get("status", "")
                if status_val:
                    msg_data["status"] = status_val
                text_val: str = fields.get("text", "")
                if text_val:
                    msg_data["text"] = text_val
                _agent_sse_push("ui", "message", msg_data)
                if sender and event_type_val == "status" and status_val:
                    _replay_rebuild_state(sender, fields, {"status": status_val})
                elif sender and event_type_val == "error" and text_val:
                    _replay_rebuild_state(sender, fields, {"status": f"ERROR: {text_val}"})


def _replay_loop() -> None:
    global _replay_index
    while not _replay_stop.is_set():
        if _replay_pause.is_set():
            _replay_stop.wait(0.05)
            continue
        with _replay_lock:
            idx: int = _replay_index
            speed: float = _replay_speed
        if idx >= len(_replay_events):
            _agent_sse_push("ui", "replay_status", {"status": "finished", "index": idx, "total": len(_replay_events)})
            _replay_pause.set()
            continue
        entry: dict[str, Any] = _replay_events[idx]
        _replay_process_event(entry)
        with _replay_lock:
            _replay_index = idx + 1
            next_idx: int = _replay_index
        if next_idx < len(_replay_events):
            current_ts: float = entry.get("ts", 0.0)
            next_ts: float = _replay_events[next_idx].get("ts", 0.0)
            delay: float = max(0.0, next_ts - current_ts)
            if speed > 0:
                delay = delay / speed
            delay = min(delay, CFG.max_replay_delay)
            if delay > 0.005:
                _replay_stop.wait(delay)


def _start_replay() -> None:
    global _replay_thread
    if _replay_thread is not None and _replay_thread.is_alive():
        _replay_pause.clear()
        return
    _replay_stop.clear()
    _replay_pause.clear()
    _replay_thread = threading.Thread(target=_replay_loop, daemon=True)
    _replay_thread.start()


def _stop_replay() -> None:
    _replay_stop.set()
    if _replay_thread is not None:
        _replay_thread.join(timeout=2.0)


class PanelHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")

    def _json(self, code: int, data: dict[str, Any]) -> None:
        raw: bytes = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _parse_body(self, body: bytes) -> dict[str, Any] | None:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _log("json_parse_error", from_comp="panel", to_comp="panel", label="bad json", error=True, error_text=str(exc))
            self._json(400, {"error": "bad json"})
            return None

    def _serve_sse(self, q: queue.Queue[bytes | None], on_cleanup: Callable[[], None]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()
        self.wfile.write(b"event: connected\ndata: {}\n\n")
        self.wfile.flush()
        _log("sse_connect", from_comp="browser", to_comp="panel", label="SSE connect")
        try:
            while True:
                try:
                    chunk: bytes | None = q.get(timeout=CFG.sse_keepalive_interval)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if chunk is None:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            on_cleanup()
            _log("sse_disconnect", from_comp="browser", to_comp="panel", label="SSE disconnect")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path: str = self.path.split("?")[0]
        match path:
            case "/":
                raw: bytes = PANEL_HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self._cors()
                self.end_headers()
                self.wfile.write(raw)
            case "/ready":
                with _agent_sse_lock:
                    ui_connected: bool = len(_agent_sse_queues.get("ui", [])) > 0
                self._json(200, {
                    "ok": True,
                    "region": _startup_region,
                    "scale": _startup_scale,
                    "ui_connected": ui_connected,
                    "replay": _replay_mode,
                })
            case "/agent-events":
                params: dict[str, list[str]] = parse_qs(urlparse(self.path).query)
                agent_name: str = params.get("agent", [CFG.sentinel])[0]
                if agent_name == CFG.sentinel:
                    self._json(400, {"error": "agent parameter required"})
                    return
                q: queue.Queue[bytes | None] = queue.Queue(maxsize=CFG.max_sse_queue_size)
                with _agent_sse_lock:
                    if agent_name not in _agent_sse_queues:
                        _agent_sse_queues[agent_name] = []
                    _agent_sse_queues[agent_name].append(q)

                def cleanup() -> None:
                    with _agent_sse_lock:
                        agent_list: list[queue.Queue[bytes | None]] = _agent_sse_queues.get(agent_name, [])
                        try:
                            agent_list.remove(q)
                        except ValueError:
                            pass

                self._serve_sse(q, cleanup)
            case "/status":
                with _replay_lock:
                    self._json(200, {
                        "replay": _replay_mode,
                        "total": len(_replay_events),
                        "index": _replay_index,
                        "speed": _replay_speed,
                        "paused": _replay_pause.is_set(),
                    })
            case _:
                self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        global _replay_speed, _replay_index
        path: str = self.path.split("?")[0]
        length: int = int(self.headers.get("Content-Length", 0))
        body: bytes = self.rfile.read(length) if length else b""

        match path:
            case "/route":
                if _replay_mode:
                    self._json(400, {"error": "replay mode active, routing disabled"})
                    return
                req: dict[str, Any] | None = self._parse_body(body)
                if req is None:
                    return

                agent: str | None = req.get("agent")
                recipients: list[str] | None = req.get("recipients")
                if agent is None or recipients is None or not isinstance(recipients, list):
                    self._json(400, {"error": "agent and recipients (list) required"})
                    return

                rid: str = str(uuid.uuid4())
                _log(
                    "route",
                    from_comp=f"brain.{agent}",
                    to_comp="panel",
                    agent=agent, request_id=rid,
                    label=f"{agent}:{recipients}",
                    recipients=recipients,
                )

                sync_targets: list[str] = [r for r in recipients if r in SYNC_RECIPIENTS]
                async_targets: list[str] = [r for r in recipients if r not in SYNC_RECIPIENTS]

                if len(sync_targets) > 1:
                    self._json(400, {"error": "at most one sync recipient allowed"})
                    return

                for target in async_targets:
                    _handle_async_push(target, req, rid, agent)

                if not sync_targets:
                    self._json(200, {"request_id": rid, "ok": True})
                    return

                sync_target: str = sync_targets[0]
                result: dict[str, Any]
                match sync_target:
                    case "win32_capture":
                        result = _handle_win32_capture(req, rid, agent)
                    case "annotate":
                        result = _handle_annotate(req, rid, agent)
                    case "vlm":
                        result = _handle_vlm(req, rid, agent)
                    case "win32_device":
                        result = _handle_win32_device(req, rid, agent)
                    case _:
                        result = {"error": f"unhandled sync recipient: {sync_target}"}

                result["request_id"] = rid
                status: int = 200 if "error" not in result else 502
                self._json(status, result)

            case "/result":
                data: dict[str, Any] | None = self._parse_body(body)
                if data is None:
                    return
                rid_val: str = data.get("request_id", CFG.sentinel)
                annotated: str = data.get("image_b64", CFG.sentinel)
                with _pending_lock:
                    slot: dict[str, Any] | None = _pending.pop(rid_val, None)
                if slot:
                    slot["result"] = annotated
                    slot["event"].set()
                    _log(
                        "result_received",
                        from_comp="browser", to_comp="panel",
                        request_id=rid_val,
                        label="annotation result",
                        size=len(annotated),
                    )
                self._json(200, {"ok": True})

            case "/panel-log":
                self._json(200, {"ok": True})

            case "/control":
                if not _replay_mode:
                    self._json(400, {"error": "not in replay mode"})
                    return
                try:
                    data = json.loads(body) if body else {}
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._json(400, {"error": "bad json"})
                    return
                action: str = data.get("action", "")
                match action:
                    case "play":
                        _start_replay()
                        self._json(200, {"ok": True, "action": "play"})
                    case "pause":
                        _replay_pause.set()
                        self._json(200, {"ok": True, "action": "pause"})
                    case "stop":
                        _stop_replay()
                        with _replay_lock:
                            _replay_index = 0
                        with _agent_ui_lock:
                            _agent_ui_state.clear()
                        self._json(200, {"ok": True, "action": "stop"})
                    case "seek":
                        seek_idx: int = max(0, min(len(_replay_events) - 1, int(data.get("index", 0))))
                        _stop_replay()
                        with _replay_lock:
                            _replay_index = 0
                        with _agent_ui_lock:
                            _agent_ui_state.clear()
                        for i in range(seek_idx):
                            _replay_process_event(_replay_events[i])
                        with _replay_lock:
                            _replay_index = seek_idx
                        self._json(200, {"ok": True, "index": seek_idx})
                    case "speed":
                        spd: float = max(0.1, min(100.0, float(data.get("speed", 1.0))))
                        with _replay_lock:
                            _replay_speed = spd
                        self._json(200, {"ok": True, "speed": spd})
                    case "step":
                        with _replay_lock:
                            idx: int = _replay_index
                        if idx < len(_replay_events):
                            _replay_process_event(_replay_events[idx])
                            with _replay_lock:
                                _replay_index = idx + 1
                        self._json(200, {"ok": True, "index": _replay_index})
                    case _:
                        self._json(400, {"error": f"unknown action: {action}"})

            case _:
                self._json(404, {"error": "not found"})


def start(host: str = CFG.host, port: int = CFG.port) -> http.server.ThreadingHTTPServer:
    server: http.server.ThreadingHTTPServer = http.server.ThreadingHTTPServer((host, port), PanelHandler)
    _log("server_start", from_comp="panel", to_comp="panel", label=f"start {host}:{port}", host=host, port=port)
    return server


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python panel.py <brain_file.py>")
        print("       python panel.py --replay <panel_log.jsonl>")
        raise SystemExit(1)

    if sys.argv[1] == "--replay":
        if len(sys.argv) < 3:
            print("Usage: python panel.py --replay <panel_log.jsonl>")
            raise SystemExit(1)
        replay_path: Path = Path(sys.argv[2])
        if not replay_path.exists():
            print(f"ERROR: {replay_path} not found")
            raise SystemExit(1)
        _replay_mode = True
        _replay_events = _load_replay_log(replay_path)
        print(f"Loaded {len(_replay_events)} events from {replay_path}")
        if (HERE / CFG.image_dir).exists():
            sidecar_count: int = len(list((HERE / CFG.image_dir).glob("*.b64")))
            print(f"Found {sidecar_count} sidecar images")
        _ensure_image_dir()
        _export_html_base64()
        srv: http.server.ThreadingHTTPServer = start()
        print(f"Panel replay on http://{CFG.host}:{CFG.port}")
        print("Controls: POST /control {action: play|pause|step|stop|seek|speed}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            _stop_replay()
    else:
        brain_arg: str = sys.argv[1]
        brain_path: Path = HERE / brain_arg
        if not brain_path.exists():
            print(f"ERROR: {brain_arg} not found")
            raise SystemExit(1)
        _startup_region, _startup_scale = _tandem_select()
        if _startup_region == CFG.sentinel:
            print("No region selected, exiting.")
            raise SystemExit(1)
        _log("startup", from_comp="panel", to_comp="panel", label="startup", region=_startup_region, scale=_startup_scale)
        print(f"Region: {_startup_region}  Scale: {_startup_scale:.4f}")
        _ensure_image_dir()
        _export_html_base64()
        srv = start()
        print(f"Panel running on http://{CFG.host}:{CFG.port}")
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            [sys.executable, str(brain_path), "--region", _startup_region, "--scale", str(_startup_scale)],
        )
        with _brain_procs_lock:
            _brain_procs[brain_path.stem] = proc
        _log("brain_launched", from_comp="panel", to_comp="brain", label=f"launch {brain_arg}", pid=proc.pid)
        print(f"Launched {brain_arg} pid={proc.pid}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            with _brain_procs_lock:
                for p in _brain_procs.values():
                    p.terminate()

import json
import math
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, fields
from typing import Any
from collections.abc import Callable


SENTINEL: str = "NONE"
NORM: int = 1000
PANEL_URL: str = "http://127.0.0.1:1236/route"
SSE_BASE_URL: str = "http://127.0.0.1:1236/agent-events"


@dataclass(frozen=True, slots=True)
class VLMConfig:
    model: str = "qwen3.5-0.8b"
    temperature: float = 0.7
    max_tokens: int = 300
    top_p: float = 0.80
    top_k: int = 20
    min_p: float = 0.0
    stream: bool = False
    presence_penalty: float = 1.5
    frequency_penalty: float = 0.0
    repetition_penalty: float = 1.0
    stop: tuple[str, ...] | None = None
    seed: int | None = None
    logit_bias: dict[str, float] | None = None


VLM: VLMConfig = VLMConfig()


@dataclass(frozen=True, slots=True)
class SSEConfig:
    reconnect_delay: float = 1.0
    max_reconnect_delay: float = 30.0
    timeout: float = 6000.0


@dataclass(frozen=True, slots=True)
class BrainArgs:
    region: str = SENTINEL
    scale: float = 1.0


def _vlm_params(cfg: VLMConfig, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for f in fields(cfg):
        v: Any = getattr(cfg, f.name)
        if v is not None:
            params[f.name] = v
    params.update(overrides)
    return params


def parse_brain_args(argv: list[str]) -> BrainArgs:
    region: str = SENTINEL
    scale: float = 1.0
    for idx, arg in enumerate(argv):
        if arg == "--region" and idx + 1 < len(argv):
            region = argv[idx + 1]
        elif arg == "--scale" and idx + 1 < len(argv):
            scale = float(argv[idx + 1])
    return BrainArgs(region=region, scale=scale)


def sse_listen(
    url: str,
    callback: Callable[[str, dict[str, Any]], None],
    sse_cfg: SSEConfig = SSEConfig(),
) -> None:
    def _loop() -> None:
        delay: float = sse_cfg.reconnect_delay
        while True:
            try:
                with urllib.request.urlopen(url, timeout=sse_cfg.timeout) as resp:
                    delay = sse_cfg.reconnect_delay
                    current_event: str = ""
                    for raw_line in resp:
                        line: str = raw_line.decode().rstrip("\r\n")
                        if line.startswith("event: "):
                            current_event = line[7:]
                        elif line.startswith("data: "):
                            if current_event:
                                try:
                                    data: dict[str, Any] = json.loads(line[6:])
                                except (json.JSONDecodeError, UnicodeDecodeError):
                                    current_event = ""
                                    continue
                                try:
                                    callback(current_event, data)
                                except Exception as exc:
                                    _log_local("sse_callback_error", error=str(exc))
                            current_event = ""
            except Exception as exc:
                _log_local("sse_reconnect", error=str(exc), delay=delay)
                time.sleep(delay)
                delay = min(delay * 2, sse_cfg.max_reconnect_delay)

    threading.Thread(target=_loop, daemon=True).start()


def _log_local(event: str, **extra: Any) -> None:
    try:
        body: dict[str, Any] = {"level": "debug", "msg": event}
        body.update(extra)
        req: urllib.request.Request = urllib.request.Request(
            "http://127.0.0.1:1236/panel-log",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5.0)
    except Exception:
        pass


def route(
    agent: str,
    recipients: list[str],
    timeout: float = 120.0,
    **payload: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"agent": agent, "recipients": recipients}
    body.update(payload)
    req: urllib.request.Request = urllib.request.Request(
        PANEL_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def capture(
    agent: str, region: str,
    width: int = 0, height: int = 0,
    scale: float = 0.0, timeout: float = 30.0,
) -> str:
    payload: dict[str, Any] = {"region": region}
    if scale > 0.0:
        payload["capture_scale"] = scale
    else:
        payload["capture_size"] = [width, height]
    resp: dict[str, Any] = route(
        agent, ["win32_capture"],
        timeout=timeout, **payload,
    )
    return resp.get("image_b64", SENTINEL)


def annotate(
    agent: str,
    image_b64: str, overlays: list[dict[str, Any]],
    timeout: float = 25.0,
) -> str:
    resp: dict[str, Any] = route(
        agent, ["annotate"],
        timeout=timeout, image_b64=image_b64, overlays=overlays,
    )
    return resp.get("image_b64", SENTINEL)


def vlm(
    agent: str,
    vlm_request: dict[str, Any], timeout: float = 360.0,
) -> dict[str, Any]:
    return route(
        agent, ["vlm"],
        timeout=timeout, vlm_request=vlm_request,
    )


def vlm_text(
    agent: str,
    vlm_request: dict[str, Any], timeout: float = 360.0,
) -> tuple[str, str]:
    resp: dict[str, Any] = vlm(agent, vlm_request, timeout)
    choices: list[Any] = resp.get("choices", [])
    if not choices:
        return SENTINEL, "none"
    message: dict[str, Any] = choices[0].get("message", {})
    content: str = message.get("content", SENTINEL)
    finish_reason: str = choices[0].get("finish_reason", "unknown")
    return content, finish_reason


def device(
    agent: str, region: str,
    actions: list[dict[str, Any]], timeout: float = 30.0,
) -> dict[str, Any]:
    return route(
        agent, ["win32_device"],
        timeout=timeout, region=region, actions=actions,
    )


def make_overlay(
    points: list[list[int]],
    closed: bool = False,
    stroke: str = "",
    stroke_width: int = 1,
    fill: str = "",
    label: str = "",
) -> dict[str, Any]:
    overlay: dict[str, Any] = {
        "type": "overlay",
        "points": points,
        "closed": closed,
    }
    if stroke:
        overlay["stroke"] = stroke
        overlay["stroke_width"] = stroke_width
    if fill:
        overlay["fill"] = fill
    if label:
        overlay["label"] = label
    return overlay


def make_grid_overlays(
    grid_size: int, color: str, stroke_width: int,
) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    step: int = NORM // grid_size
    for i in range(grid_size + 1):
        pos: int = i * step
        overlays.append(make_overlay(
            points=[[pos, 0], [pos, NORM]],
            stroke=color, stroke_width=stroke_width,
        ))
        overlays.append(make_overlay(
            points=[[0, pos], [NORM, pos]],
            stroke=color, stroke_width=stroke_width,
        ))
    return overlays


def make_arrow_overlay(
    from_col: int, from_row: int, to_col: int, to_row: int,
    color: str, grid_size: int, stroke_width: int = 8,
    label: str = "",
) -> list[dict[str, Any]]:
    step: int = NORM // grid_size
    fx: float = from_col * step + step / 2
    fy: float = from_row * step + step / 2
    tx: float = to_col * step + step / 2
    ty: float = to_row * step + step / 2
    dx: float = tx - fx
    dy: float = ty - fy
    length: float = math.hypot(dx, dy)
    if length == 0:
        return []
    ux: float = dx / length
    uy: float = dy / length
    head_len: float = step * 0.55
    head_width: float = step * 0.32
    shaft_tip_x: float = tx - ux * head_len
    shaft_tip_y: float = ty - uy * head_len
    px: float = -uy
    py: float = ux
    w1x: int = round(shaft_tip_x + px * head_width)
    w1y: int = round(shaft_tip_y + py * head_width)
    w2x: int = round(shaft_tip_x - px * head_width)
    w2y: int = round(shaft_tip_y - py * head_width)
    return [
        make_overlay(
            points=[[round(fx), round(fy)], [round(shaft_tip_x), round(shaft_tip_y)]],
            stroke=color, stroke_width=stroke_width,
        ),
        make_overlay(
            points=[[round(tx), round(ty)], [w1x, w1y], [w2x, w2y]],
            closed=True, fill=color, stroke=color, stroke_width=1,
            label=label,
        ),
    ]


def grid_to_norm(col: int, row: int, grid_size: int) -> tuple[int, int]:
    step: int = NORM // grid_size
    x: int = min(NORM, max(0, col * step + step // 2))
    y: int = min(NORM, max(0, row * step + step // 2))
    return x, y


def make_vlm_request(
    system_prompt: str,
    user_content: str | list[dict[str, Any]],
    **overrides: Any,
) -> dict[str, Any]:
    params: dict[str, Any] = _vlm_params(VLM, **overrides)
    params["messages"] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    return params


def make_vlm_request_with_image(
    system_prompt: str,
    image_b64: str,
    user_text: str,
    **overrides: Any,
) -> dict[str, Any]:
    params: dict[str, Any] = _vlm_params(VLM, **overrides)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            {"type": "text", "text": user_text},
        ]},
    ]
    params["messages"] = messages
    return params

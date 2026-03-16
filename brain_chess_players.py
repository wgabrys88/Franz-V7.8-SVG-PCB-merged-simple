import json
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

import brain_util as bu


@dataclass(frozen=True, slots=True)
class ChessConfig:
    region: str = bu.SENTINEL
    scale: float = 1.0
    grid_size: int = 8
    grid_color: str = "rgba(0,255,200,0.95)"
    grid_stroke_width: int = 4
    ready_poll_interval: float = 0.5
    ready_poll_max: int = 60
    post_ready_delay: float = 1.0
    chess_max_tokens: int = 200
    parser_max_tokens: int = 30
    post_move_delay: float = 5.0
    failure_delay: float = 5.0
    ready_url: str = "http://127.0.0.1:1236/ready"


CHESS_SYSTEM: str = """\
You are a chess engine analyzing a board screenshot. White pieces are at the bottom. Grid overlay labels columns a-h left to right, rows 1-8 bottom to top.
Suggest the best move for White.
Output ONLY two squares separated by a space. Example: e2 e4
If no good move exists output NONE.\
"""

PARSER_SYSTEM: str = """\
Convert a chess move into normalized drag coordinates.
The board is an 8x8 grid mapped to 0-1000 coordinate space.
Column centers: a=62 b=187 c=312 d=437 e=562 f=687 g=812 h=937
Row centers: 1=937 2=812 3=687 4=562 5=437 6=312 7=187 8=62
Output ONLY four integers separated by spaces: x1 y1 x2 y2
Example: for e2 e4 output 562 812 562 562
Example: for g1 f3 output 812 937 687 687\
"""


def _parse_coords(text: str) -> tuple[int, int, int, int] | None:
    parts: list[str] = text.strip().split()
    if len(parts) != 4:
        return None
    try:
        vals: list[int] = [int(p) for p in parts]
    except ValueError:
        return None
    for v in vals:
        if v < 0 or v > bu.NORM:
            return None
    return vals[0], vals[1], vals[2], vals[3]


def _wait_for_panel(cfg: ChessConfig) -> None:
    for attempt in range(cfg.ready_poll_max):
        try:
            with urllib.request.urlopen(cfg.ready_url, timeout=2.0) as resp:
                if resp.status == 200:
                    data: dict[str, Any] = json.loads(resp.read())
                    if data.get("ui_connected"):
                        return
        except Exception:
            pass
        time.sleep(cfg.ready_poll_interval)


def _run_round(
    cfg: ChessConfig,
    grid_overlays: list[dict[str, Any]],
    prev_image_b64: str,
) -> str:
    if prev_image_b64 == bu.SENTINEL:
        base_b64: str = bu.capture("chess", cfg.region, scale=cfg.scale)
        if base_b64 == bu.SENTINEL:
            return bu.SENTINEL
    else:
        base_b64 = prev_image_b64

    annotated_b64: str = bu.annotate("chess", base_b64, grid_overlays)
    if annotated_b64 == bu.SENTINEL:
        annotated_b64 = base_b64

    move_text, finish_reason = bu.vlm_text(
        "chess",
        bu.make_vlm_request_with_image(
            CHESS_SYSTEM, annotated_b64,
            "Your move for White.",
            max_tokens=cfg.chess_max_tokens,
        ),
    )

    if finish_reason == "length" or move_text == bu.SENTINEL:
        return bu.SENTINEL

    if move_text.strip().upper() == bu.SENTINEL:
        return bu.SENTINEL

    coords_text, parser_fr = bu.vlm_text(
        "parser",
        bu.make_vlm_request(
            PARSER_SYSTEM,
            move_text,
            max_tokens=cfg.parser_max_tokens,
        ),
    )

    if parser_fr == "length" or coords_text == bu.SENTINEL:
        return bu.SENTINEL

    coords: tuple[int, int, int, int] | None = _parse_coords(coords_text)
    if coords is None:
        return bu.SENTINEL

    x1, y1, x2, y2 = coords
    result: dict[str, Any] = bu.device(
        "chess", cfg.region,
        [{"type": "drag", "x1": x1, "y1": y1, "x2": x2, "y2": y2}],
    )

    if not result.get("ok", False):
        return bu.SENTINEL

    time.sleep(cfg.post_move_delay)

    new_b64: str = bu.capture("chess", cfg.region, scale=cfg.scale)
    if new_b64 == bu.SENTINEL:
        return bu.SENTINEL

    return new_b64


def main() -> None:
    args: bu.BrainArgs = bu.parse_brain_args(sys.argv[1:])
    cfg: ChessConfig = ChessConfig(region=args.region, scale=args.scale)

    grid_overlays: list[dict[str, Any]] = bu.make_grid_overlays(
        cfg.grid_size, cfg.grid_color, cfg.grid_stroke_width,
    )

    _wait_for_panel(cfg)
    time.sleep(cfg.post_ready_delay)

    board_b64: str = bu.SENTINEL
    while True:
        try:
            result: str = _run_round(cfg, grid_overlays, board_b64)
        except Exception:
            result = bu.SENTINEL
        if result == bu.SENTINEL:
            time.sleep(cfg.failure_delay)
            board_b64 = bu.SENTINEL
        else:
            board_b64 = result


if __name__ == "__main__":
    main()

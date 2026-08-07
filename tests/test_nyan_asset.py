import json
from pathlib import Path
from tools.anim_encoder import parse_header

ASSET = Path(__file__).resolve().parents[1] / "assets" / "nyan" / "nyan_72x16.anim"
META  = Path(__file__).resolve().parents[1] / "assets" / "nyan" / "meta.json"

def test_committed_asset_parses():
    h = parse_header(ASSET.read_bytes())
    assert h["magic"] == b"bicycle0"
    assert (h["width"], h["height"]) == (72, 16)
    assert h["color_mode"] == 0
    assert h["n_display"] >= 1
    assert h["fps"] == json.loads(META.read_text())["fps"]

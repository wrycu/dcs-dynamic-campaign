"""Write data/theaters/<map>/model_boxes.json: every scenery model's bounding box, from the
terrain's own model files (campaign/model_boxes.py).

    python3 -m tools.extract_model_boxes [--theatre Syria]
"""
from __future__ import annotations

import argparse
import json

from campaign import model_boxes
from tools.extract_theater import DCS_DIR


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--theatre", default="Syria")
    args = ap.parse_args()
    terrain = DCS_DIR / "Mods" / "terrains" / args.theatre
    boxes = model_boxes.read_terrain(terrain)
    out = model_boxes.path_for(args.theatre)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(sorted(boxes.items())), indent=0) + "\n")
    print(f"{len(boxes)} model boxes from {terrain / 'Models'} -> {out}")


if __name__ == "__main__":
    main()

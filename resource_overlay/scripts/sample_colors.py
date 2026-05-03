#!/usr/bin/env python3
"""Print most common RGB colours in an image (helps build config.yaml)."""

from __future__ import annotations

import argparse
from collections import Counter

import numpy as np
from PIL import Image


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image", type=str)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--quantize", type=int, default=0, help="round each channel to this step (e.g. 16)")
    args = ap.parse_args()

    img = Image.open(args.image).convert("RGB")
    a = np.asarray(img)
    flat = a.reshape(-1, 3)
    if args.quantize > 0:
        q = args.quantize
        flat = (flat // q) * q
    keys = [tuple(row.tolist()) for row in flat]
    for rgb, n in Counter(keys).most_common(args.top):
        print(f"{rgb}\t{n}")


if __name__ == "__main__":
    main()

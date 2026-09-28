"""Load JPG/JPEG OMR sheets. Folders are expanded to JPG/JPEG files."""
from __future__ import annotations
import os
import cv2
import numpy as np

IMG_EXT = {".jpg", ".jpeg"}
ALL_EXT = IMG_EXT


def collect_files(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs.sort()
                for name in sorted(names):
                    if os.path.splitext(name)[1].lower() in ALL_EXT:
                        out.append(os.path.join(root, name))
        elif os.path.splitext(p)[1].lower() in ALL_EXT:
            out.append(p)
    return out


def read_image(path):
    """cv2.imread that also works with unicode paths on Windows"""
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def iter_pages(path, dpi=200):
    """Yield one (page_no, BGR image) from a JPG/JPEG sheet."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMG_EXT:
        raise ValueError("OMR input must be a JPG or JPEG image")
    img = read_image(path)
    if img is None:
        raise ValueError("cannot read JPG image")
    yield 1, img

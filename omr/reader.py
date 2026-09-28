"""Bubble reading: measure how dark every bubble is, decide which ones are marked."""
from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
import cv2
import numpy as np
from .template import parse_options

PAD = 64
_disks = {}


def _disk(r):
    if r not in _disks:
        yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
        _disks[r] = (xx * xx + yy * yy <= r * r).astype(np.float32)
    return _disks[r]


def darkness_map(aligned_bgr):
    """0 = paper white, 1 = black. Lighting is flattened first so shadows do not look like marks."""
    gray = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, None, fx=.25, fy=.25, interpolation=cv2.INTER_AREA)
    k = max(9, int(gray.shape[1] * 0.0075) | 1)
    bg = cv2.morphologyEx(small, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    bg = cv2.GaussianBlur(cv2.resize(bg, (gray.shape[1], gray.shape[0])), (0, 0), 3)
    norm = cv2.divide(gray, np.maximum(bg, 1), scale=255)
    dark = (255 - norm).astype(np.float32) / 255.0
    return cv2.copyMakeBorder(dark, PAD, PAD, PAD, PAD, cv2.BORDER_CONSTANT, value=0)


@dataclass
class FieldReading:
    values: list           # one string per position
    marked: np.ndarray     # bool [positions x options]
    scores: np.ndarray     # darkness [positions x options]
    centers: list          # [(cx, cy)] flat, same order as scores.ravel()
    radius: float
    problems: int = 0      # ambiguous / unreadable positions


@dataclass
class HandwritingReading:
    values: list
    confidence: float = 1.0


@lru_cache(maxsize=1)
def _load_handwriting_model():
    try:
        import torch
        from transformers import TrOCRProcessor, VisionEncoderDecoderModel
    except ImportError as e:
        raise RuntimeError("Handwriting OCR needs torch and transformers. Run pip install -r requirements.txt.") from e
    model_id = "microsoft/trocr-base-handwritten"
    try:
        processor = TrOCRProcessor.from_pretrained(model_id)
        model = VisionEncoderDecoderModel.from_pretrained(model_id)
    except Exception as e:
        raise RuntimeError(f"Could not load the local handwriting model {model_id}: {e}") from e
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model.eval()
    return torch, processor, model


def read_handwriting(image_bgr, f) -> HandwritingReading:
    """Recognize a complete handwritten line with TrOCR, which is trained on handwriting."""
    from PIL import Image
    torch, processor, model = _load_handwriting_model()
    x0, y0 = max(0, int(f.x)), max(0, int(f.y))
    x1 = min(image_bgr.shape[1], int(f.x + f.w))
    y1 = min(image_bgr.shape[0], int(f.y + f.h))
    if x1 <= x0 or y1 <= y0:
        return HandwritingReading([""])
    crop = image_bgr[y0:y1, x0:x1]
    if f.orient == "rows":
        crop = cv2.rotate(crop, cv2.ROTATE_90_COUNTERCLOCKWISE)

    # Remove thin box rules at known character-cell boundaries without erasing tall letters.
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    h, w = ink.shape
    cleaned = ink.copy()
    cell_w = w / max(1, f.count)
    half_rule = max(1, round(cell_w * 0.025))
    for i in range(f.count + 1):
        x = round(i * w / f.count)
        cleaned[:, max(0, x - half_rule):min(w, x + half_rule + 1)] = 0
    border_y = max(1, round(h * 0.04))
    cleaned[:border_y, :] = 0
    cleaned[h - border_y:, :] = 0
    page = cv2.copyMakeBorder(cleaned, max(12, h // 12), max(12, h // 12),
                              max(12, h // 12), max(12, h // 12), cv2.BORDER_CONSTANT, value=0)
    # TrOCR expects dark text on a light background.
    page = cv2.bitwise_not(page)
    rgb = cv2.cvtColor(page, cv2.COLOR_GRAY2RGB)
    device = next(model.parameters()).device
    pixel_values = processor(images=Image.fromarray(rgb), return_tensors="pt").pixel_values.to(device)
    with torch.inference_mode():
        out = model.generate(pixel_values, num_beams=4, max_new_tokens=max(32, min(256, f.count * 2)),
                             return_dict_in_generate=True, output_scores=True)
    text = processor.batch_decode(out.sequences, skip_special_tokens=True)[0].strip().upper()
    allowed = set(parse_options(f.options))
    numeric_only = allowed == set("0123456789")
    if numeric_only:
        text = text.translate(str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1",
                                             "|": "1", "S": "5", "B": "8", "Z": "2"}))
    text = "".join(ch for ch in text if ch in allowed or (ch == " " and not numeric_only))
    confidence = 1.0
    scores = getattr(out, "sequences_scores", None)
    if scores is not None and text:
        confidence = float(torch.exp(scores[0]).clamp(0, 1).item())
    return HandwritingReading([text], confidence)


def read_field(dark, f, t) -> FieldReading:
    labels = f.labels()
    n_opt = len(labels)
    cw, ch = f.cell_size()
    probe = f.probe if getattr(f, "probe", 0.0) > 0 else t.probe
    r = int(max(2, min(PAD - 2, round(probe * min(cw, ch)))))
    disk = _disk(r)
    area = float(disk.sum())
    centers = [(cx, cy) for _, _, cx, cy in f.centers()]
    sc = np.zeros(len(centers), np.float32)
    for i, (cx, cy) in enumerate(centers):
        x, y = int(round(cx)) + PAD, int(round(cy)) + PAD
        if y - r < 0 or x - r < 0:
            continue
        patch = dark[y - r:y + r + 1, x - r:x + r + 1]
        if patch.shape == disk.shape:
            sc[i] = float((patch * disk).sum() / area)
    scores = sc.reshape(f.count, n_opt)
    base = np.percentile(scores, 30)                     # level of an EMPTY bubble in this field
    marked = ((scores - base) >= t.mark_delta) & (scores >= t.mark_min)

    joiner = "" if all(len(l) == 1 for l in labels) else ","
    values, problems = [], 0
    for p in range(f.count):
        idx = np.flatnonzero(marked[p])
        if len(idx) == 0:
            values.append(f.blank)
            # A missing subject choice must be surfaced just like a missing ID
            # mark.  Previously subject fields silently returned their blank
            # marker while the sheet was still reported as OK.
            problems += f.kind in ("id", "subject") and f.blank not in (" ", "")
        elif len(idx) == 1:
            values.append(labels[idx[0]])
        elif f.multi and f.kind != "subject":
            values.append(joiner.join(labels[i] for i in idx))
        else:
            order = idx[np.argsort(-scores[p, idx])]
            if scores[p, order[0]] >= t.dominance * scores[p, order[1]]:      # one clearly darker: others are erasures
                marked[p, :] = False
                marked[p, order[0]] = True
                values.append(labels[order[0]])
            else:
                values.append(f.multi_char)
                # Subject bubbles are single-choice. Do not silently accept
                # multiple comparable marks (or a loaded template's multi=True).
                problems += 1
    return FieldReading(values, marked, scores, centers, r, problems)

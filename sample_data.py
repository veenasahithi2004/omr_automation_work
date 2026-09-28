"""Generates a synthetic OMR sheet layout + simulated 'scans' so you can test the whole pipeline without real sheets."""
from __future__ import annotations
import os, random
import cv2
import numpy as np
from omr import Template, Field

W, H = 1654, 2339          # A4 @ 200 dpi


def build_template() -> Template:
    t = Template(name="sample_exam", ref_w=W, ref_h=H)
    t.fields = [
        Field("Name", "id", 100, 300, 560, 624, "columns", 20, "A-Z", blank=" "),
        Field("Hall Ticket No", "id", 720, 300, 300, 260, "columns", 10, "0-9", blank="?"),
        Field("Booklet Code", "id", 1080, 300, 210, 260, "columns", 7, "0-9", blank="?"),
        Field("Set", "id", 1080, 640, 160, 30, "rows", 1, "A-D", blank="?"),
        Field("Subject", "id", 1330, 300, 60, 260, "columns", 2, "0-9", blank="?"),
        Field("Part A (1-25)", "answers", 100, 1050, 176, 850, "rows", 25, "A-D", part="Part A", start=1),
        Field("Part A (26-50)", "answers", 400, 1050, 176, 850, "rows", 25, "A-D", part="Part A", start=26),
        Field("Part B (1-25)", "answers", 760, 1050, 220, 850, "rows", 25, "A-E", part="Part B", start=1),
        Field("Part B (26-40)", "answers", 1100, 1050, 220, 510, "rows", 15, "A-E", part="Part B", start=26),
        Field("Part C", "answers", 1400, 1050, 176, 680, "rows", 20, "1-4", multi=True, part="Part C", start=1),
    ]
    return t


def render_blank(t: Template) -> np.ndarray:
    img = np.full((H, W, 3), 255, np.uint8)
    cv2.rectangle(img, (20, 20), (W - 20, H - 20), (0, 0, 0), 3)
    for x, y in [(45, 45), (W - 85, 45), (45, H - 85), (W - 85, H - 85)]:
        cv2.rectangle(img, (x, y), (x + 40, y + 40), (0, 0, 0), -1)
    cv2.putText(img, "SAMPLE ENTRANCE EXAM 2026 - OMR ANSWER SHEET", (120, 120), cv2.FONT_HERSHEY_DUPLEX, 1.3, (0, 0, 0), 2)
    cv2.putText(img, "Use HB pencil. Fill bubbles completely. Do not fold or staple.", (120, 175), cv2.FONT_HERSHEY_SIMPLEX, .8, (60, 60, 60), 2)
    cv2.putText(img, "Candidate Name", (100, 285), cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 0, 0), 2)
    cv2.putText(img, "Hall Ticket No.", (720, 285), cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 0, 0), 2)
    cv2.putText(img, "Booklet Code", (1080, 285), cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 0, 0), 2)
    cv2.putText(img, "Set", (1080, 625), cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 0, 0), 2)
    cv2.putText(img, "Sub", (1330, 285), cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 0, 0), 2)
    cv2.putText(img, "Invigilator signature: ____________", (720, 800), cv2.FONT_HERSHEY_SIMPLEX, .9, (0, 0, 0), 2)
    cv2.putText(img, "ANSWERS", (100, 1000), cv2.FONT_HERSHEY_DUPLEX, 1.2, (0, 0, 0), 2)
    for f in t.fields:
        cw, ch = f.cell_size()
        rad = int(.36 * min(cw, ch))
        labels = f.labels()
        for p, o, cx, cy in f.centers():
            c = (int(cx), int(cy))
            cv2.circle(img, c, rad, (0, 0, 0), 1, cv2.LINE_AA)
            cv2.putText(img, labels[o][:2], (c[0] - 4 * len(labels[o][:2]), c[1] + 4), cv2.FONT_HERSHEY_SIMPLEX, .32, (90, 90, 90), 1, cv2.LINE_AA)
        if f.kind == "answers" and f.orient == "rows":
            for p in range(f.count):
                cv2.putText(img, str(f.start + p), (int(f.x) - 34, int(f.y + (p + .5) * ch) + 5), cv2.FONT_HERSHEY_SIMPLEX, .45, (0, 0, 0), 1)
            cv2.putText(img, f.part, (int(f.x), int(f.y) - 14), cv2.FONT_HERSHEY_SIMPLEX, .6, (0, 0, 0), 2)
    return img


def random_truth(t: Template, rng: random.Random):
    truth = {}
    for f in t.fields:
        labels = f.labels()
        vals = []
        for _ in range(f.count):
            if f.kind == "id":
                if f.name == "Name":
                    vals.append(rng.choice(labels + [" "] * 6))
                else:
                    vals.append(rng.choice(labels))
            elif f.multi:
                k = rng.choice([0, 1, 1, 1, 2])
                vals.append("".join(sorted(rng.sample(labels, k))))
            else:
                vals.append(rng.choice(labels + [""] * 2))
        truth[f.name] = vals
    return truth


def fill(img, t: Template, truth, rng: random.Random):
    img = img.copy()
    for f in t.fields:
        cw, ch = f.cell_size()
        rad = int(.36 * min(cw, ch))
        labels = f.labels()
        vals = truth[f.name]
        for p, o, cx, cy in f.centers():
            v = vals[p]
            if v and labels[o] in (v if f.multi or len(labels[0]) == 1 else [v]) and (labels[o] in v):
                g = rng.randint(25, 110)
                cv2.circle(img, (int(cx + rng.uniform(-1.5, 1.5)), int(cy + rng.uniform(-1.5, 1.5))),
                           rad - rng.randint(0, 2), (g, g, g), -1, cv2.LINE_AA)
    return img


def simulate_scan(img, rng: random.Random, flip=False):
    """perspective + rotation + lighting gradient + blur + noise + JPEG-ish"""
    h, w = img.shape[:2]
    j = lambda m: rng.uniform(-m, m)
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[60 + j(35), 60 + j(35)], [w - 60 + j(35), 60 + j(35)],
                      [w - 60 + j(35), h - 60 + j(35)], [60 + j(35), h - 60 + j(35)]]) * 0.9
    canvas = (int(w * 0.9) + 40, int(h * 0.9) + 40)
    out = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), canvas, borderValue=(200, 200, 200))
    if flip:
        out = cv2.rotate(out, cv2.ROTATE_180)
    gx = np.linspace(rng.uniform(.75, .95), rng.uniform(.95, 1.05), out.shape[1])[None, :, None]
    out = np.clip(out.astype(np.float32) * gx, 0, 255)
    out = cv2.GaussianBlur(out, (0, 0), rng.uniform(.6, 1.1))
    out += np.random.default_rng(rng.randint(0, 9999)).normal(0, 6, out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


def expected_strings(t: Template, truth):
    ids = {f.name: "".join(truth[f.name]).strip() for f in t.fields if f.kind == "id"}
    answers = {}
    for f in t.fields:
        if f.kind == "answers":
            d = answers.setdefault(f.part, {})
            for i, v in enumerate(truth[f.name]):
                d[f.start + i] = v
    return ids, answers


def make_demo(folder: str, n: int = 5, seed: int = 7):
    """writes template + n simulated scans; returns list of (path, expected ids, expected answers)"""
    rng = random.Random(seed)
    t = build_template()
    os.makedirs(folder, exist_ok=True)
    blank = render_blank(t)
    cv2.imwrite(os.path.join(folder, "blank_reference.png"), blank)
    t.save(os.path.join(folder, "sample_exam.json"), os.path.join(folder, "blank_reference.png"))
    scans = []
    for i in range(n):
        truth = random_truth(t, rng)
        scan = simulate_scan(fill(blank, t, truth, rng), rng, flip=(i == 2))
        p = os.path.join(folder, "scans", f"sheet_{i + 1:03d}.jpg")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        cv2.imwrite(p, scan, [cv2.IMWRITE_JPEG_QUALITY, 85])
        scans.append((p, *expected_strings(t, truth)))
    return scans

"""Bring every scanned sheet into the coordinate system of the reference sheet."""
from __future__ import annotations
import cv2
import numpy as np


def _order_quad(pts):
    pts = np.array(pts, dtype=np.float32).reshape(4, 2)
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], dtype=np.float32)


class Aligner:
    def __init__(self, ref_bgr, mode="auto"):
        self.mode = mode
        self.ref_h, self.ref_w = ref_bgr.shape[:2]
        self.ref_gray = cv2.cvtColor(ref_bgr, cv2.COLOR_BGR2GRAY)
        # features are matched on a reduced copy (fast); the transform is scaled back afterwards
        self.fs = min(1.0, 1000.0 / max(self.ref_w, self.ref_h))
        ref_small = cv2.resize(self.ref_gray, None, fx=self.fs, fy=self.fs, interpolation=cv2.INTER_AREA)
        if hasattr(cv2, "SIFT_create"):
            self.det, self.norm = cv2.SIFT_create(2500), cv2.NORM_L2
        else:
            self.det, self.norm = cv2.ORB_create(4000), cv2.NORM_HAMMING
        self.kp_r, self.des_r = self.det.detectAndCompute(ref_small, None)

    # ------------------------------------------------------------------
    def align(self, bgr):
        """returns (aligned BGR image with the reference size, method string)"""
        m = self.mode
        if m in ("auto", "features"):
            out = self._features(bgr)
            if out is not None:
                return out, "features"
            if m == "features":
                return self._plain(bgr), "none (feature match failed)"
        if m in ("auto", "page"):
            out = self._page(bgr)
            if out is not None:
                return out, "page"
        return self._plain(bgr), "none" if m == "none" else "none (no alignment found)"

    def _plain(self, bgr):
        return cv2.resize(bgr, (self.ref_w, self.ref_h), interpolation=cv2.INTER_AREA)

    def _features(self, bgr, min_inliers=15):
        h, w = bgr.shape[:2]
        s = np.sqrt((self.ref_w * self.ref_h) / float(w * h))       # bring scan to the reference's overall scale
        small = cv2.resize(bgr, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        fs = self.fs
        tiny = cv2.cvtColor(cv2.resize(small, None, fx=fs, fy=fs, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        kp, des = self.det.detectAndCompute(tiny, None)
        if des is None or self.des_r is None or len(kp) < min_inliers:
            return None
        knn = cv2.BFMatcher(self.norm).knnMatch(des, self.des_r, k=2)
        good = [a for a, b in (p for p in knn if len(p) == 2) if a.distance < 0.75 * b.distance]
        if len(good) < min_inliers:
            return None
        src = np.float32([kp[g.queryIdx].pt for g in good]).reshape(-1, 1, 2)
        dst = np.float32([self.kp_r[g.trainIdx].pt for g in good]).reshape(-1, 1, 2)
        H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 2.0)
        if H is None or int(mask.sum()) < min_inliers:
            return None
        if not (0.4 < np.linalg.det(H[:2, :2]) < 2.5):               # absurd warp -> reject
            return None
        down, up = np.diag([fs, fs, 1.0]), np.diag([1 / fs, 1 / fs, 1.0])
        M = up @ H @ down                                             # small-scan pixels -> reference pixels
        return cv2.warpPerspective(small, M, (self.ref_w, self.ref_h), flags=cv2.INTER_LINEAR,
                                   borderMode=cv2.BORDER_REPLICATE)

    def _page(self, bgr):
        """photo of a sheet lying on a table: find paper outline and straighten it"""
        h, w = bgr.shape[:2]
        k = 900.0 / max(h, w)
        small = cv2.resize(bgr, None, fx=k, fy=k)
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        edges = cv2.dilate(cv2.Canny(gray, 50, 150), np.ones((3, 3), np.uint8))
        cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5]:
            if cv2.contourArea(c) < 0.25 * small.shape[0] * small.shape[1]:
                break
            ap = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
            if len(ap) == 4:
                q = _order_quad(ap / k)
                wq = np.linalg.norm(q[1] - q[0]); hq = np.linalg.norm(q[3] - q[0])
                if (wq > hq) != (self.ref_w > self.ref_h):           # landscape/portrait mismatch -> rotate
                    q = np.roll(q, -1, axis=0)
                dst = np.float32([[0, 0], [self.ref_w - 1, 0], [self.ref_w - 1, self.ref_h - 1], [0, self.ref_h - 1]])
                return cv2.warpPerspective(bgr, cv2.getPerspectiveTransform(q, dst), (self.ref_w, self.ref_h))
        return None

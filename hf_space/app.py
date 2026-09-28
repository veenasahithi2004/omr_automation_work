"""Browser front end for OMR Reader, suitable for a Hugging Face ZeroGPU Space.

Images are processed in a temporary directory and results are returned to the
browser as an editable table and CSV. Account settings use DATABASE_URL when
provided (Postgres); the local SQLite fallback is for development only.
"""
from __future__ import annotations

import csv
from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import wave
import math
import struct
from pathlib import Path

import gradio as gr
import numpy as np
import cv2

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from omr import OMRProcessor, Template
from omr.imageio import read_image
from omr.pipeline import SheetResult

try:
    import psycopg
except ImportError:
    psycopg = None

try:
    import spaces
except ImportError:
    spaces = None


ADMIN_USERNAME = os.getenv("OMR_ADMIN_USERNAME", "admin").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SQLITE_PATH = os.getenv("OMR_SQLITE_PATH", os.path.join(tempfile.gettempdir(), "omr_users.db"))
_db_lock = threading.RLock()


def _db():
    @contextmanager
    def connection():
        if DATABASE_URL:
            if psycopg is None:
                raise RuntimeError("Install psycopg[binary] to use DATABASE_URL.")
            from psycopg.rows import dict_row
            conn = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(SQLITE_PATH)), exist_ok=True)
            conn = sqlite3.connect(SQLITE_PATH, timeout=20)
            conn.row_factory = sqlite3.Row
        try:
            yield conn
            if not DATABASE_URL:
                conn.commit()
        except Exception:
            if not DATABASE_URL:
                conn.rollback()
            raise
        finally:
            conn.close()
    return connection()


def _sql(conn, query: str, values=()):
    if DATABASE_URL:
        return conn.execute(query.replace("?", "%s"), values)
    return conn.execute(query, values)


def _init_db():
    with _db_lock, _db() as conn:
        if DATABASE_URL:
            _sql(conn, "CREATE TABLE IF NOT EXISTS omr_users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at BIGINT NOT NULL)")
            _sql(conn, "CREATE TABLE IF NOT EXISTS omr_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            _sql(conn, "CREATE TABLE IF NOT EXISTS omr_login_attempts (username TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0, locked_until BIGINT NOT NULL DEFAULT 0)")
        else:
            _sql(conn, "CREATE TABLE IF NOT EXISTS omr_users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL)")
            _sql(conn, "CREATE TABLE IF NOT EXISTS omr_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            _sql(conn, "CREATE TABLE IF NOT EXISTS omr_login_attempts (username TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0, locked_until INTEGER NOT NULL DEFAULT 0)")
        _sql(conn, "INSERT INTO omr_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING", ("max_users", os.getenv("OMR_MAX_USERS", "5")))
        admin = _sql(conn, "SELECT username FROM omr_users WHERE username=?", (ADMIN_USERNAME,)).fetchone()
        initial_password = os.getenv("OMR_ADMIN_PASSWORD", "")
        if admin is None:
            if len(initial_password) < 12:
                raise RuntimeError("Set OMR_ADMIN_PASSWORD to a unique password of at least 12 characters in the host's Secrets settings, then restart.")
            _sql(conn, "INSERT INTO omr_users(username,password_hash,role,active,created_at) VALUES(?,?,?,?,?)",
                 (ADMIN_USERNAME, _hash_password(initial_password), "admin", 1, int(time.time())))


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + digest.hex()


def _check_password(password: str, encoded: str) -> bool:
    try:
        method, salt_hex, digest_hex = encoded.split("$", 2)
        if method != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def authenticate(username: str, password: str) -> bool:
    if not username or not password:
        return False
    username = username.strip()
    with _db_lock, _db() as conn:
        attempt = _sql(conn, "SELECT attempts,locked_until FROM omr_login_attempts WHERE username=?", (username,)).fetchone()
        now = int(time.time())
        if attempt and int(attempt["locked_until"]) > now:
            return False
        row = _sql(conn, "SELECT password_hash,active FROM omr_users WHERE username=?", (username,)).fetchone()
        valid = bool(row and row["active"] and _check_password(password, row["password_hash"]))
        if valid:
            _sql(conn, "DELETE FROM omr_login_attempts WHERE username=?", (username,))
            return True
        failures = int(attempt["attempts"]) + 1 if attempt else 1
        locked_until = now + 900 if failures >= 5 else 0
        _sql(conn, "INSERT INTO omr_login_attempts(username,attempts,locked_until) VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET attempts=excluded.attempts,locked_until=excluded.locked_until",
             (username, failures, locked_until))
        return False


def _setting(conn, key, default=""):
    row = _sql(conn, "SELECT value FROM omr_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _admin(request: gr.Request) -> bool:
    username = getattr(request, "username", None)
    if not username:
        return False
    with _db_lock, _db() as conn:
        row = _sql(conn, "SELECT role,active FROM omr_users WHERE username=?", (username,)).fetchone()
    return bool(row and row["active"] and row["role"] == "admin")


def admin_snapshot(request: gr.Request):
    if not _admin(request):
        return gr.update(visible=False), "", [], "Admin access only."
    with _db_lock, _db() as conn:
        limit = int(_setting(conn, "max_users", "5"))
        rows = _sql(conn, "SELECT username,role,active,created_at FROM omr_users ORDER BY role DESC,username").fetchall()
    values = [[r["username"], r["role"], "Active" if r["active"] else "Disabled"] for r in rows]
    user_count = sum(bool(r["active"]) and r["role"] == "user" for r in rows)
    return gr.update(visible=True), limit, values, f"{user_count} of {limit} user accounts enabled (admin account is separate)."


def save_limit(new_limit, request: gr.Request):
    if not _admin(request):
        return "Admin access only.", *admin_snapshot(request)[1:]
    try:
        limit = int(new_limit)
        if limit < 1 or limit > 1000:
            raise ValueError
    except (TypeError, ValueError):
        _tab, old_limit, rows, note = admin_snapshot(request)
        return "Enter a whole number from 1 to 1000.", old_limit, rows, note
    with _db_lock, _db() as conn:
        active_count = _sql(conn, "SELECT COUNT(*) AS n FROM omr_users WHERE active=1 AND role='user'").fetchone()["n"]
        if limit < active_count:
            _tab, old_limit, rows, note = admin_snapshot(request)
            return f"Disable accounts first; {active_count} accounts are currently active.", old_limit, rows, note
        _sql(conn, "INSERT INTO omr_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("max_users", str(limit)))
    _tab, actual_limit, rows, note = admin_snapshot(request)
    return "Account limit saved.", actual_limit, rows, note


def add_user(username, password, request: gr.Request):
    if not _admin(request):
        return "Admin access only.", *admin_snapshot(request)[1:]
    username = (username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", username) or username.lower() == ADMIN_USERNAME.lower():
        _tab, limit, rows, note = admin_snapshot(request)
        return "Use a unique 3–32 character username (letters, numbers, dot, underscore, hyphen).", limit, rows, note
    if not password or len(password) < 12:
        _tab, limit, rows, note = admin_snapshot(request)
        return "Set a unique password with at least 12 characters.", limit, rows, note
    with _db_lock, _db() as conn:
        limit = int(_setting(conn, "max_users", "5"))
        count = _sql(conn, "SELECT COUNT(*) AS n FROM omr_users WHERE active=1 AND role='user'").fetchone()["n"]
        exists = _sql(conn, "SELECT username FROM omr_users WHERE username=?", (username,)).fetchone()
        if exists:
            message = "That username already exists."
        elif count >= limit:
            message = "The user limit has been reached. Increase the limit first."
        else:
            _sql(conn, "INSERT INTO omr_users(username,password_hash,role,active,created_at) VALUES(?,?,?,?,?)",
                 (username, _hash_password(password), "user", 1, int(time.time())))
            message = f"Created account {username}. Share its password with that user securely."
    _tab, actual_limit, rows, note = admin_snapshot(request)
    return message, actual_limit, rows, note


def disable_user(username, request: gr.Request):
    if not _admin(request):
        return "Admin access only.", *admin_snapshot(request)[1:]
    username = (username or "").strip()
    if not username or username.lower() == ADMIN_USERNAME.lower():
        _tab, limit, rows, note = admin_snapshot(request)
        return "Select a non-admin account.", limit, rows, note
    with _db_lock, _db() as conn:
        cur = _sql(conn, "UPDATE omr_users SET active=0 WHERE username=? AND role='user'", (username,))
        message = "Account disabled." if cur.rowcount else "No matching user account."
    _tab, limit, rows, note = admin_snapshot(request)
    return message, limit, rows, note


def _safe_input_path(path: str) -> str:
    p = os.path.abspath(str(path))
    allowed = os.path.abspath(os.getenv("GRADIO_TEMP_DIR", os.path.join(tempfile.gettempdir(), "gradio")))
    if os.path.commonpath((p, allowed)) != allowed or not os.path.isfile(p):
        raise ValueError("An uploaded file could not be validated. Please upload it again.")
    return p


def _required_field_names(template: Template):
    names = {"hallticketno", "hallticketnumber", "hallticket", "halltktno", "halltktnumber",
             "class", "admissionnum", "admissionnumber", "admissionno", "admissionnumberno", "partsubject", "centercode"}
    required = []
    for field in template.fields:
        if not field.enabled or not field.has_region() or field.kind not in ("id", "subject", "handwriting"):
            continue
        normalized = re.sub(r"[^a-z0-9]", "", field.name.lower())
        if normalized in names or "hallticket" in normalized or "halltkt" in normalized or ("subject" in normalized and normalized.startswith("part")):
            required.append(field.name)
    return required


def _apply_status(result: SheetResult, required_names):
    result.required_fields = required_names
    return result.status


def _scan_cpu(template: Template, reference, inputs, progress):
    processor = OMRProcessor(template, reference, draw_debug=True)
    results = []
    count = len(inputs)
    required = _required_field_names(template)
    for index, item in enumerate(inputs, 1):
        image = read_image(item["path"])
        if image is None:
            result = SheetResult(source=item["name"], warnings=["failed: could not read JPG/JPEG image"], template_name=template.name)
        else:
            try:
                result = processor.process(image, item["name"])
            except Exception as exc:
                result = SheetResult(source=item["name"], warnings=[f"failed: {exc}"], template_name=template.name)
        result.source = item["name"]
        result.required_fields = required
        results.append(result)
        if progress:
            progress(index / max(1, count), desc=f"Recognized {index}/{count}: {item['name']}")

    # Flag repeated nonblank hall-ticket numbers within this submitted batch.
    field_name = next((name for name in required if "hallticket" in re.sub(r"[^a-z0-9]", "", name.lower())
                       or "halltkt" in re.sub(r"[^a-z0-9]", "", name.lower())), None)
    if field_name:
        groups = {}
        for i, result in enumerate(results):
            val = re.sub(r"[^A-Z0-9]", "", result.ids.get(field_name, "").upper())
            if val:
                groups.setdefault(val, []).append(i)
        for group in groups.values():
            if len(group) > 1:
                for i in group:
                    results[i].duplicate = True
                    results[i].warnings.append(f"Duplicate hall ticket number: {results[i].ids.get(field_name, '')}")
    return results


def scan_batch(template_json, reference_file, jpg_files, folder_files, progress=gr.Progress()):
    if not template_json or not reference_file:
        raise gr.Error("Choose a saved .json template and its reference sheet image.")
    raw_files = []
    for group in (jpg_files or [], folder_files or []):
        if isinstance(group, str):
            raw_files.append(group)
        else:
            raw_files.extend(group)
    accepted = {}
    for item in raw_files:
        path = _safe_input_path(item)
        if Path(path).suffix.lower() in (".jpg", ".jpeg"):
            accepted.setdefault(os.path.normcase(path), path)
    inputs = [{"path": p, "name": os.path.basename(p)} for p in accepted.values()]
    if not inputs:
        raise gr.Error("Select JPG/JPEG files or a root folder containing them.")
    if len(inputs) > 80:
        raise gr.Error("For this free preview, scan at most 80 JPG/JPEG files in one batch.")
    if sum(os.path.getsize(item["path"]) for item in inputs) > 250 * 1024 * 1024:
        raise gr.Error("This batch exceeds the 250 MB upload limit.")

    template_path = _safe_input_path(template_json)
    ref_path = _safe_input_path(reference_file)
    with tempfile.TemporaryDirectory(prefix="omr_template_") as work:
        with open(template_path, "r", encoding="utf-8") as source:
            document = json.load(source)
        reference = document.get("reference", "")
        if not reference or os.path.basename(reference) != reference or reference in (".", ".."):
            raise gr.Error("The template JSON must point to a reference image in its own folder.")
        local_json = os.path.join(work, os.path.basename(template_path))
        local_reference = os.path.join(work, reference)
        shutil.copyfile(template_path, local_json)
        shutil.copyfile(ref_path, local_reference)
        try:
            template = Template.load(local_json)
            ref = read_image(template.reference_path)
            if ref is None:
                raise ValueError("The selected reference file could not be read as an image.")
        except Exception as exc:
            raise gr.Error(f"Could not load the template/reference pair: {exc}") from exc
        has_handwriting = any(f.enabled and f.kind == "handwriting" and f.has_region() for f in template.fields)
        if has_handwriting and spaces is not None and os.getenv("SPACE_ID"):
            results = _scan_gpu(template, ref, inputs, progress)
        else:
            results = _scan_cpu(template, ref, inputs, progress)

    id_cols = list(dict.fromkeys(k for r in results for k in r.ids))
    q_cols = list(dict.fromkeys((part, q) for r in results for part, qs in r.answers.items() for q in sorted(qs)))
    columns = ["Source JPG", "Status"] + id_cols + [f"{part}_Q{q}" for part, q in q_cols] + ["Warnings"]
    rows = []
    for result in results:
        rows.append([result.source, result.status] + [result.ids.get(name, "") for name in id_cols]
                    + [result.answers.get(part, {}).get(q, "") for part, q in q_cols]
                    + ["; ".join(result.warnings)])
    import pandas as pd
    frame = pd.DataFrame(rows, columns=columns)
    status_counts = {key: sum(r.status == key for r in results) for key in ("OK", "CHECK", "BLANK", "DUPLICATE")}
    summary = (f"All {len(inputs)} JPG file(s) have been recognised.  "
               f"OK: {status_counts['OK']} · CHECK: {status_counts['CHECK']} · "
               f"BLANK: {status_counts['BLANK']} · DUPLICATE: {status_counts['DUPLICATE']}\n\n"
               "Review and edit the table below, then use Save corrections & download CSV. "
               "Uploaded images are processed temporarily and are not stored as scan records.")
    gallery = []
    for result in results[:18]:
        if result.debug is not None:
            image = result.debug
            h, w = image.shape[:2]
            if w > 900:
                image = cv2.resize(image, (900, int(h * 900 / w)), interpolation=cv2.INTER_AREA)
            gallery.append((image, result.source))
    gr.Info(f"All {len(inputs)} JPG file(s) have been recognised.")
    return summary, frame, gr.update(value=gallery, visible=bool(gallery))


def _scan_gpu(template, reference, inputs, progress):
    return _scan_gpu_impl(template, reference, inputs, progress)


if spaces is not None:
    _scan_gpu = spaces.GPU(duration=300)(_scan_gpu)


def export_corrections(frame):
    if frame is None:
        raise gr.Error("Scan a batch first.")
    try:
        data = frame.to_dict(orient="records")
        columns = list(frame.columns)
    except AttributeError:
        columns = frame[0]
        data = [dict(zip(columns, row)) for row in frame[1:]]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(data)
    path = os.path.join(tempfile.gettempdir(), f"omr_corrections_{secrets.token_hex(5)}.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(output.getvalue())
    return path, f"Saved corrected CSV with {len(data)} sheet row(s)."


def _bell_file():
    path = os.path.join(tempfile.gettempdir(), "omr_scan_complete_bell.wav")
    if not os.path.isfile(path):
        rate, duration = 22050, 0.45
        with wave.open(path, "wb") as sound:
            sound.setnchannels(1)
            sound.setsampwidth(2)
            sound.setframerate(rate)
            frames = []
            for i in range(int(rate * duration)):
                t = i / rate
                envelope = math.exp(-5.5 * t)
                sample = int(9000 * envelope * (math.sin(2 * math.pi * 880 * t) + 0.45 * math.sin(2 * math.pi * 1320 * t)))
                frames.append(struct.pack("<h", max(-32768, min(32767, sample))))
            sound.writeframes(b"".join(frames))
    return path


def _gpu_scan_impl(template, reference, inputs, progress):
    return _scan_cpu(template, reference, inputs, progress)


# Keep the CPU/GPU selection separate: bubble-only batches do not use ZeroGPU.
_scan_gpu_impl = _gpu_scan_impl


def build_app():
    _init_db()
    with gr.Blocks(title="OMR Reader") as demo:
        gr.Markdown("# OMR Reader\nSecure OMR scanning with editable recognition results.")
        with gr.Tab("Scan JPG sheets"):
            gr.Markdown("Upload a saved JSON template and its reference image. Select multiple JPGs or a root folder; nested subfolders are included.")
            with gr.Row():
                template_json = gr.File(label="Saved template (.json)", file_types=[".json"], type="filepath")
                reference_file = gr.File(label="Template reference image", file_types=[".jpg", ".jpeg", ".png"], type="filepath")
            with gr.Row():
                jpg_files = gr.File(label="Select JPG files", file_types=[".jpg", ".jpeg"], file_count="multiple", type="filepath")
                folder_files = gr.File(label="Select root folder (includes subfolders)", file_types=[".jpg", ".jpeg"], file_count="directory", type="filepath")
            scan_button = gr.Button("Start scan", variant="primary")
            scan_summary = gr.Markdown()
            results_table = gr.Dataframe(label="Review results — cells are editable", interactive=True, wrap=True)
            download_button = gr.Button("Save corrections & download CSV")
            corrected_file = gr.File(label="Corrected CSV")
            correction_status = gr.Markdown()
            completion_bell = gr.Audio(label="Scan completion bell", autoplay=True, visible=False)
            scan_gallery = gr.Gallery(label="Recognized sheets", columns=3, height="auto", visible=False)
            scan_button.click(scan_batch, inputs=[template_json, reference_file, jpg_files, folder_files],
                               outputs=[scan_summary, results_table, scan_gallery], concurrency_limit=1).then(
                                   lambda: gr.update(value=_bell_file(), visible=True), outputs=completion_bell)
            download_button.click(export_corrections, inputs=results_table,
                                  outputs=[corrected_file, correction_status])
        with gr.Tab("Admin", visible=False) as admin_tab:
            gr.Markdown("## User access\nAccounts use usernames and passwords only; no email addresses are collected. Passwords are stored as salted scrypt hashes.")
            with gr.Row():
                limit_input = gr.Number(label="Maximum active user accounts (admin excluded)", precision=0, minimum=1, maximum=1000)
                save_limit_button = gr.Button("Save user limit")
            limit_status = gr.Markdown()
            users_table = gr.Dataframe(headers=["Username", "Role", "Status"], interactive=False, label="Accounts")
            with gr.Row():
                new_username = gr.Textbox(label="New username (3–32 characters)")
                new_password = gr.Textbox(label="Initial password (12+ characters)", type="password")
                add_user_button = gr.Button("Create account")
            with gr.Row():
                disable_username = gr.Textbox(label="Username to disable")
                disable_button = gr.Button("Disable account", variant="stop")
            admin_status = gr.Markdown()
            demo.load(admin_snapshot, outputs=[admin_tab, limit_input, users_table, limit_status])
            save_limit_button.click(save_limit, inputs=limit_input, outputs=[admin_status, limit_input, users_table, limit_status])
            add_user_button.click(add_user, inputs=[new_username, new_password], outputs=[admin_status, limit_input, users_table, limit_status])
            disable_button.click(disable_user, inputs=disable_username, outputs=[admin_status, limit_input, users_table, limit_status])
    return demo


if __name__ == "__main__":
    app = build_app()
    app.queue(default_concurrency_limit=1).launch(
        server_name="0.0.0.0", server_port=int(os.getenv("PORT", "7860")),
        auth=authenticate,
        auth_message="Sign in with your username and password. No email is needed; ask the administrator for an account.",
        max_file_size="25mb", analytics_enabled=False, show_api=False,
        delete_cache=(60, 120),
    )

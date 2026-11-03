# -*- coding: utf-8 -*-
"""
工作台集成模块：门户 + 活动档案管理台 + 童颜守护

职责：
  1. 提供统一入口门户（/）
  2. 挂载「活动档案管理台」页面与 CSV 读写 API（/archive、/archive/api/*）
  3. 挂载「童颜守护」照片脱敏页面与图片保存 API（/tongyan、/tongyan/api/*）

本模块被 app.py 在启动时调用 register_modules(app) 注册，不改动原有报名业务代码。
"""

import base64
import csv
import io
import os
import re
import time
import uuid
from datetime import datetime

from flask import jsonify, request, send_file, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")
ARCHIVE_DATA = os.path.join(ARCHIVE_DIR, "data")
TONGYAN_DIR = os.path.join(BASE_DIR, "tongyan")
TONGYAN_SAVED = os.path.join(TONGYAN_DIR, "saved")
PORTAL_DIR = os.path.join(BASE_DIR, "portal")

ARCHIVE_TABLES = [
    "activities", "persons", "registrations", "checkins", "exceptions",
    "authorizations", "ledgers", "photo_index", "_oplog", "_schema", "_dict",
]

SAVE_LIMIT = 32 * 1024 * 1024  # 单次保存上限 32MB（base64 编码后）


# --------------------------- CSV 读写 ---------------------------

def _table_path(name):
    if name not in ARCHIVE_TABLES:
        raise ValueError("unknown table: %s" % name)
    return os.path.join(ARCHIVE_DATA, name + ".csv")


def read_table(name):
    """读取某表 → [{...}]"""
    path = _table_path(name)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return []
    header = [str(c).strip() for c in rows[0]]
    out = []
    for r in rows[1:]:
        item = {}
        for i, h in enumerate(header):
            item[h] = r[i] if i < len(r) else ""
        out.append(item)
    return out


def read_table_header(name):
    path = _table_path(name)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if row and any(str(c).strip() for c in row):
                return [str(c).strip() for c in row]
    return []


def write_table(name, rows):
    """整表覆盖写回 CSV。表头优先沿用原文件，其次按首行键推断。"""
    path = _table_path(name)
    header = read_table_header(name)
    if not header:
        for r in rows or []:
            if isinstance(r, dict) and r:
                header = list(r.keys())
                break
    if not header:
        return False
    os.makedirs(ARCHIVE_DATA, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows or []:
            w.writerow({k: r.get(k, "") for k in header})
    os.replace(tmp, path)
    return True


# --------------------------- 童颜守护：文件名 ---------------------------

def _safe_prefix(name, default="photo"):
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]", "", str(name or "")).strip()
    s = re.sub(r"\s+", "_", s)
    s = s[:60].strip("._")
    return s or default


def _saved_files():
    if not os.path.isdir(TONGYAN_SAVED):
        return []
    items = []
    for fn in os.listdir(TONGYAN_SAVED):
        p = os.path.join(TONGYAN_SAVED, fn)
        if not os.path.isfile(p) or not fn.lower().endswith(".png"):
            continue
        st = os.stat(p)
        items.append({
            "file": fn,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


# --------------------------- 路由注册 ---------------------------

def register_modules(app):
    """在既有 Flask app 上挂载门户与两个模块"""

    # ---- 门户 ----
    @app.route("/")
    def portal_home():
        return send_file(os.path.join(PORTAL_DIR, "index.html"))

    @app.route("/favicon.svg")
    def favicon_svg():
        return send_file(os.path.join(ARCHIVE_DIR, "favicon.svg"),
                         mimetype="image/svg+xml")

    # ---- 模块二：活动档案管理台 ----
    @app.route("/archive")
    @app.route("/archive/")
    def archive_page():
        return send_file(os.path.join(ARCHIVE_DIR, "index.html"))

    @app.route("/archive/favicon.svg")
    def archive_favicon():
        return send_file(os.path.join(ARCHIVE_DIR, "favicon.svg"),
                         mimetype="image/svg+xml")

    @app.route("/archive/api/data", methods=["GET"])
    def archive_get_data():
        try:
            data = {t: read_table(t) for t in ARCHIVE_TABLES}
            return jsonify({"ok": True, "data": data})
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/archive/api/data", methods=["POST"])
    def archive_post_data():
        try:
            payload = (request.get_json(force=True, silent=True) or {}).get("tables")
            if not isinstance(payload, dict):
                return jsonify({"ok": False, "error": "missing tables"}), 400
            saved = []
            for name, rows in payload.items():
                if name not in ARCHIVE_TABLES:
                    continue
                rows = rows if isinstance(rows, list) else []
                # 只保留 dict 行，避免脏数据写坏文件
                rows = [r for r in rows if isinstance(r, dict)]
                if write_table(name, rows):
                    saved.append(name)
            return jsonify({"ok": True, "saved": saved})
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/archive/api/health", methods=["GET"])
    def archive_health():
        counts = {t: len(read_table(t)) for t in ARCHIVE_TABLES}
        return jsonify({"ok": True, "mode": "server", "counts": counts})

    @app.route("/archive/api/export/<table>", methods=["GET"])
    def archive_export(table):
        if table not in ARCHIVE_TABLES:
            return "not found", 404
        rows = read_table(table)
        header = read_table_header(table)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore")
        if header:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})
        out = io.BytesIO(("\ufeff" + buf.getvalue()).encode("utf-8"))
        return send_file(out, mimetype="text/csv", as_attachment=True,
                         download_name="%s.csv" % table)

    # ---- 模块三：童颜守护 ----
    @app.route("/tongyan")
    @app.route("/tongyan/")
    def tongyan_page():
        return send_file(os.path.join(TONGYAN_DIR, "index.html"))

    @app.route("/tongyan/api/save", methods=["POST"])
    def tongyan_save():
        try:
            body = request.get_json(force=True, silent=True) or {}
            data_url = body.get("dataUrl") or ""
            m = re.match(r"^data:image/(png|jpeg|jpg);base64,(.*)$", data_url, re.S)
            if not m:
                return jsonify({"ok": False, "error": "仅支持 PNG / JPG 图片"}), 400
            payload = m.group(2)
            if len(payload) > SAVE_LIMIT:
                return jsonify({"ok": False, "error": "图片超过 32MB 上限"}), 413
            raw = base64.b64decode(payload)
            ext = ".jpg" if m.group(1) in ("jpeg", "jpg") else ".png"
            prefix = _safe_prefix(body.get("name"), "photo")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            fname = "%s_%s-%s%s" % (prefix, stamp, uuid.uuid4().hex[:4], ext)
            os.makedirs(TONGYAN_SAVED, exist_ok=True)
            with open(os.path.join(TONGYAN_SAVED, fname), "wb") as f:
                f.write(raw)
            return jsonify({
                "ok": True,
                "file": fname,
                "url": "/tongyan/saved/" + fname,
                "size": len(raw),
                "mime": "image/jpeg" if ext == ".jpg" else "image/png",
                "mtime": datetime.fromtimestamp(time.time()).isoformat(),
            })
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/tongyan/api/saved", methods=["GET"])
    def tongyan_saved_list():
        try:
            return jsonify({"ok": True, "files": _saved_files()})
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/tongyan/api/saved/<path:filename>", methods=["DELETE"])
    def tongyan_saved_delete(filename):
        # 防目录穿越：只接受单层文件名
        if "/" in filename or "\\" in filename or ".." in filename:
            return jsonify({"ok": False, "error": "非法文件名"}), 400
        p = os.path.join(TONGYAN_SAVED, filename)
        if not os.path.isfile(p):
            return jsonify({"ok": False, "error": "文件不存在"}), 404
        try:
            os.remove(p)
            return jsonify({"ok": True})
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/tongyan/saved/<path:filename>")
    def tongyan_saved_file(filename):
        if "/" in filename or "\\" in filename or ".." in filename:
            return "not found", 404
        return send_from_directory(TONGYAN_SAVED, filename)

    return app

# -*- coding: utf-8 -*-
"""
示例机构多来源报名统一接入 Demo MVP 后端
技术栈：Python + Flask + SQLite
主链路：数据接入 -> 入库 -> 列表展示 -> 筛选 -> 导出
"""

import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime
from functools import wraps

from flask import Flask, jsonify, request, send_file, g, session
from flask_cors import CORS

from config import (
    CORS_ORIGINS, DEFAULT_NEW_USER_PASSWORD, DEMO_PARENT_PHONE,
    DEMO_VOLUNTEER_PHONE, ENABLE_DEMO_LOGIN, SECRET_KEY,
    SEED_ADMIN_PASSWORD, SEED_WORKER_PASSWORD,
)

# --------------------------- 配置 ---------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "rainbow.db")

app = Flask(__name__, static_url_path="/static", static_folder="static")
app.config["JSON_AS_ASCII"] = False  # 中文正常输出
app.secret_key = SECRET_KEY  # 会话签名密钥来自环境变量，未配置时随机生成
if CORS_ORIGINS:  # 未配置跨域白名单时不开放跨域（同源部署无需 CORS）
    CORS(app, resources={r"/api/*": {"origins": CORS_ORIGINS}}, supports_credentials=True)

# 枚举常量（与交付物文档一致）
SOURCE_TYPES = ["miniapp", "group_signup", "teacher_proxy", "phone_private"]
SOURCE_LABELS = {
    "miniapp": "小程序",
    "group_signup": "群接龙",
    "teacher_proxy": "老师代报",
    "phone_private": "电话/私聊",
}
STATUS_TYPES = ["待处理", "已确认", "已合并", "已修正"]

# 任务3.5：活动状态枚举
ACTIVITY_STATUS_TYPES = ["进行中", "已结束"]

# --------------------------- 任务5：账号权限 + 服务提示 常量 ---------------------------

ROLES = ["admin", "social_worker", "volunteer", "parent"]
ROLE_LABELS = {
    "admin": "管理员/机构负责人",
    "social_worker": "社工/康复师",
    "volunteer": "志愿者",
    "parent": "家长",
}
USER_STATUSES = ["待审核", "启用", "停用", "已驳回"]

# 权限键（角色权限矩阵可配置）
PERMISSION_KEYS = [
    ("manage_users", "账号与权限配置"),
    ("manage_activities", "活动管理"),
    ("manage_enrollments", "报名管理与异常处理"),
    ("attendance_callback", "签到回传（全量名单）"),
    ("export_data", "签到表/台账导出"),
    ("history_profiles", "历史信息库"),
    ("view_sensitive", "敏感字段查看/修改"),
    ("volunteer_portal", "志愿者端"),
    ("parent_portal", "家长端"),
]

DEFAULT_ROLE_PERMISSIONS = {
    "admin": {k: 1 for k, _ in PERMISSION_KEYS},
    "social_worker": {
        "manage_users": 0, "manage_activities": 1, "manage_enrollments": 1, "attendance_callback": 1,
        "export_data": 1, "history_profiles": 1, "view_sensitive": 1,
        "volunteer_portal": 0, "parent_portal": 0,
    },
    "volunteer": {k: (1 if k == "volunteer_portal" else 0) for k, _ in PERMISSION_KEYS},
    "parent": {k: (1 if k == "parent_portal" else 0) for k, _ in PERMISSION_KEYS},
}

# 敏感字段（仅 view_sensitive 权限=社工/管理员可见；志愿者端返回占位提示）
SENSITIVE_FIELDS = [
    ("allergy_info", "过敏原"),
    ("emotion_triggers", "情绪触发点"),
    ("toilet_habits", "如厕习惯"),
]
SENSITIVE_PLACEHOLDER = "仅社工可见"

# 活动服务提示字段池（按活动勾选启用；志愿者只看启用维度 + 配对孩子）
SERVICE_TIP_FIELDS = ["陪护重点", "沟通方式", "安全注意事项", "饮食注意", "携带物品", "其他提示"]


# 任务4：现场签到差异回传口径（纸质签到表勾选后誊录回传）
ATTENDANCE_STATUSES = ["到场", "未到场", "临时到场"]
ATTENDANCE_LABELS = {
    "到场": "tag-ok",
    "未到场": "tag-danger",
    "临时到场": "tag-warn",
}

# 任务3.5：历史自动补全允许的字段（只补报名里为空的字段，绝不覆盖已有值）
AUTOFILL_FIELDS = ["student_real_name", "student_nickname", "parent_name", "phone", "relation"]
AUTOFILL_FIELD_LABELS = {
    "student_real_name": "学生真名",
    "student_nickname": "学生小名",
    "parent_name": "家长姓名",
    "phone": "手机号",
    "relation": "与孩子关系",
}

# 人工手动标签预设项（社工可自定义补充）
MANUAL_TAG_PRESETS = [
    "信息不实",
    "非本人报名",
    "联系方式无效",
    "疑似重复（人工判断）",
    "需电话回访确认",
    "已退报",
]

# --------------------------- 数据库 ---------------------------

def get_db():
    """获取 SQLite 连接，使用 Row 工厂便于序列化"""
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    """初始化数据库表"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS enrollments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_real_name TEXT,
            student_nickname TEXT,
            parent_name TEXT,
            phone TEXT,
            id_card TEXT,
            emergency_contact TEXT,
            relation TEXT,
            source_type TEXT NOT NULL,
            source_user TEXT,
            source_time TEXT,
            import_batch_id TEXT,
            anomaly_tags TEXT DEFAULT '[]',
            status TEXT DEFAULT '待处理',
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # 常用查询索引
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_source_type ON enrollments(source_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_status ON enrollments(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_phone ON enrollments(phone)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_real_name ON enrollments(student_real_name)")

    # 操作日志表，用于记录人工兜底过程
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enrollment_id INTEGER,
            import_batch_id TEXT,
            operation_type TEXT NOT NULL,
            field_name TEXT,
            old_value TEXT,
            new_value TEXT,
            tag TEXT,
            operator TEXT,
            operated_at TEXT NOT NULL,
            FOREIGN KEY (enrollment_id) REFERENCES enrollments(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_op_enrollment ON operation_logs(enrollment_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_op_batch ON operation_logs(import_batch_id)")

    # 导入预览缓存（持久化，避免服务重启后社工已处理的预览丢失）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS import_previews (
            preview_id TEXT PRIMARY KEY,
            import_batch_id TEXT,
            source_user TEXT,
            records TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # 兼容旧库：为 enrollments 增加人工兜底结果字段
    existing_cols = [r[1] for r in cursor.execute("PRAGMA table_info(enrollments)").fetchall()]
    migrations = [
        ("system_tags", "system_tags TEXT DEFAULT '[]'"),
        ("manual_tags", "manual_tags TEXT DEFAULT '[]'"),
        ("confirmed_ok", "confirmed_ok INTEGER DEFAULT 0"),
        ("not_duplicate", "not_duplicate INTEGER DEFAULT 0"),
        ("merged_into", "merged_into INTEGER DEFAULT 0"),
        ("history_profile_id", "history_profile_id INTEGER DEFAULT 0"),
        ("history_status", "history_status TEXT DEFAULT ''"),
        ("activity_id", "activity_id INTEGER DEFAULT 0"),
        # 任务4：纸质签到表现场差异回传（到场/未到场/临时到场）
        ("attendance_status", "attendance_status TEXT DEFAULT ''"),
        ("attendance_note", "attendance_note TEXT DEFAULT ''"),
        ("attendance_time", "attendance_time TEXT DEFAULT ''"),
        ("attendance_operator", "attendance_operator TEXT DEFAULT ''"),
    ]
    for col, ddl in migrations:
        if col not in existing_cols:
            cursor.execute(f"ALTER TABLE enrollments ADD COLUMN {ddl}")

    # 任务3.5：活动层——每条报名必须归属一个活动；异常处理/留痕/状态挂在活动下，便于按活动归档导出
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            activity_time TEXT DEFAULT '',
            location TEXT DEFAULT '',
            capacity INTEGER DEFAULT 0,
            deadline TEXT DEFAULT '',
            status TEXT DEFAULT '进行中',
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_status ON activities(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_enrollment_activity ON enrollments(activity_id)")

    # 任务3：历史信息库（儿童档案）——沉淀已确认的「真名/小名/家长/手机号」对应关系
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS child_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_real_name TEXT DEFAULT '',
            student_nickname TEXT DEFAULT '',
            parent_name TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            relation TEXT DEFAULT '',
            note TEXT DEFAULT '',
            enrollment_count INTEGER DEFAULT 0,
            enrollment_ids TEXT DEFAULT '',
            history_status TEXT DEFAULT '',
            merged_into INTEGER DEFAULT 0,
            confirmed_by TEXT DEFAULT '',
            confirmed_at TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_profile_phone ON child_profiles(phone)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_profile_real ON child_profiles(student_real_name)")

    # 档案操作日志：建档 / 编辑 / 合并 / 命中确认，全部可追溯
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS profile_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            operation_type TEXT NOT NULL,
            field_name TEXT DEFAULT '',
            old_value TEXT DEFAULT '',
            new_value TEXT DEFAULT '',
            tag TEXT DEFAULT '',
            operator TEXT DEFAULT '',
            operated_at TEXT NOT NULL,
            FOREIGN KEY (profile_id) REFERENCES child_profiles(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_plog_profile ON profile_logs(profile_id)")

    # --------------------------- 任务5：账号权限 + 活动服务提示 ---------------------------
    # 账号表：角色绑定账号（不能自选）；注册 = 申请意向，审核通过才有角色
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT DEFAULT '',
            display_name TEXT DEFAULT '',
            role TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            status TEXT DEFAULT '待审核',
            apply_role TEXT DEFAULT '',
            org_name TEXT DEFAULT '',
            staff_no TEXT DEFAULT '',
            reject_reason TEXT DEFAULT '',
            reviewed_by TEXT DEFAULT '',
            reviewed_at TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # 角色权限矩阵（管理员可配置；allowed: 1=允许 0=禁止）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS role_permissions (
            role TEXT NOT NULL,
            perm TEXT NOT NULL,
            allowed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (role, perm)
        )
    """)

    # 关键操作审计日志：注册申请/审核/登录/权限变更/敏感字段查看与修改/合并档案复核
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 0,
            username TEXT DEFAULT '',
            action TEXT NOT NULL,
            target_type TEXT DEFAULT '',
            target_id TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action)")

    # 手机验证码（家长/志愿者验证码登录；Demo：验证码随接口返回模拟短信下发）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS verification_codes (
            phone TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # 志愿者-孩子配对（按活动）；志愿者只看自己配对孩子的服务提示
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS volunteer_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL DEFAULT 0,
            volunteer_user_id INTEGER NOT NULL DEFAULT 0,
            enrollment_id INTEGER NOT NULL DEFAULT 0,
            note TEXT DEFAULT '',
            created_by TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assign_volunteer ON volunteer_assignments(volunteer_user_id)")

    # 任务5：报名敏感字段 + 家长确认/授权
    existing_cols = [r[1] for r in cursor.execute("PRAGMA table_info(enrollments)").fetchall()]
    for col, ddl in [
        ("allergy_info", "allergy_info TEXT DEFAULT ''"),
        ("emotion_triggers", "emotion_triggers TEXT DEFAULT ''"),
        ("toilet_habits", "toilet_habits TEXT DEFAULT ''"),
        ("service_tips", "service_tips TEXT DEFAULT '{}'"),
        ("parent_confirmed", "parent_confirmed INTEGER DEFAULT 0"),
        ("parent_authorized", "parent_authorized INTEGER DEFAULT 0"),
        ("parent_confirmed_at", "parent_confirmed_at TEXT DEFAULT ''"),
        ("parent_authorized_at", "parent_authorized_at TEXT DEFAULT ''"),
    ]:
        if col not in existing_cols:
            cursor.execute(f"ALTER TABLE enrollments ADD COLUMN {ddl}")

    # 任务5：活动服务提示字段配置（该活动要在志愿者端展示的提示维度）
    act_cols = [r[1] for r in cursor.execute("PRAGMA table_info(activities)").fetchall()]
    if "tip_fields" not in act_cols:
        cursor.execute("ALTER TABLE activities ADD COLUMN tip_fields TEXT DEFAULT '[]'")

    # 首次启动创建管理员；口令来自环境变量/配置，源码内不硬编码
    if cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        cursor.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, display_name, role, phone, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("admin", hash_password(SEED_ADMIN_PASSWORD), "管理员", "admin", "", "启用", now_str(), now_str()),
        )

    # 默认角色权限矩阵（已存在则不动，保留管理员的自定义配置）
    for role, perms in DEFAULT_ROLE_PERMISSIONS.items():
        for perm, allowed in perms.items():
            cursor.execute(
                "INSERT OR IGNORE INTO role_permissions (role, perm, allowed) VALUES (?, ?, ?)",
                (role, perm, 1 if allowed else 0),
            )

    conn.commit()
    conn.close()


# --------------------------- 工具函数 ---------------------------

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------- 任务5：认证 / 权限 / 审计 ---------------------------

def hash_password(password):
    """Demo 级口令哈希（生产环境应加盐 + 慢哈希）"""
    import hashlib

    return hashlib.sha256((password or "").encode("utf-8")).hexdigest()


def current_user():
    """从会话取当前登录用户（dict 或 None）"""
    uid = session.get("uid")
    if not uid:
        return None
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row) if row else None


def log_audit(action, target_type="", target_id="", detail="", user=None):
    """关键操作审计：登录/注册/审核/权限变更/敏感字段查看与修改/合并档案"""
    u = user or current_user() or {}
    db = g.db if hasattr(g, "db") and g.db is not None else None
    conn = db or get_db()
    conn.execute(
        "INSERT INTO audit_logs (user_id, username, action, target_type, target_id, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (u.get("id", 0), u.get("username", ""), action, str(target_type), str(target_id), detail, now_str()),
    )
    conn.commit()


def require_login():
    """登录校验；待审核/停用/驳回账号不能使用任何管理功能"""
    user = current_user()
    if not user:
        return None, (jsonify({"error": "未登录，请先登录", "need_login": True}), 401)
    if user["status"] != "启用":
        msg = {
            "待审核": "账号待审核，审核通过后才能使用管理功能",
            "已驳回": f"注册申请已驳回：{user.get('reject_reason') or '不符合条件'}",
            "停用": "账号已停用，请联系管理员",
        }.get(user["status"], f"账号状态异常：{user['status']}")
        return None, (jsonify({"error": msg, "status": user["status"], "need_login": True}), 403)
    return user, None


def has_perm(user, perm):
    """查角色权限矩阵"""
    db = get_db()
    row = db.execute(
        "SELECT allowed FROM role_permissions WHERE role = ? AND perm = ?", (user["role"], perm)
    ).fetchone()
    return bool(row and row["allowed"])


def require_perm(perm):
    """权限矩阵校验装饰器（先登录且状态启用，再查角色权限）"""

    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user, err = require_login()
            if err:
                return err
            if not has_perm(user, perm):
                return jsonify({
                    "error": f"没有权限：{dict(PERMISSION_KEYS).get(perm, perm)}（当前角色：{ROLE_LABELS.get(user['role'], user['role'])}）",
                    "need_perm": perm,
                }), 403
            g.current_user = user
            return fn(*args, **kwargs)

        return wrapper

    return deco


def require_login_api(fn):
    """仅需登录（不限角色）的端点：如活动基础信息列表"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user, err = require_login()
        if err:
            return err
        g.current_user = user
        return fn(*args, **kwargs)

    return wrapper


def sanitize_enrollment(d, user):
    """
    敏感字段分级：无 view_sensitive 权限的用户（志愿者/家长走专用端点时也经过此处）
    返回的报名数据中，过敏原/情绪触发点/如厕习惯置为占位提示。
    """
    if user and has_perm(user, "view_sensitive"):
        return d
    for col, _label in SENSITIVE_FIELDS:
        if d.get(col):
            d[col] = SENSITIVE_PLACEHOLDER
    return d


def parse_tags(value):
    """把标签字段（JSON 字符串或列表）解析为列表"""
    if isinstance(value, list):
        return value
    try:
        return json.loads(value or "[]")
    except Exception:
        return []


def row_to_dict(row):
    """把 sqlite3.Row 转换为前端可用的 dict"""
    data = dict(row)
    data["anomaly_tags"] = parse_tags(data.get("anomaly_tags"))
    data["system_tags"] = parse_tags(data.get("system_tags"))
    data["manual_tags"] = parse_tags(data.get("manual_tags"))
    data["confirmed_ok"] = bool(data.get("confirmed_ok"))
    data["not_duplicate"] = bool(data.get("not_duplicate"))
    return data


DUPLICATE_TAG_PREFIX = "重复报名"


def is_duplicate_tag(tag):
    return str(tag).startswith(DUPLICATE_TAG_PREFIX)


# --------------------------- 任务3.5：活动层 ---------------------------

def activity_to_dict(row, enrollment_count=None):
    """activities Row → dict（capacity=0 表示不限）"""
    data = dict(row)
    if enrollment_count is not None:
        data["enrollment_count"] = enrollment_count
    return data


def get_activity(db, activity_id):
    """按 id 查活动，不存在返回 None"""
    if not activity_id:
        return None
    row = db.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    return dict(row) if row else None


def check_activity_for_enrollment(db, activity_id):
    """
    报名归属活动校验（每条报名必须归属一个「进行中」的活动）：
    返回 (activity_dict, None) 或 (None, 错误信息)
    """
    if not activity_id:
        return None, "缺少归属活动（activity_id），报名必须归属一个活动"
    activity = get_activity(db, activity_id)
    if not activity:
        return None, f"归属活动不存在（activity_id={activity_id}）"
    if activity["status"] != "进行中":
        return None, f"活动「{activity['name']}」已结束，不能再接入报名"
    return activity, None


def get_activity_name_map(db):
    """{activity_id: name}，列表/导出展示用"""
    return {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM activities").fetchall()}


def apply_history_autofill(record, profile):
    """
    任务3.5：历史命中 → 自动补全报名缺失字段。
    规则：只补报名里为空的字段；档案里非空才补；绝不覆盖已有值；不改档案、不自动合并。
    返回补全明细列表 [{field, value, profile_id}]。
    """
    filled = []
    for f in AUTOFILL_FIELDS:
        cur = (record.get(f) or "").strip()
        pval = (profile.get(f) or "").strip()
        if not cur and pval:
            record[f] = pval
            filled.append({"field": f, "value": pval, "profile_id": profile["id"]})
    return filled


def log_autofill(record_or_db, enrollment_dict, filled, operator, import_batch_id=None):
    """
    自动补全留痕。两种形态：
    - 预览 record：record_or_db 是 dict → 写入 record["operation_logs"]
    - 已入库报名：record_or_db 是 db 连接 → 写入 operation_logs 表
    """
    for item in filled:
        label = AUTOFILL_FIELD_LABELS.get(item["field"], item["field"])
        msg = f"命中历史档案 #{item['profile_id']}，自动补全「{label}」→ {item['value']}（请人工复核）"
        if isinstance(record_or_db, dict):
            add_operation_log(record_or_db, {
                "type": "history_autofill",
                "field": item["field"],
                "old_value": "",
                "new_value": item["value"],
                "tag": f"来自档案#{item['profile_id']}",
                "message": msg,
                "operator": operator,
            })
        else:
            write_log(record_or_db, enrollment_dict.get("id"), import_batch_id,
                      "history_autofill", field_name=item["field"], old_value="",
                      new_value=item["value"], tag=f"来自档案#{item['profile_id']}", operator=operator)


# --------------------------- 任务3：历史信息库（儿童档案） ---------------------------

def profile_to_dict(row):
    """child_profiles Row → dict（enrollment_ids 转列表）"""
    data = dict(row)
    data["enrollment_ids"] = [int(x) for x in str(data.get("enrollment_ids") or "").split(",") if x.strip().isdigit()]
    data["merged"] = bool(data.get("merged_into"))
    return data


def get_all_profiles(db):
    """全部档案（含已合并，调用方按需过滤）"""
    return [profile_to_dict(r) for r in db.execute("SELECT * FROM child_profiles ORDER BY id ASC").fetchall()]


def write_profile_log(db, profile_id, op_type, field_name="", old_value="",
                      new_value="", tag="", operator="", operated_at=None):
    """档案操作留痕：建档 / 编辑 / 合并 / 命中确认，全部可追溯"""
    db.execute(
        """
        INSERT INTO profile_logs (
            profile_id, operation_type, field_name, old_value, new_value, tag, operator, operated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (profile_id, op_type, field_name, str(old_value), str(new_value), tag, operator, operated_at or now_str()),
    )


def get_profile_logs(db, profile_id):
    logs = db.execute(
        "SELECT * FROM profile_logs WHERE profile_id = ? ORDER BY id ASC", (profile_id,)
    ).fetchall()
    return [dict(l) for l in logs]


def match_child_profiles(payload, profiles):
    """
    任务3 核心规则：新报名与历史信息库自动比对，命中则提示机构人员核对。
    只提示、不自动合并、不自动改动任何数据。

    命中规则（任一命中即提示）：
    - 同手机号：手机号相同且非空（同一家庭/联系人，强信号）
    - 同真名同家长：真名+家长姓名都相同（同一儿童，强信号）
    - 同小名同家长：小名+家长姓名都相同（真名缺失时的辅助判断）

    返回 [{profile_id, reasons, profile}]，强命中（原因多/同手机号）排前面。
    """
    real = (payload.get("student_real_name") or "").strip()
    nick = (payload.get("student_nickname") or "").strip()
    parent = (payload.get("parent_name") or "").strip()
    phone = (payload.get("phone") or "").strip()

    hits = []
    for p in profiles:
        if p.get("merged_into"):
            continue  # 已并入其他档案的不再参与命中
        reasons = []
        p_phone = (p.get("phone") or "").strip()
        p_real = (p.get("student_real_name") or "").strip()
        p_nick = (p.get("student_nickname") or "").strip()
        p_parent = (p.get("parent_name") or "").strip()
        if phone and p_phone and phone == p_phone:
            reasons.append("同手机号")
        if real and p_real and parent and p_parent and real == p_real and parent == p_parent:
            reasons.append("同真名同家长")
        if nick and p_nick and parent and p_parent and nick == p_nick and parent == p_parent:
            reasons.append("同小名同家长")
        if reasons:
            hits.append({"profile_id": p["id"], "reasons": reasons, "profile": p})

    hits.sort(key=lambda h: (-len(h["reasons"]), 0 if "同手机号" in h["reasons"] else 1, h["profile_id"]))
    return hits


def enrollment_history_matches(db, enrollment_dict):
    """报名记录当前的历史档案命中（实时查询）"""
    return match_child_profiles(enrollment_dict, get_all_profiles(db))


def apply_history_confirm(db, profile_id, enrollment_dict, operator):
    """
    人工确认「是同一儿童」后，把该报名沉淀进历史信息库：
    - profile_id > 0：更新已有档案——空字段补全；非空但与最新报名不一致的字段以最新报名为准（旧值留痕）
      报名次数 +1，关联报名 id 追加
    - profile_id = 0：以该报名新建档案
    返回（profile_id, 变更日志条数）
    """
    now = now_str()

    def pick(field):
        return (enrollment_dict.get(field) or "").strip()

    if profile_id and profile_id > 0:
        row = db.execute("SELECT * FROM child_profiles WHERE id = ?", (profile_id,)).fetchone()
        if not row:
            raise ValueError(f"历史档案 #{profile_id} 不存在")
        profile = profile_to_dict(row)
        updates = {}
        # 空字段补全 / 过期字段以最新报名为准
        for field in ["student_real_name", "student_nickname", "parent_name", "phone", "relation"]:
            new_val = pick(field)
            old_val = (profile.get(field) or "").strip()
            if new_val and new_val != old_val:
                updates[field] = new_val
                write_profile_log(db, profile_id, "profile_fill" if not old_val else "profile_edit",
                                  field_name=field, old_value=old_val, new_value=new_val,
                                  tag="以最新报名为准" if old_val else "报名补全", operator=operator)
        ids = list(profile.get("enrollment_ids") or [])
        eid = enrollment_dict.get("id")
        if eid and eid not in ids:
            ids.append(eid)
        updates["enrollment_count"] = profile.get("enrollment_count", 0) + 1
        updates["enrollment_ids"] = ",".join(str(i) for i in ids)
        updates["history_status"] = "confirmed"
        updates["confirmed_by"] = operator
        updates["confirmed_at"] = now
        updates["updated_at"] = now
        sets = ", ".join(f"{k} = ?" for k in updates)
        db.execute(f"UPDATE child_profiles SET {sets} WHERE id = ?", list(updates.values()) + [profile_id])
        write_profile_log(db, profile_id, "hit_confirm",
                          new_value=f"报名#{eid or '预览'} 确认同一儿童，累计报名 {updates['enrollment_count']} 次",
                          operator=operator, operated_at=now)
        db.commit()
        return profile_id
    else:
        # 新建档案（沉淀）
        cursor = db.execute(
            """
            INSERT INTO child_profiles (
                student_real_name, student_nickname, parent_name, phone, relation,
                enrollment_count, enrollment_ids, history_status, confirmed_by, confirmed_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pick("student_real_name"), pick("student_nickname"), pick("parent_name"),
                pick("phone"), pick("relation"),
                1, str(enrollment_dict.get("id") or ""), "confirmed", operator, now, now, now,
            ),
        )
        new_id = cursor.lastrowid
        write_profile_log(db, new_id, "create",
                          new_value=f"由报名#{enrollment_dict.get('id') or '预览'} 沉淀建档", operator=operator, operated_at=now)
        db.commit()
        return new_id


def detect_anomalies(payload, existing_rows):
    """
    异常检测逻辑（与交付物文档对齐）
    返回异常标签列表。
    - 已合并（status='已合并'）与已标记「非重复」的记录不作为重复对照
    - 记录自身被标记「非重复」时，不再输出重复标签
    """
    tags = []
    real_name = (payload.get("student_real_name") or "").strip()
    nickname = (payload.get("student_nickname") or "").strip()
    parent_name = (payload.get("parent_name") or "").strip()
    phone = (payload.get("phone") or "").strip()
    source_type = payload.get("source_type")

    # 1. 姓名异常：真名缺失（只有小名）
    if not real_name:
        tags.append("姓名异常（仅小名）")

    # 2. 信息缺失：必填字段为空
    missing = []
    if not parent_name:
        missing.append("缺家长姓名")
    if not phone:
        missing.append("缺手机号")
    if not real_name:
        missing.append("缺学生真名")
    if missing:
        tags.append(f"信息缺失({','.join(missing)})")

    # 3. 待补全：群接龙字段极简
    if source_type == "group_signup":
        if not nickname or not payload.get("relation"):
            tags.append("待补全（群接龙字段极简）")

    # 4. 重复报名：同手机号 或 同真名同家长
    #    已合并、已标记「非重复」的记录不作为对照
    for e in existing_rows:
        if e.get("status") == "已合并" or e.get("not_duplicate"):
            continue
        if phone and e.get("phone") == phone:
            tags.append("重复报名（同手机号）")
            break
        if (
            real_name
            and e.get("student_real_name") == real_name
            and parent_name
            and e.get("parent_name") == parent_name
        ):
            tags.append("重复报名（同真名同家长）")
            break

    # 该记录被人工确认「非重复」后，不再输出重复标签
    if payload.get("not_duplicate"):
        tags = [t for t in tags if not is_duplicate_tag(t)]

    return tags


def recalculate_preview_record(record, all_records, operator="系统", action=None):
    """
    重新计算单条 preview 记录的系统异常标签。
    action: 触发本次重算的动作（edit / add / confirm_ok …），用于判断是否撤销「确认无误」
    返回更新后的 record（含 system_tags / anomaly_tags / status / operation_logs）。

    语义约定：
    - system_tags 始终如实反映系统当前检测到的异常
    - confirmed_ok=True 表示社工认定系统标红是误报 → 展示层面不再标红
    - manual_tags 由社工自己负责，不受「确认无误」影响
    - 已确认后又被编辑出新的系统异常 → 自动撤销确认，不让确认掩盖新问题
    """
    # 用于重复检测的现有记录：数据库中已有（排除已合并）+ preview 中其他记录（排除自己）
    db = get_db()
    db_existing = [
        row_to_dict(r) for r in
        db.execute("SELECT * FROM enrollments WHERE status != '已合并'").fetchall()
    ]
    others = [r for r in all_records if r is not record]
    combined_for_detect = db_existing + others

    # 任务3.5：历史命中 → 自动补全缺失字段（只补空、不覆盖、不合并；once-only，编辑后不反复回填）
    if record.get("history_status") in ("confirmed", "ignored"):
        record["history_matches"] = []
    else:
        matches = match_child_profiles(record, get_all_profiles(db))
        record["history_matches"] = matches
        if matches and not record.get("history_autofilled"):
            record["history_autofilled"] = True
            filled = apply_history_autofill(record, matches[0]["profile"])
            if filled:
                log_autofill(record, record, filled, operator)

    new_system_tags = detect_anomalies(record, combined_for_detect)
    record["system_tags"] = new_system_tags
    manual_tags = record.get("manual_tags", []) or []

    if action in ("edit", "add") and record.get("confirmed_ok") and new_system_tags:
        record["confirmed_ok"] = False
        add_operation_log(record, {
            "type": "confirm_revoke",
            "message": f"编辑后出现新的系统异常（{'、'.join(new_system_tags)}），自动撤销「确认无误」",
            "operator": operator,
        })

    visible_system = [] if record.get("confirmed_ok") else new_system_tags
    combined_tags = list(dict.fromkeys(visible_system + manual_tags))
    record["anomaly_tags"] = combined_tags
    record["status"] = "待处理" if combined_tags else "已确认"
    return record


def add_operation_log(record, log):
    """给 preview record 添加操作日志"""
    record.setdefault("operation_logs", []).append({
        **log,
        "time": log.get("time") or now_str(),
    })


def recalc_all_preview_records(records, operator="系统", action=None):
    """
    整体重算 preview 中所有记录的异常标签。
    用于重复处理类操作（标记非重复 / 取消非重复 / 预览合并）：
    这些操作会改变"重复对照关系"，因此组内所有行的重复标签都要联动刷新，
    而不是只刷新被操作的那一行。
    action 传非 edit/add 值（如 "mark" / "merge"），不会触发「确认无误」的自动撤销。
    """
    for r in list(records):
        recalculate_preview_record(r, records, operator=operator, action=action)


def save_preview(preview_id, records, import_batch_id, source_user):
    """写入/更新导入预览（持久化到 SQLite）"""
    db = get_db()
    now = now_str()
    db.execute(
        """
        INSERT INTO import_previews (preview_id, import_batch_id, source_user, records, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(preview_id) DO UPDATE SET
            records = excluded.records,
            updated_at = excluded.updated_at
        """,
        (preview_id, import_batch_id, source_user, json.dumps(records, ensure_ascii=False), now, now),
    )
    db.commit()


def load_preview(preview_id):
    """读取导入预览"""
    db = get_db()
    row = db.execute("SELECT * FROM import_previews WHERE preview_id = ?", (preview_id,)).fetchone()
    if not row:
        return None
    try:
        records = json.loads(row["records"])
    except Exception:
        return None
    return {
        "records": records,
        "import_batch_id": row["import_batch_id"],
        "source_user": row["source_user"],
    }


def drop_preview(preview_id):
    """删除导入预览"""
    db = get_db()
    db.execute("DELETE FROM import_previews WHERE preview_id = ?", (preview_id,))
    db.commit()


# --------------------------- API ---------------------------

@app.route("/api/filters", methods=["GET"])
@require_perm("manage_enrollments")
def get_filters():
    """返回筛选枚举值"""
    return jsonify({
        "source_types": [{"value": v, "label": SOURCE_LABELS[v]} for v in SOURCE_TYPES],
        "status": STATUS_TYPES,
        "anomaly_tags": [
            "无",
            "姓名异常（仅小名）",
            "信息缺失(缺家长姓名)",
            "信息缺失(缺手机号)",
            "信息缺失(缺学生真名)",
            "待补全（群接龙字段极简）",
            "重复报名（同手机号）",
            "重复报名（同真名同家长）",
        ],
        "manual_tag_presets": MANUAL_TAG_PRESETS,
    })


# --------------------------- 任务3.5：活动管理 API ---------------------------

ACTIVITY_EDITABLE_FIELDS = ["name", "activity_time", "location", "deadline", "note"]


@app.route("/api/activities", methods=["GET"])
@require_login_api
def list_activities():
    """活动列表：?status=进行中|已结束 筛选；附带每活动的报名数（含已合并，归档口径）"""
    db = get_db()
    status = request.args.get("status", "").strip()

    where_sql = "WHERE status = ?" if status else ""
    params = [status] if status else []
    rows = db.execute(f"SELECT * FROM activities {where_sql} ORDER BY id DESC", params).fetchall()

    counts = {
        r["activity_id"]: r["c"]
        for r in db.execute(
            "SELECT activity_id, COUNT(*) AS c FROM enrollments WHERE activity_id > 0 GROUP BY activity_id"
        ).fetchall()
    }
    data = [activity_to_dict(r, counts.get(r["id"], 0)) for r in rows]
    return jsonify({"data": data, "total": len(data)})


@app.route("/api/activities", methods=["POST"])
@require_perm("manage_activities")
def create_activity():
    """新建活动：名称必填，其余可选（capacity=0 表示不限）"""
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "活动名称不能为空"}), 400

    try:
        capacity = int(data.get("capacity") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "招募人数必须为数字"}), 400
    if capacity < 0:
        return jsonify({"error": "招募人数不能为负数"}), 400

    status = (data.get("status") or "进行中").strip()
    if status not in ACTIVITY_STATUS_TYPES:
        return jsonify({"error": f"活动状态非法: {status}"}), 400

    # 服务提示启用维度（任务5）：勾选后志愿者端才会看到对应维度的提示
    tip_fields = data.get("tip_fields")
    tips_json = "[]"
    if tip_fields is not None:
        if not isinstance(tip_fields, list) or any(t not in SERVICE_TIP_FIELDS for t in tip_fields):
            return jsonify({"error": f"服务提示维度非法，仅支持：{'、'.join(SERVICE_TIP_FIELDS)}"}), 400
        tips_json = json.dumps([t for t in SERVICE_TIP_FIELDS if t in tip_fields], ensure_ascii=False)

    db = get_db()
    now = now_str()
    cursor = db.execute(
        """
        INSERT INTO activities (name, activity_time, location, capacity, deadline, status, note, tip_fields, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            (data.get("activity_time") or "").strip(),
            (data.get("location") or "").strip(),
            capacity,
            (data.get("deadline") or "").strip(),
            status,
            (data.get("note") or "").strip(),
            tips_json,
            now, now,
        ),
    )
    db.commit()
    activity = get_activity(db, cursor.lastrowid)
    return jsonify({"data": activity_to_dict(activity, 0), "message": f"活动「{name}」已创建"}), 201


@app.route("/api/activities/<int:activity_id>", methods=["GET"])
@require_perm("manage_enrollments")
def get_activity_detail(activity_id):
    """活动详情 + 该活动下的报名列表（按活动归档查看）"""
    db = get_db()
    activity = get_activity(db, activity_id)
    if not activity:
        return jsonify({"error": "活动不存在"}), 404

    rows = db.execute(
        "SELECT * FROM enrollments WHERE activity_id = ? ORDER BY source_time DESC, id DESC",
        (activity_id,),
    ).fetchall()
    enrollments = [row_to_dict(r) for r in rows]

    counts = {
        r["source_type"]: r["c"]
        for r in db.execute(
            "SELECT source_type, COUNT(*) AS c FROM enrollments WHERE activity_id = ? GROUP BY source_type",
            (activity_id,),
        ).fetchall()
    }
    by_source = {SOURCE_LABELS.get(k, k): v for k, v in counts.items()}

    # 任务4：签到结果统计（到场/未到场/临时到场/未记录）
    att_counts = {s: 0 for s in ATTENDANCE_STATUSES}
    att_counts["未记录"] = 0
    for r in db.execute(
        "SELECT attendance_status, COUNT(*) AS c FROM enrollments WHERE activity_id = ? GROUP BY attendance_status",
        (activity_id,),
    ).fetchall():
        key = r["attendance_status"] or "未记录"
        att_counts[key] = att_counts.get(key, 0) + r["c"]

    data = activity_to_dict(activity, len(enrollments))
    data["by_source"] = by_source
    data["attendance_counts"] = att_counts
    data["enrollments"] = enrollments
    return jsonify({"data": data})


@app.route("/api/activities/<int:activity_id>", methods=["PUT"])
@require_perm("manage_activities")
def update_activity(activity_id):
    """
    编辑活动 / 关闭活动：
    - name/activity_time/location/capacity/deadline/note 常规编辑
    - status 只允许 进行中/已结束（关闭 = status 置「已结束」，关闭后不能再接入报名）
    """
    data = request.get_json(force=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    if not row:
        return jsonify({"error": "活动不存在"}), 404

    updates = {}
    for f in ACTIVITY_EDITABLE_FIELDS:
        if f in data:
            val = (data.get(f) or "").strip()
            if f == "name" and not val:
                return jsonify({"error": "活动名称不能为空"}), 400
            updates[f] = val
    if "capacity" in data:
        try:
            capacity = int(data.get("capacity") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "招募人数必须为数字"}), 400
        if capacity < 0:
            return jsonify({"error": "招募人数不能为负数"}), 400
        updates["capacity"] = capacity
    if "status" in data:
        status = (data.get("status") or "").strip()
        if status not in ACTIVITY_STATUS_TYPES:
            return jsonify({"error": f"活动状态非法: {status}"}), 400
        updates["status"] = status
    if "tip_fields" in data:
        # 任务5：活动服务提示字段配置（志愿者端只展示这里勾选的维度）
        tip_fields = data.get("tip_fields")
        if not isinstance(tip_fields, list) or any(t not in SERVICE_TIP_FIELDS for t in tip_fields):
            return jsonify({"error": f"服务提示字段非法，仅支持：{'、'.join(SERVICE_TIP_FIELDS)}"}), 400
        updates["tip_fields"] = json.dumps(tip_fields, ensure_ascii=False)

    if not updates:
        return jsonify({"error": "无可更新字段"}), 400

    now = now_str()
    updates["updated_at"] = now
    sets = ", ".join(f"{k} = ?" for k in updates)
    db.execute(f"UPDATE activities SET {sets} WHERE id = ?", list(updates.values()) + [activity_id])
    db.commit()

    activity = get_activity(db, activity_id)
    if "tip_fields" in updates:
        activity["tip_fields"] = parse_tags(activity.get("tip_fields"))
    message = f"活动「{activity['name']}」已关闭，不再接受新报名" if activity["status"] == "已结束" and row["status"] == "进行中" else "活动已更新"
    return jsonify({"data": activity, "message": message})


@app.route("/api/enrollments", methods=["POST"])
@require_perm("manage_enrollments")
def create_enrollment():
    """接收报名数据，异常检测后入库。支持单条或批量"""
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "请求体为空"}), 400

    items = data if isinstance(data, list) else [data]
    db = get_db()

    created = []
    for item in items:
        # 参数校验
        source_type = item.get("source_type")
        if source_type not in SOURCE_TYPES:
            return jsonify({"error": f"source_type 非法: {source_type}"}), 400

        # 任务3.5：每条报名必须归属一个「进行中」的活动（小程序/老师代报/电话私聊等直接接入同规则）
        activity, act_err = check_activity_for_enrollment(db, item.get("activity_id"))
        if act_err:
            return jsonify({"error": act_err}), 400
        item["activity_id"] = activity["id"]

        # 任务3.5：命中历史信息库 → 自动补全缺失字段（只补空、不覆盖、不自动合并），补全后再做异常检测
        profiles = get_all_profiles(db)
        pre_matches = match_child_profiles(item, profiles)
        autofilled = []
        if pre_matches:
            autofilled = apply_history_autofill(item, pre_matches[0]["profile"])

        # 查询现有记录用于重复检测
        existing = [row_to_dict(r) for r in db.execute("SELECT * FROM enrollments").fetchall()]

        # 异常检测：如果客户端没传 anomaly_tags，则自动检测
        anomaly_tags = item.get("anomaly_tags")
        if anomaly_tags is None:
            anomaly_tags = detect_anomalies(item, existing)
        elif isinstance(anomaly_tags, str):
            anomaly_tags = json.loads(anomaly_tags)

        # 状态：客户端没传则默认"待处理"
        status = item.get("status", "待处理")
        if status not in STATUS_TYPES:
            status = "待处理"

        # 人工兜底结果字段（兼容预览阶段带过来的数据）
        system_tags = item.get("system_tags")
        manual_tags = item.get("manual_tags")
        if system_tags is None:
            system_tags = anomaly_tags
        if manual_tags is None:
            manual_tags = []
        if isinstance(system_tags, str):
            system_tags = parse_tags(system_tags)
        if isinstance(manual_tags, str):
            manual_tags = parse_tags(manual_tags)
        confirmed_ok = 1 if item.get("confirmed_ok") else 0

        now = now_str()
        cursor = db.execute(
            """
            INSERT INTO enrollments (
                student_real_name, student_nickname, parent_name, phone,
                id_card, emergency_contact, relation, source_type, source_user,
                source_time, import_batch_id, anomaly_tags, system_tags,
                manual_tags, confirmed_ok, status, note, activity_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.get("student_real_name", "").strip(),
                item.get("student_nickname", "").strip(),
                item.get("parent_name", "").strip(),
                item.get("phone", "").strip(),
                item.get("id_card", "").strip(),
                item.get("emergency_contact", "").strip(),
                item.get("relation", "").strip(),
                source_type,
                item.get("source_user", ""),
                item.get("source_time") or now,
                item.get("import_batch_id"),
                json.dumps(anomaly_tags, ensure_ascii=False),
                json.dumps(system_tags, ensure_ascii=False),
                json.dumps(manual_tags, ensure_ascii=False),
                confirmed_ok,
                status,
                item.get("note", ""),
                item.get("activity_id", 0),
                now,
                now,
            ),
        )
        db.commit()

        new_row = db.execute("SELECT * FROM enrollments WHERE id = ?", (cursor.lastrowid,)).fetchone()
        enr = row_to_dict(new_row)
        # 任务3.5：自动补全留痕（每字段一条，注明来源档案，供人工复核）
        if autofilled:
            log_autofill(db, enr, autofilled, "系统（历史档案自动补全）",
                         import_batch_id=item.get("import_batch_id"))
            db.commit()
        created.append(enr)

    # 任务3：新报名自动与历史信息库比对，命中则提示机构人员核对（只提示、不自动合并）
    db.commit()
    response_created = []
    hit_any = False
    for item in created:
        matches = enrollment_history_matches(db, item)
        if matches:
            hit_any = True
        item["history_matches"] = matches
        response_created.append(item)

    result = {"data": response_created}
    if hit_any:
        result["message"] = "注意：有报名命中历史信息库，请到详情页人工核对后再确认"
    return jsonify(result), 201


@app.route("/api/enrollments", methods=["GET"])
@require_perm("manage_enrollments")
def list_enrollments():
    """统一报名列表，支持按来源/异常标签/状态/活动筛选 + 关键词搜索"""
    source_type = request.args.get("source_type")
    anomaly_tag = request.args.get("anomaly_tag")
    status = request.args.get("status")
    keyword = request.args.get("keyword", "").strip()
    import_batch_id = request.args.get("import_batch_id", "").strip()
    activity_id = request.args.get("activity_id", "").strip()
    # 任务4：签到状态筛选（到场/未到场/临时到场；attendance_status=none 表示未记录）
    attendance_status = request.args.get("attendance_status", "").strip()
    page = request.args.get("page", "1")
    per_page = request.args.get("per_page", "50")

    try:
        page = max(1, int(page))
        per_page = max(1, min(100, int(per_page)))
    except ValueError:
        page, per_page = 1, 50

    where_clauses = []
    params = []

    if source_type:
        where_clauses.append("source_type = ?")
        params.append(source_type)
    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if anomaly_tag:
        # anomaly_tags 存 JSON 字符串，用 LIKE 简单匹配
        where_clauses.append("anomaly_tags LIKE ?")
        params.append(f"%{anomaly_tag}%")
    if import_batch_id:
        where_clauses.append("import_batch_id LIKE ?")
        params.append(f"%{import_batch_id}%")
    if activity_id:
        # 任务3.5：按活动筛选（activity_id=0 视为「未归属」，一般不会出现）
        if activity_id == "0":
            where_clauses.append("(activity_id IS NULL OR activity_id = 0)")
        else:
            where_clauses.append("activity_id = ?")
            params.append(int(activity_id))
    if attendance_status == "none":
        where_clauses.append("(attendance_status IS NULL OR attendance_status = '')")
    elif attendance_status:
        where_clauses.append("attendance_status = ?")
        params.append(attendance_status)
    if keyword:
        where_clauses.append(
            "(student_real_name LIKE ? OR student_nickname LIKE ? OR parent_name LIKE ? OR phone LIKE ?)"
        )
        like = f"%{keyword}%"
        params.extend([like, like, like, like])

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    db = get_db()

    # 总数
    total = db.execute(f"SELECT COUNT(*) FROM enrollments {where_sql}", params).fetchone()[0]

    # 分页列表
    offset = (page - 1) * per_page
    rows = db.execute(
        f"SELECT * FROM enrollments {where_sql} ORDER BY source_time DESC, id DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    data = [row_to_dict(r) for r in rows]

    # 任务3.5：附带所属活动名称（列表展示「活动」列）
    activity_names = get_activity_name_map(db)
    for item in data:
        item["activity_name"] = activity_names.get(item.get("activity_id") or 0, "")

    # 任务3：每条报名附带实时历史命中信息（行内「待核对/已建档」标记用）
    profiles = get_all_profiles(db)
    for item in data:
        if item.get("history_status") == "confirmed":
            item["history_pending"] = False
        elif item.get("history_status") == "ignored":
            item["history_pending"] = False
        else:
            hits = match_child_profiles(item, profiles)
            item["history_pending"] = bool(hits)
            item["history_match_reasons"] = hits[0]["reasons"] if hits else []

    # 任务5：敏感字段分级——无 view_sensitive 权限的响应一律置为占位提示（前后端双重校验）
    for item in data:
        sanitize_enrollment(item, g.get("current_user"))

    return jsonify({
        "data": data,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
        },
    })


@app.route("/api/enrollments/<int:enrollment_id>", methods=["GET"])
@require_perm("manage_enrollments")
def get_enrollment(enrollment_id):
    """查看单条报名详情，含来源明细"""
    db = get_db()
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not row:
        return jsonify({"error": "记录不存在"}), 404
    data = row_to_dict(row)
    # 任务3.5：附带所属活动名称与信息
    activity = get_activity(db, data.get("activity_id") or 0)
    data["activity_name"] = activity["name"] if activity else ""
    data["activity"] = activity
    # 任务5：敏感字段分级（前后端双重校验）
    sanitize_enrollment(data, g.get("current_user"))
    return jsonify({"data": data})


# --------------------------- 任务4：现场签到差异回传 ---------------------------

def _attendance_response(db, enrollment_id, message):
    """签到回传后的统一响应（重新取行 + 附活动名）"""
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    d = row_to_dict(row)
    activity = get_activity(db, d.get("activity_id") or 0)
    d["activity_name"] = activity["name"] if activity else ""
    return jsonify({"data": d, "message": message})


@app.route("/api/enrollments/<int:enrollment_id>/attendance", methods=["POST"])
@require_perm("attendance_callback")
def set_attendance(enrollment_id):
    """
    任务4：单条签到结果回传（现场差异标记）。
    status：到场 / 未到场 / 临时到场；传 "" 且 clear=true 清除签到记录。
    只传 note（status 为空且非 clear）= 仅保存现场备注，不改动已记录的签到状态。
    每次回传写留痕（operation_logs type=attendance），备案可追溯。
    """
    data = request.get_json(force=True) or {}
    status = (data.get("status") or "").strip()
    note = (data.get("note") or "").strip()
    operator = (data.get("operator") or "现场工作人员").strip()

    if status and status not in ATTENDANCE_STATUSES:
        return jsonify({"error": f"签到状态非法，仅支持：{'、'.join(ATTENDANCE_STATUSES)}（或空字符串清除）"}), 400

    db = get_db()
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not row:
        return jsonify({"error": "记录不存在"}), 404
    enr = row_to_dict(row)

    now = now_str()
    if not status and not data.get("clear"):
        # 仅保存现场备注：保留原签到状态
        db.execute(
            "UPDATE enrollments SET attendance_note = ? WHERE id = ?",
            (note, enrollment_id),
        )
        write_log(
            db, enrollment_id, enr.get("import_batch_id", ""), "attendance",
            field_name="attendance_note",
            old_value=enr.get("attendance_note", "") or "（空）",
            new_value=note or "（空）",
            tag="", operator=operator,
        )
        db.commit()
        return _attendance_response(db, enrollment_id, "现场备注已保存")

    db.execute(
        "UPDATE enrollments SET attendance_status = ?, attendance_note = ?, attendance_time = ?, attendance_operator = ? WHERE id = ?",
        (status, note, now if status else "", operator if status else "", enrollment_id),
    )
    write_log(
        db, enrollment_id, enr.get("import_batch_id", ""), "attendance",
        field_name="attendance_status",
        old_value=enr.get("attendance_status", "") or "（未记录）",
        new_value=status or "（清除）",
        tag=note, operator=operator,
    )
    db.commit()
    return _attendance_response(db, enrollment_id, f"已记录签到结果：{status}" if status else "已清除签到记录")


@app.route("/api/enrollments/attendance/batch", methods=["POST"])
@require_perm("attendance_callback")
def batch_attendance():
    """
    任务4：纸质签到表誊录后批量回传。
    body: {records: [{id, status, note?}], operator?}
    - status 非法或 id 不存在 → 记入 failed，其余继续（部分成功）
    - 返回 {updated, failed, data:[更新后记录]}
    """
    data = request.get_json(force=True) or {}
    items = data.get("records") or []
    operator = (data.get("operator") or "现场工作人员").strip()
    if not items:
        return jsonify({"error": "records 不能为空"}), 400

    db = get_db()
    updated, failed, results = 0, [], []
    now = now_str()
    for item in items:
        try:
            eid = int(item.get("id") or 0)
        except (TypeError, ValueError):
            failed.append({"id": item.get("id"), "error": "id 非法"})
            continue
        status = (item.get("status") or "").strip()
        note = (item.get("note") or "").strip()
        if status not in ATTENDANCE_STATUSES:
            failed.append({"id": eid, "error": f"签到状态非法（{status or '空'}），仅支持：{'、'.join(ATTENDANCE_STATUSES)}"})
            continue
        row = db.execute("SELECT * FROM enrollments WHERE id = ?", (eid,)).fetchone()
        if not row:
            failed.append({"id": eid, "error": "记录不存在"})
            continue
        enr = row_to_dict(row)
        db.execute(
            "UPDATE enrollments SET attendance_status = ?, attendance_note = ?, attendance_time = ?, attendance_operator = ? WHERE id = ?",
            (status, note, now, operator, eid),
        )
        write_log(
            db, eid, enr.get("import_batch_id", ""), "attendance",
            field_name="attendance_status",
            old_value=enr.get("attendance_status", "") or "（未记录）",
            new_value=status, tag=note, operator=operator,
        )
        updated += 1
        results.append(eid)
    db.commit()

    out = []
    for eid in results:
        r = db.execute("SELECT * FROM enrollments WHERE id = ?", (eid,)).fetchone()
        d = row_to_dict(r)
        d["activity_name"] = get_activity_name_map(db).get(d.get("activity_id") or 0, "")
        out.append(d)
    msg = f"批量回传完成：成功 {updated} 条" + (f"，失败 {len(failed)} 条" if failed else "")
    return jsonify({"data": {"updated": updated, "failed": failed, "records": out}, "message": msg})


@app.route("/api/activities/<int:activity_id>/attendance", methods=["GET"])
@require_perm("attendance_callback")
def activity_attendance(activity_id):
    """任务4：某活动签到结果名单（现场签到页 / 誊录核对用）"""
    db = get_db()
    activity = get_activity(db, activity_id)
    if not activity:
        return jsonify({"error": "活动不存在"}), 404
    rows = db.execute(
        "SELECT * FROM enrollments WHERE activity_id = ? AND status != '已合并' ORDER BY student_real_name = '', student_real_name, id",
        (activity_id,),
    ).fetchall()
    data = [row_to_dict(r) for r in rows]
    counts = {s: 0 for s in ATTENDANCE_STATUSES}
    counts["未记录"] = 0
    for d in data:
        key = d.get("attendance_status") or "未记录"
        counts[key] = counts.get(key, 0) + 1
    return jsonify({
        "data": data,
        "activity": activity_to_dict(activity, len(data)),
        "attendance_counts": counts,
        "total": len(data),
    })


@app.route("/api/enrollments/<int:enrollment_id>/logs", methods=["GET"])
@require_perm("manage_enrollments")
def get_enrollment_logs(enrollment_id):
    """查看单条报名的操作日志"""
    db = get_db()
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not row:
        return jsonify({"error": "记录不存在"}), 404

    logs = db.execute(
        """
        SELECT operation_type, field_name, old_value, new_value, tag, operator, operated_at
        FROM operation_logs
        WHERE enrollment_id = ?
        ORDER BY id ASC
        """,
        (enrollment_id,),
    ).fetchall()
    return jsonify({"data": [dict(l) for l in logs]})


def write_log(db, enrollment_id, import_batch_id, op_type, field_name="",
              old_value="", new_value="", tag="", operator="", operated_at=None):
    """写一条操作留痕"""
    db.execute(
        """
        INSERT INTO operation_logs (
            enrollment_id, import_batch_id, operation_type, field_name,
            old_value, new_value, tag, operator, operated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            enrollment_id, import_batch_id, op_type, field_name,
            str(old_value), str(new_value), tag, operator,
            operated_at or now_str(),
        ),
    )


def recalc_stored_record(db, enrollment_id, operator="社工", action="edit"):
    """
    入库记录被人工编辑后：重算系统异常 → 更新 anomaly_tags / status / confirmed_ok。
    语义与预览阶段一致：
    - system_tags 如实反映当前检测
    - confirmed_ok=True 则展示层面不再标红
    - 编辑后出现新系统异常 → 自动撤销「确认无误」并留痕
    返回更新后的记录 dict。
    """
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    rec = row_to_dict(row)

    others = [
        row_to_dict(r) for r in db.execute(
            "SELECT * FROM enrollments WHERE id != ? AND status != '已合并'",
            (enrollment_id,),
        ).fetchall()
    ]
    old_system_tags = list(rec["system_tags"])
    new_system_tags = detect_anomalies(rec, others)

    if rec["confirmed_ok"]:
        newly_added = [t for t in new_system_tags if t not in old_system_tags]
        if newly_added:
            rec["confirmed_ok"] = 0
            write_log(
                db, enrollment_id, rec.get("import_batch_id"), "confirm_revoke",
                new_value="、".join(newly_added), operator=operator,
            )

    visible_system = [] if rec["confirmed_ok"] else new_system_tags
    anomaly_tags = list(dict.fromkeys(visible_system + rec["manual_tags"]))
    status = "待处理" if anomaly_tags else "已确认"

    db.execute(
        """
        UPDATE enrollments
        SET system_tags = ?, anomaly_tags = ?, status = ?, confirmed_ok = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            json.dumps(new_system_tags, ensure_ascii=False),
            json.dumps(anomaly_tags, ensure_ascii=False),
            status,
            1 if rec["confirmed_ok"] else 0,
            now_str(),
            enrollment_id,
        ),
    )
    db.commit()

    updated = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    return row_to_dict(updated)


@app.route("/api/enrollments/<int:enrollment_id>", methods=["PUT"])
@require_perm("manage_enrollments")
def update_enrollment(enrollment_id):
    """人工修正字段/状态/备注：留痕 + 实时重算异常标签"""
    data = request.get_json(force=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not row:
        return jsonify({"error": "记录不存在"}), 404

    allowed_fields = [
        "student_real_name", "student_nickname", "parent_name", "phone",
        "id_card", "emergency_contact", "relation", "status", "note"
    ]
    updates = {k: data[k] for k in allowed_fields if k in data}
    if not updates:
        return jsonify({"error": "无可更新字段"}), 400

    operator = data.get("operator", "社工")
    now = now_str()

    # 统一列表里的人工修正同样留痕（操作人、时间、修改前值、修改后值）
    old = dict(row)
    changed = False
    for field, new_value in updates.items():
        old_value = old.get(field) or ""
        if str(old_value) == str(new_value):
            continue
        changed = True
        write_log(
            db, enrollment_id, old.get("import_batch_id"), "edit",
            field_name=field, old_value=old_value, new_value=new_value,
            operator=operator, operated_at=now,
        )

    if changed:
        updates["updated_at"] = now
        sets = ", ".join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [enrollment_id]
        db.execute(f"UPDATE enrollments SET {sets} WHERE id = ?", values)
        db.commit()

        # 实时重算异常：补全字段后对应系统标签自动消失
        recalc_stored_record(db, enrollment_id, operator=operator, action="edit")

    updated = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    updated_dict = row_to_dict(updated)
    sanitize_enrollment(updated_dict, g.get("current_user"))
    return jsonify({"data": updated_dict})


# --------------------------- 疑似重复分组 / 合并 / 非重复 ---------------------------

def find_duplicate_groups():
    """
    扫描全库，用连通分量把疑似重复的记录聚组：
    - 同手机号（非空）连边
    - 同「真名+家长姓名」（均非空）连边
    已合并（status='已合并'）与已标记「非重复」的记录不参与成组。
    返回组列表，每组为记录 dict 列表（按 id 升序）。
    """
    db = get_db()
    rows = [
        row_to_dict(r) for r in
        db.execute(
            "SELECT * FROM enrollments WHERE status != '已合并' AND not_duplicate = 0 ORDER BY id ASC"
        ).fetchall()
    ]

    parent = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_phone = {}
    by_name = {}
    for r in rows:
        phone = (r.get("phone") or "").strip()
        real = (r.get("student_real_name") or "").strip()
        pname = (r.get("parent_name") or "").strip()
        if phone:
            if phone in by_phone:
                union(by_phone[phone], r["id"])
            else:
                by_phone[phone] = r["id"]
        if real and pname:
            key = (real, pname)
            if key in by_name:
                union(by_name[key], r["id"])
            else:
                by_name[key] = r["id"]

    groups = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r)

    result = []
    for members in groups.values():
        if len(members) < 2:
            continue
        # 生成重复理由：组内两两比较
        reasons = []
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if (a.get("phone") or "").strip() and a.get("phone") == b.get("phone"):
                    reasons.append("同手机号")
                if (
                    (a.get("student_real_name") or "").strip()
                    and a.get("student_real_name") == b.get("student_real_name")
                    and (a.get("parent_name") or "").strip()
                    and a.get("parent_name") == b.get("parent_name")
                ):
                    reasons.append("同真名同家长")
        result.append({
            "group_key": f"G{members[0]['id']}",
            "reasons": sorted(set(reasons)),
            "records": members,
        })

    result.sort(key=lambda g: g["records"][0]["id"])
    return result


@app.route("/api/duplicates", methods=["GET"])
@require_perm("manage_enrollments")
def list_duplicates():
    """疑似重复分组：同手机号 或 同真名同家长，自动成组返回"""
    groups = find_duplicate_groups()
    total_records = sum(len(g["records"]) for g in groups)
    return jsonify({"groups": groups, "group_count": len(groups), "record_count": total_records})


MERGEABLE_FIELDS = [
    "student_real_name", "student_nickname", "parent_name", "phone",
    "id_card", "emergency_contact", "relation",
]


@app.route("/api/enrollments/merge", methods=["POST"])
@require_perm("manage_enrollments")
def merge_enrollments():
    """
    人工确认同一人 → 合并为一条：
    - primary_id：保留的主记录
    - merge_ids：被合并的记录（status 置为「已合并」，merged_into=主记录 id）
    - 主记录空字段用被合并记录的非空值补齐（逐字段留痕）
    - 备注、人工标签并入主记录
    - 全程留痕，合并后主记录重算异常
    """
    data = request.get_json(force=True) or {}
    primary_id = data.get("primary_id")
    merge_ids = data.get("merge_ids") or []
    operator = data.get("operator", "社工")

    if not primary_id or not merge_ids:
        return jsonify({"error": "缺少 primary_id 或 merge_ids"}), 400
    if primary_id in merge_ids:
        return jsonify({"error": "主记录不能同时是被合并记录"}), 400

    db = get_db()
    now = now_str()

    primary_row = db.execute("SELECT * FROM enrollments WHERE id = ?", (primary_id,)).fetchone()
    if not primary_row:
        return jsonify({"error": f"主记录 {primary_id} 不存在"}), 404
    primary = dict(primary_row)

    merge_rows = []
    for mid in merge_ids:
        r = db.execute("SELECT * FROM enrollments WHERE id = ?", (mid,)).fetchone()
        if not r:
            return jsonify({"error": f"被合并记录 {mid} 不存在"}), 404
        if r["status"] == "已合并":
            return jsonify({"error": f"记录 {mid} 已被合并过"}), 400
        merge_rows.append(dict(r))

    # 1. 主记录空字段用被合并记录的非空值补齐
    for m in merge_rows:
        for field in MERGEABLE_FIELDS:
            cur = (primary.get(field) or "").strip()
            inc = (m.get(field) or "").strip()
            if not cur and inc:
                write_log(
                    db, primary_id, primary.get("import_batch_id"), "merge_fill",
                    field_name=field, old_value="", new_value=inc,
                    tag=f"补自#{m['id']}", operator=operator, operated_at=now,
                )
                primary[field] = inc

    # 2. 备注并入
    notes = [m.get("note") or "" for m in merge_rows if (m.get("note") or "").strip()]
    if notes:
        primary_note = (primary.get("note") or "").strip()
        merged_note = "；".join([primary_note] + notes) if primary_note else "；".join(notes)
        write_log(
            db, primary_id, primary.get("import_batch_id"), "merge_fill",
            field_name="note", old_value=primary_note, new_value=merged_note,
            tag="备注并入", operator=operator, operated_at=now,
        )
        primary["note"] = merged_note

    # 3. 人工标签并入主记录
    primary_manual = parse_tags(primary.get("manual_tags"))
    for m in merge_rows:
        for t in parse_tags(m.get("manual_tags")):
            if t not in primary_manual:
                primary_manual.append(t)
                write_log(
                    db, primary_id, primary.get("import_batch_id"), "manual_tag",
                    tag=t, operator=f"{operator}（合并自#{m['id']}）", operated_at=now,
                )

    updates = {f: primary.get(f) for f in MERGEABLE_FIELDS}
    updates["note"] = primary.get("note")
    updates["manual_tags"] = json.dumps(primary_manual, ensure_ascii=False)
    updates["updated_at"] = now
    sets = ", ".join([f"{k} = ?" for k in updates.keys()])
    db.execute(
        f"UPDATE enrollments SET {sets} WHERE id = ?",
        list(updates.values()) + [primary_id],
    )

    # 4. 被合并记录：状态置「已合并」，merged_into 指向主记录
    for m in merge_rows:
        db.execute(
            "UPDATE enrollments SET status = '已合并', merged_into = ?, updated_at = ? WHERE id = ?",
            (primary_id, now, m["id"]),
        )
        write_log(
            db, m["id"], m.get("import_batch_id"), "merged_into",
            new_value=f"#{primary_id}", operator=operator, operated_at=now,
        )

    # 5. 主记录总留痕
    merged_ids_text = "、".join(f"#{m['id']}" for m in merge_rows)
    write_log(
        db, primary_id, primary.get("import_batch_id"), "merge",
        field_name="merged", old_value=merged_ids_text, new_value=f"#{primary_id}",
        operator=operator, operated_at=now,
    )
    db.commit()

    # 6. 主记录重算异常（被合并记录已不作为对照，重复标签应自动消失）
    updated = recalc_stored_record(db, primary_id, operator=operator, action="merge")

    merged_row = db.execute("SELECT * FROM enrollments WHERE id = ?", (primary_id,)).fetchone()
    return jsonify({
        "data": row_to_dict(merged_row),
        "merged_ids": merge_ids,
        "message": f"已把 {merged_ids_text} 合并进 #{primary_id}",
    })


@app.route("/api/enrollments/<int:enrollment_id>/mark_duplicate", methods=["POST"])
@require_perm("manage_enrollments")
def mark_duplicate(enrollment_id):
    """
    人工判定重复真伪：
    - is_duplicate=false → 标记「非重复」，解除重复标签（重算后不再出现）
    - is_duplicate=true  → 取消「非重复」，恢复系统重复检测
    """
    data = request.get_json(force=True) or {}
    is_duplicate = data.get("is_duplicate")
    operator = data.get("operator", "社工")

    if is_duplicate is None:
        return jsonify({"error": "缺少 is_duplicate"}), 400

    db = get_db()
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not row:
        return jsonify({"error": "记录不存在"}), 404
    if row["status"] == "已合并":
        return jsonify({"error": "该记录已被合并，无法操作"}), 400

    not_duplicate = 0 if is_duplicate else 1
    db.execute(
        "UPDATE enrollments SET not_duplicate = ?, updated_at = ? WHERE id = ?",
        (not_duplicate, now_str(), enrollment_id),
    )
    write_log(
        db, enrollment_id, row["import_batch_id"],
        "mark_not_duplicate" if not_duplicate else "mark_duplicate_again",
        old_value="疑似重复" if not_duplicate else "非重复",
        new_value="非重复（人工确认）" if not_duplicate else "疑似重复",
        operator=operator,
    )
    db.commit()

    updated = recalc_stored_record(db, enrollment_id, operator=operator, action="mark")
    return jsonify({"data": updated})


# --------------------------- 任务3：历史信息库 API ---------------------------

PROFILE_EDITABLE_FIELDS = ["student_real_name", "student_nickname", "parent_name", "phone", "relation", "note"]


@app.route("/api/profiles", methods=["GET"])
@require_perm("history_profiles")
def list_profiles():
    """
    历史信息库档案列表（人工处理后台）
    ?keyword= 搜索真名/小名/家长/手机号；?include_merged=1 含已并入档案
    """
    db = get_db()
    keyword = (request.args.get("keyword") or "").strip()
    include_merged = request.args.get("include_merged") == "1"

    rows = get_all_profiles(db)
    if not include_merged:
        rows = [p for p in rows if not p.get("merged_into")]
    if keyword:
        kw = keyword.lower()
        rows = [
            p for p in rows
            if any(kw in (p.get(f) or "").lower() for f in ["student_real_name", "student_nickname", "parent_name", "phone"])
        ]

    # 附带每条档案最近关联报名的时间，便于核对
    for p in rows:
        p["latest_source_time"] = ""
        if p["enrollment_ids"]:
            q = db.execute(
                f"SELECT MAX(source_time) AS t FROM enrollments WHERE id IN ({','.join('?' * len(p['enrollment_ids']))})",
                p["enrollment_ids"],
            ).fetchone()
            p["latest_source_time"] = q["t"] if q and q["t"] else ""

    rows.sort(key=lambda x: (-x.get("enrollment_count", 0), x["id"]))
    return jsonify({"data": rows, "total": len(rows)})


@app.route("/api/profiles/<int:profile_id>", methods=["GET"])
@require_perm("history_profiles")
def get_profile(profile_id):
    """档案详情：字段 + 关联报名列表 + 档案操作日志"""
    db = get_db()
    row = db.execute("SELECT * FROM child_profiles WHERE id = ?", (profile_id,)).fetchone()
    if not row:
        return jsonify({"error": "档案不存在"}), 404

    profile = profile_to_dict(row)
    related = []
    if profile["enrollment_ids"]:
        q = db.execute(
            f"SELECT * FROM enrollments WHERE id IN ({','.join('?' * len(profile['enrollment_ids']))}) ORDER BY source_time DESC",
            profile["enrollment_ids"],
        ).fetchall()
        related = [row_to_dict(r) for r in q]
    profile["related_enrollments"] = related
    profile["logs"] = get_profile_logs(db, profile_id)
    if profile.get("merged_into"):
        profile["merged_target"] = profile["merged_into"]
    return jsonify({"data": profile})


@app.route("/api/profiles", methods=["POST"])
@require_perm("history_profiles")
def create_profile():
    """
    新建档案。两种用法：
    1. 直接传字段：{student_real_name, student_nickname, parent_name, phone, relation, operator}
    2. 从报名沉淀：{enrollment_id, operator} —— 以该报名当前字段建档，报名留痕 history_confirm
    """
    data = request.get_json(force=True) or {}
    operator = data.get("operator", "社工")
    db = get_db()
    now = now_str()

    enrollment_id = data.get("enrollment_id")
    if enrollment_id:
        row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
        if not row:
            return jsonify({"error": "报名记录不存在"}), 404
        enr = row_to_dict(row)
        if enr.get("history_status") == "confirmed":
            return jsonify({"error": "该报名已确认写入历史库，请勿重复操作"}), 400
        new_id = apply_history_confirm(db, 0, enr, operator)
        db.execute(
            "UPDATE enrollments SET history_profile_id = ?, history_status = 'confirmed', updated_at = ? WHERE id = ?",
            (new_id, now, enrollment_id),
        )
        write_log(db, enrollment_id, row["import_batch_id"], "history_confirm",
                  new_value=f"#{new_id}", tag="沉淀建档", operator=operator)
        db.commit()
        return jsonify({"data": profile_to_dict(db.execute("SELECT * FROM child_profiles WHERE id = ?", (new_id,)).fetchone())}), 201

    payload = {f: (data.get(f) or "").strip() for f in PROFILE_EDITABLE_FIELDS}
    if not payload["student_real_name"] and not payload["student_nickname"]:
        return jsonify({"error": "至少填写学生真名或小名"}), 400

    cursor = db.execute(
        """
        INSERT INTO child_profiles (
            student_real_name, student_nickname, parent_name, phone, relation, note,
            enrollment_count, enrollment_ids, history_status, confirmed_by, confirmed_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, '', 'confirmed', ?, ?, ?, ?)
        """,
        (payload["student_real_name"], payload["student_nickname"], payload["parent_name"],
         payload["phone"], payload["relation"], payload["note"], operator, now, now, now),
    )
    new_id = cursor.lastrowid
    write_profile_log(db, new_id, "create", new_value="手动新建档案", operator=operator, operated_at=now)
    db.commit()
    return jsonify({"data": profile_to_dict(db.execute("SELECT * FROM child_profiles WHERE id = ?", (new_id,)).fetchone())}), 201


@app.route("/api/profiles/<int:profile_id>", methods=["PUT"])
@require_perm("history_profiles")
def update_profile(profile_id):
    """编辑档案字段：逐字段留痕（旧值 → 新值）。已并入档案不可直接编辑。"""
    data = request.get_json(force=True) or {}
    operator = data.get("operator", "社工")
    db = get_db()
    now = now_str()

    row = db.execute("SELECT * FROM child_profiles WHERE id = ?", (profile_id,)).fetchone()
    if not row:
        return jsonify({"error": "档案不存在"}), 404
    if row["merged_into"]:
        return jsonify({"error": f"该档案已并入 #{row['merged_into']}，请编辑保留的档案"}), 400

    updates = {k: (data.get(k) or "").strip() for k in PROFILE_EDITABLE_FIELDS if k in data}
    updates = {k: v for k, v in updates.items() if str(row[k] or "") != v}
    if not updates:
        return jsonify({"error": "内容无变化"}), 400

    for field, new_value in updates.items():
        write_profile_log(db, profile_id, "profile_edit", field_name=field,
                          old_value=row[field] or "", new_value=new_value, operator=operator)

    sets = ", ".join(f"{k} = ?" for k in updates)
    db.execute(f"UPDATE child_profiles SET {sets}, updated_at = ? WHERE id = ?",
               list(updates.values()) + [now, profile_id])
    db.commit()

    updated = profile_to_dict(db.execute("SELECT * FROM child_profiles WHERE id = ?", (profile_id,)).fetchone())
    updated["logs"] = get_profile_logs(db, profile_id)
    return jsonify({"data": updated})


@app.route("/api/profiles/merge", methods=["POST"])
@require_perm("history_profiles")
def merge_profiles():
    """
    人工合并档案：确认两条历史档案是同一个儿童。
    - 主档案保留：空字段从被并入档案补全（留痕 profile_fill）
    - 报名次数累加、关联报名 id 取并集
    - 被并入档案置 merged_into=主档案id，留痕保留、不再参与命中
    - 关联报名的 history_profile_id 同步指向主档案
    """
    data = request.get_json(force=True) or {}
    primary_id = data.get("primary_id")
    merge_ids = data.get("merge_ids") or []
    operator = data.get("operator", "社工")
    now = now_str()

    if not primary_id or not merge_ids:
        return jsonify({"error": "缺少 primary_id 或 merge_ids"}), 400
    if primary_id in merge_ids:
        return jsonify({"error": "主档案不能同时是被并入档案"}), 400
    if not data.get("confirm"):
        # 任务5：合并儿童档案为关键操作——前端二次确认 + 后端显式 confirm 参数 + 管理员审计留痕
        return jsonify({"error": "合并儿童档案为关键操作，需二次确认（请求需带 confirm: true）并记录审计"}), 400

    db = get_db()
    primary_row = db.execute("SELECT * FROM child_profiles WHERE id = ?", (primary_id,)).fetchone()
    if not primary_row:
        return jsonify({"error": f"主档案 {primary_id} 不存在"}), 404
    if primary_row["merged_into"]:
        return jsonify({"error": f"主档案 #{primary_id} 已并入 #{primary_row['merged_into']}，请以保留档案为主档案"}), 400
    primary = profile_to_dict(primary_row)

    all_ids = list(primary["enrollment_ids"])
    count = primary.get("enrollment_count", 0)
    updates = {}
    for mid in merge_ids:
        m_row = db.execute("SELECT * FROM child_profiles WHERE id = ?", (mid,)).fetchone()
        if not m_row:
            return jsonify({"error": f"被并入档案 {mid} 不存在"}), 404
        if m_row["merged_into"]:
            return jsonify({"error": f"档案 #{mid} 已并入 #{m_row['merged_into']}，不能重复合并"}), 400
        m = profile_to_dict(m_row)

        for field in ["student_real_name", "student_nickname", "parent_name", "phone", "relation", "note"]:
            old_val = (updates.get(field) if field in updates else primary.get(field)) or ""
            m_val = (m.get(field) or "").strip()
            if m_val and not old_val.strip():
                updates[field] = m_val
                write_profile_log(db, primary_id, "profile_fill", field_name=field,
                                  old_value="", new_value=m_val, tag=f"补自档案#{mid}", operator=operator)
        count += m.get("enrollment_count", 0)
        all_ids.extend(m["enrollment_ids"])

        # 被并入档案：置 merged_into，留痕保留
        write_profile_log(db, mid, "merged_into", new_value=f"#{primary_id}", operator=operator, operated_at=now)
        db.execute("UPDATE child_profiles SET merged_into = ?, updated_at = ? WHERE id = ?", (primary_id, now, mid))
        # 其关联报名同步指向主档案
        if m["enrollment_ids"]:
            q = ",".join("?" * len(m["enrollment_ids"]))
            db.execute(f"UPDATE enrollments SET history_profile_id = ? WHERE id IN ({q})",
                       [primary_id] + m["enrollment_ids"])

    updates["enrollment_count"] = count
    updates["enrollment_ids"] = ",".join(str(i) for i in dict.fromkeys(all_ids))
    updates["history_status"] = "confirmed"
    updates["updated_at"] = now

    sets = ", ".join(f"{k} = ?" for k in updates)
    db.execute(f"UPDATE child_profiles SET {sets} WHERE id = ?", list(updates.values()) + [primary_id])
    write_profile_log(db, primary_id, "merge", field_name="merged",
                      old_value="、".join(f"#{m}" for m in merge_ids), new_value=f"#{primary_id}",
                      operator=operator, operated_at=now)
    db.commit()

    updated = profile_to_dict(db.execute("SELECT * FROM child_profiles WHERE id = ?", (primary_id,)).fetchone())
    # 任务5：关键操作审计（合并儿童档案，管理员可复核）
    log_audit("merge_profile", "profile", primary_id,
              f"合并儿童档案：把 {'、'.join('#' + str(m) for m in merge_ids)} 并入 #{primary_id}"
              f"（{primary.get('student_real_name') or '（真名空）'}），操作人 {operator}", user=g.current_user)
    return jsonify({
        "data": updated,
        "merged_ids": merge_ids,
        "message": f"已把 {'、'.join('#' + str(m) for m in merge_ids)} 并入档案 #{primary_id}",
    })


@app.route("/api/enrollments/<int:enrollment_id>/history_matches", methods=["GET"])
@require_perm("history_profiles")
def enrollment_matches(enrollment_id):
    """报名当前的历史档案命中（实时查询，详情弹窗用）"""
    db = get_db()
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not row:
        return jsonify({"error": "记录不存在"}), 404
    enr = row_to_dict(row)
    matches = enrollment_history_matches(db, enr)
    # 已确认/已忽略的给出明确状态
    return jsonify({
        "data": {
            "history_status": enr.get("history_status") or "",
            "history_profile_id": enr.get("history_profile_id") or 0,
            "matches": matches,
        }
    })


@app.route("/api/enrollments/<int:enrollment_id>/history_confirm", methods=["POST"])
@require_perm("history_profiles")
def history_confirm(enrollment_id):
    """
    人工核对历史命中（只提示、人工拍板，不自动合并）：
    - {is_same: true,  profile_id: N}  → 确认与档案 N 是同一儿童：档案更新（+1/补全/以最新为准），报名留痕
    - {is_same: true,  profile_id: 0}  → 历史库没有 → 以该报名沉淀新档案
    - {is_same: false}                 → 判定不是同一儿童：忽略提示，留痕，不再提示
    """
    data = request.get_json(force=True) or {}
    is_same = data.get("is_same")
    operator = data.get("operator", "社工")
    profile_id = int(data.get("profile_id") or 0)
    now = now_str()

    if is_same is None:
        return jsonify({"error": "缺少 is_same"}), 400

    db = get_db()
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not row:
        return jsonify({"error": "记录不存在"}), 404
    enr = row_to_dict(row)
    if enr.get("status") == "已合并":
        return jsonify({"error": "该记录已被合并，无法操作"}), 400

    if is_same:
        new_pid = apply_history_confirm(db, profile_id, enr, operator)
        db.execute(
            "UPDATE enrollments SET history_profile_id = ?, history_status = 'confirmed', updated_at = ? WHERE id = ?",
            (new_pid, now, enrollment_id),
        )
        write_log(db, enrollment_id, row["import_batch_id"], "history_confirm",
                  new_value=f"#{new_pid}", tag="确认同一儿童" if profile_id else "沉淀建档", operator=operator)
        db.commit()
        updated = row_to_dict(db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone())
        profile = profile_to_dict(db.execute("SELECT * FROM child_profiles WHERE id = ?", (new_pid,)).fetchone())
        return jsonify({
            "data": updated,
            "profile": profile,
            "message": f"已确认同一儿童并写入历史档案 #{new_pid}，下次报名自动比对复用",
        })
    else:
        db.execute(
            "UPDATE enrollments SET history_status = 'ignored', updated_at = ? WHERE id = ?",
            (now, enrollment_id),
        )
        write_log(db, enrollment_id, row["import_batch_id"], "history_ignore",
                  old_value=f"档案#{profile_id}" if profile_id else "",
                  new_value="人工判定不是同一儿童", operator=operator)
        db.commit()
        updated = row_to_dict(db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone())
        return jsonify({"data": updated, "message": "已忽略该历史命中，不再提示（留痕可查）"})


@app.route("/api/enrollments/<int:enrollment_id>/history_autofill", methods=["POST"])
@require_perm("history_profiles")
def history_autofill_endpoint(enrollment_id):
    """
    任务3.5：手动「按档案补全」（已入库报名）。
    用命中的历史档案补全该报名缺失字段（只补空、不覆盖、不自动合并、不改档案）。
    可传 {profile_id: N} 指定档案，默认用最强命中。逐字段留痕 history_autofill。
    """
    data = request.get_json(force=True) or {}
    operator = data.get("operator", "社工")
    db = get_db()

    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not row:
        return jsonify({"error": "记录不存在"}), 404
    if row["status"] == "已合并":
        return jsonify({"error": "该记录已被合并，无法操作"}), 400

    enr = row_to_dict(row)
    matches = match_child_profiles(enr, get_all_profiles(db))
    if not matches:
        return jsonify({"error": "该报名当前没有命中的历史档案，无法按档案补全"}), 400

    profile_id = int(data.get("profile_id") or 0)
    hit = next((m for m in matches if m["profile_id"] == profile_id), None) if profile_id else matches[0]
    if not hit:
        return jsonify({"error": f"该报名未命中档案 #{profile_id}"}), 400

    filled = apply_history_autofill(enr, hit["profile"])
    if not filled:
        return jsonify({"data": enr, "message": "没有可补全的空字段（已有值不会被覆盖）"})

    for item in filled:
        label = AUTOFILL_FIELD_LABELS.get(item["field"], item["field"])
        write_log(db, enrollment_id, enr.get("import_batch_id"), "history_autofill",
                  field_name=item["field"], old_value="", new_value=item["value"],
                  tag=f"来自档案#{item['profile_id']}", operator=operator)

    sets = ", ".join(f"{i['field']} = ?" for i in filled) + ", updated_at = ?"
    values = [i["value"] for i in filled] + [now_str(), enrollment_id]
    db.execute(f"UPDATE enrollments SET {sets} WHERE id = ?", values)
    db.commit()

    # 补全后重算异常（信息缺失标签应消失）
    updated = recalc_stored_record(db, enrollment_id, operator=operator, action="edit")
    labels = "、".join(AUTOFILL_FIELD_LABELS.get(i["field"], i["field"]) for i in filled)
    return jsonify({
        "data": updated,
        "filled": filled,
        "message": f"已按档案 #{hit['profile_id']} 补全：{labels}（请人工复核）",
    })


@app.route("/api/history/pending", methods=["GET"])
@require_perm("history_profiles")
def history_pending():
    """
    人工处理后台：待核对命中队列。
    扫描未核对（history_status=''）且未合并的报名，返回当前有历史命中的记录 + 命中详情。
    """
    db = get_db()
    activity_names = get_activity_name_map(db)
    rows = db.execute(
        "SELECT * FROM enrollments WHERE history_status = '' AND status != '已合并' ORDER BY id DESC"
    ).fetchall()
    pending = []
    for r in rows:
        enr = row_to_dict(r)
        matches = enrollment_history_matches(db, enr)
        if matches:
            enr["activity_name"] = activity_names.get(enr.get("activity_id") or 0, "")
            pending.append({"enrollment": enr, "matches": matches[:3]})
    return jsonify({"data": pending, "total": len(pending)})


# --------------------------- 导出 ---------------------------

def get_export_rows(source_type=None, anomaly_tag=None, status=None, keyword="", activity_id=None, attendance_status=None):
    """获取导出数据（复用筛选逻辑）"""
    where_clauses = []
    params = []

    if source_type:
        where_clauses.append("source_type = ?")
        params.append(source_type)
    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if anomaly_tag:
        where_clauses.append("anomaly_tags LIKE ?")
        params.append(f"%{anomaly_tag}%")
    if activity_id:
        where_clauses.append("activity_id = ?")
        params.append(int(activity_id))
    if attendance_status == "none":
        where_clauses.append("(attendance_status IS NULL OR attendance_status = '')")
    elif attendance_status:
        where_clauses.append("attendance_status = ?")
        params.append(attendance_status)
    if keyword:
        where_clauses.append(
            "(student_real_name LIKE ? OR student_nickname LIKE ? OR parent_name LIKE ? OR phone LIKE ?)"
        )
        like = f"%{keyword}%"
        params.extend([like, like, like, like])

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    db = get_db()
    rows = db.execute(f"SELECT * FROM enrollments {where_sql} ORDER BY source_time DESC", params).fetchall()
    data = [row_to_dict(r) for r in rows]
    activity_names = get_activity_name_map(db)
    for item in data:
        item["activity_name"] = activity_names.get(item.get("activity_id") or 0, "")
    return data


EXPORT_COLUMNS = [
    ("id", "序号"),
    ("student_real_name", "学生真名"),
    ("student_nickname", "学生小名"),
    ("parent_name", "家长姓名"),
    ("phone", "手机号"),
    ("id_card", "身份证"),
    ("emergency_contact", "紧急联系人"),
    ("relation", "与孩子关系"),
    ("activity_name", "所属活动"),
    ("source_type", "来源类型"),
    ("source_user", "来源用户"),
    ("source_time", "来源时间"),
    ("import_batch_id", "导入批次号"),
    ("system_tags", "系统异常标签"),
    ("manual_tags", "人工异常标签"),
    ("confirmed_ok", "已人工确认"),
    ("anomaly_tags", "异常标签（合计）"),
    ("status", "状态"),
    ("attendance_status", "签到状态"),
    ("attendance_note", "现场备注"),
    ("attendance_time", "签到回传时间"),
    ("attendance_operator", "签到记录人"),
    ("note", "备注"),
]


def serialize_export_value(row, col):
    """导出单元格序列化"""
    if col in ("anomaly_tags", "system_tags", "manual_tags"):
        return "；".join(row.get(col) or [])
    if col == "confirmed_ok":
        return "是" if row.get("confirmed_ok") else "否"
    if col == "source_type":
        return SOURCE_LABELS.get(row.get("source_type"), row.get("source_type") or "")
    return row.get(col, "")


@app.route("/api/export/csv", methods=["GET"])
@require_perm("export_data")
def export_csv():
    """导出 CSV"""
    source_type = request.args.get("source_type")
    anomaly_tag = request.args.get("anomaly_tag")
    status = request.args.get("status")
    keyword = request.args.get("keyword", "")
    activity_id = request.args.get("activity_id", "")
    attendance_status = request.args.get("attendance_status", "") or None

    rows = get_export_rows(source_type, anomaly_tag, status, keyword, activity_id=activity_id or None,
                           attendance_status=attendance_status)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([label for _, label in EXPORT_COLUMNS])
    for row in rows:
        writer.writerow([serialize_export_value(row, col) for col, _ in EXPORT_COLUMNS])

    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        mimetype="text/csv; charset=utf-8-sig",
        as_attachment=True,
        download_name="报名列表.csv",
    )


@app.route("/api/export/xlsx", methods=["GET"])
@require_perm("export_data")
def export_xlsx():
    """导出 Excel"""
    from openpyxl import Workbook

    source_type = request.args.get("source_type")
    anomaly_tag = request.args.get("anomaly_tag")
    status = request.args.get("status")
    keyword = request.args.get("keyword", "")
    activity_id = request.args.get("activity_id", "")
    attendance_status = request.args.get("attendance_status", "") or None

    rows = get_export_rows(source_type, anomaly_tag, status, keyword, activity_id=activity_id or None,
                           attendance_status=attendance_status)

    wb = Workbook()
    ws = wb.active
    ws.title = "报名列表"
    ws.append([label for _, label in EXPORT_COLUMNS])
    for row in rows:
        ws.append([serialize_export_value(row, col) for col, _ in EXPORT_COLUMNS])

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="报名列表.xlsx",
    )


# --------------------------- 任务4：纸质签到表 / 台账导出 ---------------------------

SIGNUP_SHEET_COLUMNS = [
    ("idx", "序号"),
    ("student_real_name", "学生真名"),
    ("student_nickname", "学生小名"),
    ("parent_name", "家长姓名"),
    ("phone", "联系电话"),
    ("relation", "与孩子关系"),
    ("attendance", "到场情况"),
    ("blank_note", "现场备注"),
    ("blank_sign", "家长签名"),
]

# 台账 PDF 用精选列（XLSX 台账含全部 EXPORT_COLUMNS，字段与统一列表导出口径一致）
LEDGER_PDF_COLUMNS = [
    ("id", "序号", 32),
    ("student_real_name", "学生真名", 62),
    ("student_nickname", "学生小名", 56),
    ("parent_name", "家长姓名", 62),
    ("phone", "手机号", 80),
    ("relation", "关系", 46),
    ("activity_name", "所属活动", 96),
    ("source_type", "来源", 60),
    ("source_time", "来源时间", 90),
    ("anomaly_tags", "异常标签", 110),
    ("status", "状态", 52),
    ("attendance_status", "签到状态", 56),
    ("attendance_note", "现场备注", 110),
]


def get_signup_sheet_rows(db, activity_id):
    """签到表名单：该活动全部有效报名（排除已合并），按真名排序（空真名排后），同真名按报名顺序"""
    rows = db.execute(
        "SELECT * FROM enrollments WHERE activity_id = ? AND status != '已合并' "
        "ORDER BY student_real_name = '', student_real_name, id",
        (activity_id,),
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def attendance_cell_text(row):
    """到场情况单元格：已回传的☑回显 + 全部选项□（纸质勾选）"""
    st = row.get("attendance_status") or ""
    return "　".join(("☑" if s == st else "□") + s for s in ATTENDANCE_STATUSES)


def _pdf_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))  # 内置 CID 中文字体，不依赖系统字体
    return "STSong-Light"


def _pdf_build(sections, filename):
    """
    任务4 通用 PDF 构建：多段表格（A4 横向，中文字体 STSong-Light，内置 CID 不依赖系统字体）。
    sections: [(标题, meta行列表, 表头, 数据行, 列宽), ...]
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = _pdf_font()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=28, rightMargin=28, topMargin=26, bottomMargin=24,
                            title=filename)
    style_title = ParagraphStyle("t", fontName=font, fontSize=15, alignment=1, spaceAfter=4)
    style_meta = ParagraphStyle("m", fontName=font, fontSize=9.5, alignment=0, leading=13)
    style_cell = ParagraphStyle("c", fontName=font, fontSize=8, leading=10)
    style_head = ParagraphStyle("h", fontName=font, fontSize=8.5, leading=11, textColor=colors.white)

    story = []
    for si, (title, meta_lines, header, data_rows, col_widths) in enumerate(sections):
        if si:
            story.append(PageBreak())
        story.append(Paragraph(title, style_title))
        for line in meta_lines:
            story.append(Paragraph(line, style_meta))
        story.append(Spacer(1, 6))
        table_data = [[Paragraph(str(h), style_head) for h in header]]
        for r in data_rows:
            table_data.append([Paragraph(str(c), style_cell) for c in r])
        t = Table(table_data, colWidths=col_widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563EB")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9CA3AF")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)
    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=filename)


def _sheet_header_meta(activity, rows):
    """签到表/台账抬头：机构 + 活动信息（打印/备案）"""
    cap = f"{activity['capacity']} 人" if activity.get("capacity") else "不限"
    meta = [
        f"活动名称：{activity['name']}",
        f"活动时间：{activity.get('activity_time') or '（待定）'}　　活动地点：{activity.get('location') or '（待定）'}"
        f"　　招募上限：{cap}　　名单人数：{len(rows)} 人",
    ]
    return meta


@app.route("/api/activities/<int:activity_id>/signup_sheet")
@require_perm("export_data")
def export_signup_sheet(activity_id):
    """
    任务4：按活动导出纸质签到表。
    format=html（浏览器直接打印）/ xlsx / pdf
    - 名单：该活动全部有效报名（排除已合并），真名+小名同时显示
    - 纸质列：到场情况（□到场 □未到场 □临时到场，已回传的☑回显）/ 现场备注 / 家长签名
    """
    fmt = (request.args.get("format") or "html").lower()
    if fmt not in ("html", "xlsx", "pdf"):
        return jsonify({"error": "format 仅支持 html / xlsx / pdf"}), 400

    db = get_db()
    activity = get_activity(db, activity_id)
    if not activity:
        return jsonify({"error": "活动不存在"}), 404
    rows = get_signup_sheet_rows(db, activity_id)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    title = f"示例公益机构 · 活动签到表"
    meta = _sheet_header_meta(activity, rows)

    if fmt == "html":
        # 可打印 HTML：A4 纵向，浏览器打印即得
        head = "".join(f"<th>{label}</th>" for _, label in SIGNUP_SHEET_COLUMNS)
        body = []
        for i, r in enumerate(rows, 1):
            cells = [
                str(i),
                r.get("student_real_name") or "（空）",
                r.get("student_nickname") or "—",
                r.get("parent_name") or "—",
                r.get("phone") or "—",
                r.get("relation") or "—",
                attendance_cell_text(r),
                r.get("attendance_note") or "",
                "",
            ]
            body.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title} - {activity['name']}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
         margin: 24px auto; max-width: 1000px; color: #111; font-size: 13px; }}
  h1 {{ text-align: center; font-size: 20px; margin: 0 0 4px; }}
  .org {{ text-align: center; font-size: 14px; color: #444; margin-bottom: 10px; }}
  .meta {{ margin: 8px 0 10px; line-height: 1.7; }}
  .meta b {{ margin-right: 14px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ border: 1px solid #666; padding: 5px 6px; text-align: left; vertical-align: middle; }}
  th {{ background: #eef2ff; font-weight: 600; }}
  td {{ height: 26px; }}
  .foot {{ margin-top: 14px; display: flex; justify-content: space-between; font-size: 13px; }}
  @media print {{
    body {{ margin: 0; max-width: none; font-size: 12px; }}
    .no-print {{ display: none; }}
    th {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    tr {{ page-break-inside: avoid; }}
    @page {{ size: A4 landscape; margin: 10mm; }}
  }}
</style>
</head>
<body>
<div class="no-print" style="text-align:right;margin-bottom:8px;">
  <button onclick="window.print()" style="padding:6px 18px;background:#2563eb;color:#fff;border:0;border-radius:6px;cursor:pointer;">🖨 打印</button>
</div>
<h1>活动签到表</h1>
<div class="org">示例公益机构</div>
<div class="meta"><b>活动名称：{activity['name']}</b><br>{meta[1]}<br>填表日期：{'____年____月____日'}</div>
<table>
  <thead><tr>{head}</tr></thead>
  <tbody>{''.join(body)}</tbody>
</table>
<div class="foot">
  <span>应到：{len(rows)} 人　　实到：____ 人　　临时到场：____ 人　　未到场：____ 人</span>
  <span>现场负责人签字：____________</span>
</div>
</body>
</html>"""
        return send_file(io.BytesIO(html.encode("utf-8")), mimetype="text/html; charset=utf-8",
                         as_attachment=False, download_name=f"签到表_{activity['name']}_{stamp}.html")

    if fmt == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill

        wb = Workbook()
        ws = wb.active
        ws.title = "签到表"
        ws.append([f"{title} —— {activity['name']}"])
        ws.append([meta[1]])
        ws.append([label for _, label in SIGNUP_SHEET_COLUMNS])
        for i, r in enumerate(rows, 1):
            ws.append([
                i,
                r.get("student_real_name") or "（空）",
                r.get("student_nickname") or "—",
                r.get("parent_name") or "—",
                r.get("phone") or "—",
                r.get("relation") or "—",
                attendance_cell_text(r),
                r.get("attendance_note") or "",
                "",
            ])
        ws.append([])
        ws.append([f"应到 {len(rows)} 人　实到 ____ 人　临时到场 ____ 人　未到场 ____ 人　现场负责人签字：____________"])
        # 样式：标题加粗、表头蓝底、列宽、行高（打印友好）
        bold = Font(bold=True, size=13)
        ws["A1"].font = bold
        head_fill = PatternFill("solid", fgColor="2563EB")
        for cell in ws[3]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        widths = [5, 14, 12, 12, 14, 11, 24, 18, 12]
        for idx, w in enumerate(widths, 1):
            ws.column_dimensions[chr(64 + idx)].width = w
        for row_cells in ws.iter_rows(min_row=4):
            for c in row_cells:
                c.alignment = Alignment(vertical="center", wrap_text=True)
            ws.row_dimensions[c.row].height = 22
        ws.print_options.horizontalCentered = True
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"签到表_{activity['name']}_{stamp}.xlsx")

    # PDF：A4 横向
    data_rows = []
    for i, r in enumerate(rows, 1):
        data_rows.append([
            i,
            r.get("student_real_name") or "（空）",
            r.get("student_nickname") or "—",
            r.get("parent_name") or "—",
            r.get("phone") or "—",
            r.get("relation") or "—",
            attendance_cell_text(r),
            r.get("attendance_note") or "",
            "",
        ])
    data_rows.append([f"应到 {len(rows)} 人　实到 ____ 人　临时到场 ____ 人　未到场 ____ 人", "", "", "", "", "",
                      "", "", "现场负责人签字：____________"])
    return _pdf_build(
        [(title, meta, [label for _, label in SIGNUP_SHEET_COLUMNS], data_rows,
          [30, 80, 70, 75, 90, 60, 150, 110, 100])],
        f"签到表_{activity['name']}_{stamp}.pdf",
    )


def _log_summary(l):
    """operation_logs 行 → 中文摘要（台账异常处理记录备案用）"""
    op = {
        "create": "创建", "edit": "编辑", "manual_tag": "人工打标", "remove_manual_tag": "取消打标",
        "mark_duplicate": "标记重复", "mark_not_duplicate": "标记非重复", "merge": "合并",
        "auto_merge": "自动合并", "split": "拆分组", "history_confirm": "历史确认建档",
        "history_ignore": "历史忽略", "history_autofill": "历史自动补全", "attendance": "签到回传",
        "update": "更新", "delete": "删除",
    }.get(l["operation_type"], l["operation_type"])
    parts = []
    if l["field_name"] and (l["old_value"] or l["new_value"]):
        parts.append(f"{l['field_name']}：{l['old_value']} → {l['new_value']}")
    elif l["new_value"]:
        parts.append(str(l["new_value"]))
    if l["tag"]:
        parts.append(f"【{l['tag']}】")
    who = l["operator"] or "系统"
    return f"[{l['operated_at']}] {op} {' '.join(p for p in parts if p)}（{who}）".strip()


def _attendance_sheet_rows(data):
    """签到结果明细行（台账 sheet3 / PDF 段3）"""
    out = []
    for i, r in enumerate(data, 1):
        out.append([
            i,
            r.get("student_real_name") or "（空）",
            r.get("student_nickname") or "—",
            r.get("parent_name") or "—",
            r.get("attendance_status") or "未记录",
            r.get("attendance_note") or "—",
            r.get("attendance_time") or "—",
            r.get("attendance_operator") or "—",
        ])
    return out

LEDGER_ATTENDANCE_HEADER = ["序号", "学生真名", "学生小名", "家长姓名", "签到状态", "现场备注", "回传时间", "记录人"]


@app.route("/api/export/ledger")
@require_perm("export_data")
def export_ledger():
    """
    任务4：活动台账导出（备案口径）= 报名记录 + 异常处理记录 + 最终签到结果。
    activity_id 可选：不传 = 全部活动；传 = 单活动归档台账。
    format=xlsx（多 sheet）/ pdf
    """
    activity_id = request.args.get("activity_id", "") or None
    fmt = (request.args.get("format") or "xlsx").lower()
    if fmt not in ("xlsx", "pdf"):
        return jsonify({"error": "format 仅支持 xlsx / pdf"}), 400

    db = get_db()
    activity = get_activity(db, activity_id) if activity_id else None
    if activity_id and not activity:
        return jsonify({"error": "活动不存在"}), 404
    data = get_export_rows(activity_id=activity_id)
    if activity_id:
        data = [d for d in data if d.get("status") != "已合并"]
    scope_name = activity["name"] if activity else "全部活动"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    title = f"示例公益机构 · 报名与签到台账（{scope_name}）"
    meta = [
        f"统计范围：{scope_name}　　台账生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"报名总数：{len(data)} 条　　到场：{sum(1 for d in data if d.get('attendance_status') == '到场')}　"
        f"未到场：{sum(1 for d in data if d.get('attendance_status') == '未到场')}　"
        f"临时到场：{sum(1 for d in data if d.get('attendance_status') == '临时到场')}　"
        f"未记录：{sum(1 for d in data if not d.get('attendance_status'))}",
    ]

    if fmt == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill

        wb = Workbook()

        # Sheet1 报名台账：与统一列表导出字段一致 + 签到结果
        ws = wb.active
        ws.title = "报名台账"
        ws.append([title])
        ws.append([meta[0]])
        ws.append([label for _, label in EXPORT_COLUMNS])
        for d in data:
            ws.append([serialize_export_value(d, col) for col, _ in EXPORT_COLUMNS])
        ws["A1"].font = Font(bold=True, size=13)
        head_fill = PatternFill("solid", fgColor="2563EB")
        for cell in ws[3]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for row_cells in ws.iter_rows(min_row=4):
            for c in row_cells:
                c.alignment = Alignment(vertical="center", wrap_text=True)

        # Sheet2 异常处理记录：有异常标签或有留痕的报名 + 全量留痕摘要
        ws2 = wb.create_sheet("异常处理记录")
        ws2.append(["序号", "学生真名", "学生小名", "家长姓名", "异常标签（合计）", "人工确认", "当前状态", "处理留痕（时间 类型 内容 操作人）"])
        ids = [d["id"] for d in data]
        logs_map = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for l in db.execute(
                f"SELECT * FROM operation_logs WHERE enrollment_id IN ({marks}) ORDER BY enrollment_id, operated_at, id", ids
            ).fetchall():
                logs_map.setdefault(l["enrollment_id"], []).append(l)
        rowno = 0
        for d in data:
            logs = logs_map.get(d["id"], [])
            if not d.get("anomaly_tags") and not logs and not d.get("confirmed_ok"):
                continue
            rowno += 1
            ws2.append([
                rowno,
                d.get("student_real_name") or "（空）",
                d.get("student_nickname") or "—",
                d.get("parent_name") or "—",
                "；".join(d.get("anomaly_tags") or []) or "无",
                "是" if d.get("confirmed_ok") else "否",
                d.get("status") or "",
                "\n".join(_log_summary(l) for l in logs) or "（无留痕）",
            ])
        if rowno == 0:
            ws2.append(["—", "—", "—", "—", "无异常记录", "—", "—", "—"])
        for cell in ws2[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        widths2 = [5, 12, 10, 10, 26, 8, 10, 70]
        for idx, w in enumerate(widths2, 1):
            ws2.column_dimensions[chr(64 + idx)].width = w
        for row_cells in ws2.iter_rows(min_row=2):
            for c in row_cells:
                c.alignment = Alignment(vertical="top", wrap_text=True)

        # Sheet3 签到结果：统计 + 明细
        ws3 = wb.create_sheet("签到结果")
        ws3.append(["签到结果汇总"])
        ws3.append([meta[1]])
        ws3.append([])
        ws3.append(LEDGER_ATTENDANCE_HEADER)
        for r in _attendance_sheet_rows(data):
            ws3.append(r)
        ws3["A1"].font = Font(bold=True, size=12)
        for cell in ws3[4]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = head_fill
        widths3 = [5, 14, 12, 12, 10, 24, 20, 12]
        for idx, w in enumerate(widths3, 1):
            ws3.column_dimensions[chr(64 + idx)].width = w

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"台账_{scope_name}_{stamp}.xlsx")

    # PDF 台账：三段（报名台账精选列 → 异常处理记录 → 签到结果）
    data_rows = []
    for d in data:
        vals = []
        for col, _, _w in LEDGER_PDF_COLUMNS:
            if col == "anomaly_tags":
                vals.append("；".join(d.get("anomaly_tags") or []) or "无")
            elif col == "source_type":
                vals.append(SOURCE_LABELS.get(d.get("source_type"), d.get("source_type") or ""))
            else:
                vals.append(d.get(col) if d.get(col) not in (None, "") else "—")
        data_rows.append(vals)
    if not data_rows:
        data_rows = [["—"] * len(LEDGER_PDF_COLUMNS)]

    # 异常处理段
    ids = [d["id"] for d in data]
    logs_map = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for l in db.execute(
            f"SELECT * FROM operation_logs WHERE enrollment_id IN ({marks}) ORDER BY enrollment_id, operated_at, id", ids
        ).fetchall():
            logs_map.setdefault(l["enrollment_id"], []).append(l)
    an_rows = []
    rowno = 0
    for d in data:
        logs = logs_map.get(d["id"], [])
        if not d.get("anomaly_tags") and not logs and not d.get("confirmed_ok"):
            continue
        rowno += 1
        an_rows.append([
            rowno,
            d.get("student_real_name") or "（空）",
            d.get("student_nickname") or "—",
            "；".join(d.get("anomaly_tags") or []) or "无",
            "是" if d.get("confirmed_ok") else "否",
            d.get("status") or "",
            "\n".join(_log_summary(l) for l in logs) or "（无留痕）",
        ])
    if not an_rows:
        an_rows = [["—", "—", "—", "无异常记录", "—", "—", "—"]]

    att_rows = _attendance_sheet_rows(data) if data else [["—"] * len(LEDGER_ATTENDANCE_HEADER)]
    sections = [
        (title, meta, [label for _, label, _w in LEDGER_PDF_COLUMNS], data_rows,
         [w for _, _, w in LEDGER_PDF_COLUMNS]),
        ("异常处理记录", meta, ["序号", "学生真名", "学生小名", "异常标签", "人工确认", "当前状态", "处理留痕"],
         an_rows, [28, 70, 60, 130, 46, 52, 430]),
        ("最终签到结果", meta, LEDGER_ATTENDANCE_HEADER, att_rows, [28, 80, 66, 76, 56, 170, 150, 80]),
    ]
    return _pdf_build(sections, f"台账_{scope_name}_{stamp}.pdf")


# --------------------------- 群接龙导入预览 ---------------------------

# 预览数据持久化在 SQLite（import_previews 表），服务重启不丢


def normalize_header(header):
    """标准化表头：去空格、去换行、小写"""
    return (header or "").strip().replace("\n", "").replace("\r", "").lower()


COLUMN_ALIASES = {
    "student_real_name": ["姓名", "真名", "学生姓名", "学生真名", "孩子姓名", "名字"],
    "student_nickname": ["小名", "昵称", "孩子小名", "乳名"],
    "parent_name": ["家长姓名", "家长", "家长名字", "监护人", "父母"],
    "phone": ["手机号", "电话", "联系方式", "手机", "联系电话"],
    "relation": ["关系", "亲属关系", "与孩子关系", "家长关系", "称呼"],
}


def map_columns(headers):
    """把群接龙表头映射到统一字段"""
    normalized = {normalize_header(h): idx for idx, h in enumerate(headers)}
    mapping = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            key = normalize_header(alias)
            if key in normalized:
                mapping[field] = normalized[key]
                break
    return mapping


def read_csv_file(path):
    """尝试多种编码读取 CSV"""
    encodings = ["utf-8-sig", "utf-8", "gbk", "gb2312"]
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                reader = csv.reader(f)
                rows = list(reader)
                return rows
        except Exception:
            continue
    raise Exception("CSV 编码无法识别，请保存为 UTF-8 或 GBK")


def read_excel_file(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(values_only=True):
        rows.append([str(cell) if cell is not None else "" for cell in row])
    return rows


# --------------------------- 群接龙文本解析 ---------------------------

PHONE_RE = re.compile(r"1\d{10}")
PARENT_KEYWORDS = ["妈妈", "爸爸", "家长", "妈", "爸", "爷爷", "奶奶", "外公", "外婆", "姥姥", "姥爷", "女士", "先生"]


def detect_parent_name(cells):
    """从字段中识别家长姓名"""
    for cell in cells:
        for kw in PARENT_KEYWORDS:
            if kw in cell:
                return cell
    return ""


def parse_group_text(text):
    """
    解析用户粘贴的群接龙文本
    支持两种格式：
    1. 表头格式：第一行是表头（姓名/小名/家长姓名/手机号/关系）
    2. 自由接龙格式：每行一个报名，字段顺序为 家长姓名 学生真名 学生小名 手机号
       如果只有 3 个字段，则视为 家长姓名 学生真名 手机号（无小名）
    返回 records 列表，每条包含 raw_text
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return []

    # 判断第一行是否为表头
    first_line = lines[0]
    first_cells = re.split(r"[\s\t,，]+", first_line)
    is_header = any(k in "".join(first_cells) for k in ["姓名", "小名", "家长", "手机", "电话", "关系", "孩子"])

    records = []
    mapping = None
    data_lines = lines

    if is_header:
        mapping = map_columns(first_cells)
        if mapping:
            # 真的识别出列映射才按表头处理，否则回退为数据行
            data_lines = lines[1:]
        else:
            mapping = None
            is_header = False

    for idx, raw_line in enumerate(data_lines, start=1):
        # 去掉常见序号前缀，如 "1.", "1、", "1)"
        content = re.sub(r"^\d+\s*[\.、\)）\]\】]\s*", "", raw_line).strip()
        if not content:
            continue

        student_real_name = ""
        student_nickname = ""
        parent_name = ""
        phone = ""
        relation = ""

        if mapping:
            # 表头模式，按表头映射
            cells = re.split(r"[\s\t,，]+", content)
            def get(field):
                col = mapping.get(field)
                if col is None or col >= len(cells):
                    return ""
                return cells[col].strip()
            student_real_name = get("student_real_name")
            student_nickname = get("student_nickname")
            parent_name = get("parent_name")
            phone = get("phone")
            relation = get("relation")
            # 常见错位容错：手机号列缺失时后列左移（如「母亲」落到手机号列）。
            # 手机号值不符合 1 开头 11 位格式、且看着是关系称谓 → 归位到关系列。
            if phone and not PHONE_RE.fullmatch(phone):
                if not relation and (phone in ("母亲", "父亲") or any(kw == phone for kw in PARENT_KEYWORDS)):
                    relation = phone
                    phone = ""
        else:
            # 自由接龙模式
            # 提取手机号
            phone_match = PHONE_RE.search(content)
            if phone_match:
                phone = phone_match.group(0)
                content_no_phone = content.replace(phone, "", 1).strip()
            else:
                content_no_phone = content

            # 分割剩余字段
            cells = [c.strip() for c in re.split(r"[\s\t,，]+", content_no_phone) if c.strip()]

            # 识别家长姓名
            parent_name = detect_parent_name(cells)
            if parent_name:
                cells = [c for c in cells if c != parent_name]

            # 剩余字段按顺序分配
            if len(cells) >= 1:
                student_real_name = cells[0]
            if len(cells) >= 2:
                student_nickname = cells[1]

        records.append({
            "student_real_name": student_real_name,
            "student_nickname": student_nickname,
            "parent_name": parent_name,
            "phone": phone,
            "relation": relation,
            "raw_text": raw_line,
            "source_type": "group_signup",
            "source_user": f"文本粘贴#第{idx}条",
            "source_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    return records


@app.route("/api/import/group_signup", methods=["POST"])
@require_perm("manage_enrollments")
def import_group_signup():
    """
    群接龙 CSV/Excel 导入预览接口
    自动列映射、自动异常检测、返回 preview_id
    """
    if "file" not in request.files:
        return jsonify({"error": "未上传文件"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".csv", ".xlsx", ".xls"]:
        return jsonify({"error": "仅支持 csv / xlsx 文件"}), 400

    # 保存临时文件
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        if ext == ".csv":
            raw_rows = read_csv_file(tmp_path)
        else:
            raw_rows = read_excel_file(tmp_path)
    except Exception as e:
        return jsonify({"error": f"文件解析失败：{str(e)}"}), 400
    finally:
        os.unlink(tmp_path)

    if len(raw_rows) < 2:
        return jsonify({"error": "文件内容不足，至少需要表头+一行数据"}), 400

    # 任务3.5：群接龙导入也必须选择归属活动（进行中）
    db = get_db()
    activity, act_err = check_activity_for_enrollment(db, request.form.get("activity_id"))
    if act_err:
        return jsonify({"error": act_err}), 400

    headers = raw_rows[0]
    mapping = map_columns(headers)

    # 必填字段映射检查
    missing_required = []
    if "student_real_name" not in mapping and "student_nickname" not in mapping:
        missing_required.append("学生真名/小名")
    if "phone" not in mapping:
        missing_required.append("手机号")
    if missing_required:
        return jsonify({
            "error": "缺少必要列",
            "missing": missing_required,
            "detected_mapping": mapping,
            "headers": headers
        }), 400

    # 当前库内已有记录，用于重复检测
    existing = [row_to_dict(r) for r in db.execute("SELECT * FROM enrollments").fetchall()]

    uploader = request.form.get("uploader", "运营导入")
    source_user = request.form.get("source_user", f"{uploader}@群接龙导入")
    import_batch_id = f"BATCH-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
    preview_id = f"preview-{uuid.uuid4().hex}"

    preview_records = []
    anomaly_count = 0

    for idx, row in enumerate(raw_rows[1:], start=1):
        # 跳过全空行
        if not any(str(cell).strip() for cell in row):
            continue

        def get(field):
            col = mapping.get(field)
            if col is None or col >= len(row):
                return ""
            return str(row[col]).strip()

        item = {
            "student_real_name": get("student_real_name"),
            "student_nickname": get("student_nickname"),
            "parent_name": get("parent_name"),
            "phone": get("phone"),
            "relation": get("relation"),
            "note": "",
            "raw_text": " / ".join(str(c).strip() for c in row if str(c).strip()),
            "source_type": "group_signup",
            "source_user": f"{source_user}#第{idx}条",
            "source_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "import_batch_id": import_batch_id,
            "activity_id": activity["id"],
            "system_tags": [],
            "manual_tags": [],
            "confirmed_ok": False,
            "operation_logs": [],
        }

        item = recalculate_preview_record(item, existing, operator=uploader)
        if item["anomaly_tags"]:
            anomaly_count += 1

        preview_records.append(item)
        existing.append({
            "student_real_name": item["student_real_name"],
            "parent_name": item["parent_name"],
            "phone": item["phone"],
        })

    # 逐条检测时后面的记录还未进入对照池（单向），这里全量重算补上双向重复对照
    recalc_all_preview_records(preview_records, operator=uploader)
    anomaly_count = sum(1 for r in preview_records if r["anomaly_tags"])

    save_preview(preview_id, preview_records, import_batch_id, source_user)

    return jsonify({
        "preview_id": preview_id,
        "import_batch_id": import_batch_id,
        "total": len(preview_records),
        "anomaly_count": anomaly_count,
        "mapping": mapping,
        "activity": {"id": activity["id"], "name": activity["name"]},
        "records": preview_records,
    })


@app.route("/api/import/group_signup_text", methods=["POST"])
@require_perm("manage_enrollments")
def import_group_signup_text():
    """
    群接龙文本粘贴导入预览接口
    自动解析文本、异常检测、返回 preview_id
    """
    data = request.get_json(force=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "粘贴文本为空"}), 400

    # 任务3.5：群接龙粘贴导入也必须选择归属活动（进行中）
    db = get_db()
    activity, act_err = check_activity_for_enrollment(db, data.get("activity_id"))
    if act_err:
        return jsonify({"error": act_err}), 400

    uploader = data.get("uploader", "文本粘贴")
    source_user = data.get("source_user", f"{uploader}@群接龙导入")
    import_batch_id = f"BATCH-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"

    # 当前库内已有记录，用于重复检测
    existing = [row_to_dict(r) for r in db.execute("SELECT * FROM enrollments").fetchall()]

    parsed_records = parse_group_text(text)
    if not parsed_records:
        return jsonify({"error": "未能解析出任何报名记录，请检查文本格式"}), 400

    preview_records = []
    anomaly_count = 0

    for idx, item in enumerate(parsed_records, start=1):
        item["source_user"] = f"{source_user}#第{idx}条"
        item["import_batch_id"] = import_batch_id
        item["activity_id"] = activity["id"]
        item["note"] = ""
        item["system_tags"] = []
        item["manual_tags"] = []
        item["confirmed_ok"] = False
        item["operation_logs"] = []

        item = recalculate_preview_record(item, existing, operator=uploader)
        if item["anomaly_tags"]:
            anomaly_count += 1

        preview_records.append(item)
        existing.append({
            "student_real_name": item["student_real_name"],
            "parent_name": item["parent_name"],
            "phone": item["phone"],
        })

    # 逐条检测时后面的记录还未进入对照池（单向），这里全量重算补上双向重复对照
    recalc_all_preview_records(preview_records, operator=uploader)
    anomaly_count = sum(1 for r in preview_records if r["anomaly_tags"])

    preview_id = f"preview-{uuid.uuid4().hex}"
    save_preview(preview_id, preview_records, import_batch_id, source_user)

    return jsonify({
        "preview_id": preview_id,
        "import_batch_id": import_batch_id,
        "total": len(preview_records),
        "anomaly_count": anomaly_count,
        "activity": {"id": activity["id"], "name": activity["name"]},
        "records": preview_records,
    })


@app.route("/api/import/operate", methods=["POST"])
@require_perm("manage_enrollments")
def operate_preview():
    """
    预览阶段人工兜底操作统一接口
    action: edit | add | manual_tag | remove_manual_tag | confirm_ok | add_note
    返回更新后的单条 record 或整个 preview
    """
    data = request.get_json(force=True) or {}
    preview_id = data.get("preview_id")
    action = data.get("action")

    if not preview_id or not action:
        return jsonify({"error": "缺少 preview_id 或 action"}), 400

    preview = load_preview(preview_id)
    if not preview:
        return jsonify({"error": "预览已过期或不存在，请重新上传"}), 404

    operator = data.get("operator", "社工")
    records = preview["records"]
    now = now_str()
    db = get_db()

    def persist_and_return(record, **extra):
        """把最新 records 写回持久化存储后返回"""
        save_preview(preview_id, records, preview["import_batch_id"], preview["source_user"])
        return jsonify({"data": record, **extra})

    if action == "edit":
        idx = data.get("idx")
        field = data.get("field")
        old_value = data.get("old_value", "")
        new_value = data.get("new_value", "")

        if idx is None or idx < 0 or idx >= len(records):
            return jsonify({"error": "记录索引非法"}), 400
        if field not in ["student_real_name", "student_nickname", "parent_name", "phone", "relation", "note"]:
            return jsonify({"error": "字段不可编辑"}), 400

        record = records[idx]
        record[field] = new_value.strip()

        add_operation_log(record, {
            "type": "edit",
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "operator": operator,
            "time": now,
        })

        record = recalculate_preview_record(record, records, operator=operator, action="edit")
        return persist_and_return(record, idx=idx)

    elif action == "add":
        new_record = data.get("record", {})
        # 任务3.5：手动补漏的行同样必须归属活动——继承本批次的归属活动
        batch_activity_id = data.get("activity_id")
        if not batch_activity_id:
            batch_activity_id = next((r.get("activity_id") for r in records if r.get("activity_id")), 0)
        new_record.update({
            "source_type": "group_signup",
            "source_user": f"{operator}@手动补漏",
            "source_time": now,
            "import_batch_id": preview["import_batch_id"],
            "activity_id": int(batch_activity_id or 0),
            "raw_text": "手动新增",
            "system_tags": [],
            "manual_tags": [],
            "confirmed_ok": False,
            "operation_logs": [{
                "type": "add",
                "message": "人工手动补漏新增记录",
                "operator": operator,
                "time": now,
            }],
        })
        new_record = recalculate_preview_record(new_record, records, operator=operator, action="add")
        records.append(new_record)
        return persist_and_return(new_record, idx=len(records) - 1, total=len(records))

    elif action == "manual_tag":
        idx = data.get("idx")
        tag = data.get("tag", "").strip()
        if idx is None or idx < 0 or idx >= len(records) or not tag:
            return jsonify({"error": "参数非法"}), 400

        record = records[idx]
        if tag not in record.get("manual_tags", []):
            record.setdefault("manual_tags", []).append(tag)
            add_operation_log(record, {
                "type": "manual_tag",
                "tag": tag,
                "operator": operator,
                "time": now,
            })
        record = recalculate_preview_record(record, records, operator=operator)
        return persist_and_return(record, idx=idx)

    elif action == "remove_manual_tag":
        idx = data.get("idx")
        tag = data.get("tag", "").strip()
        if idx is None or idx < 0 or idx >= len(records) or not tag:
            return jsonify({"error": "参数非法"}), 400

        record = records[idx]
        if tag in record.get("manual_tags", []):
            record["manual_tags"] = [t for t in record["manual_tags"] if t != tag]
            add_operation_log(record, {
                "type": "remove_manual_tag",
                "tag": tag,
                "operator": operator,
                "time": now,
            })
        record = recalculate_preview_record(record, records, operator=operator)
        return persist_and_return(record, idx=idx)

    elif action == "confirm_ok":
        idx = data.get("idx")
        if idx is None or idx < 0 or idx >= len(records):
            return jsonify({"error": "记录索引非法"}), 400

        record = records[idx]
        record["confirmed_ok"] = True
        add_operation_log(record, {
            "type": "confirm_ok",
            "message": "人工确认该记录无异常（系统标红判定为误报）",
            "operator": operator,
            "time": now,
        })
        record = recalculate_preview_record(record, records, operator=operator, action="confirm_ok")
        return persist_and_return(record, idx=idx)

    elif action == "add_note":
        idx = data.get("idx")
        old_note = data.get("old_note", "")
        new_note = data.get("new_note", "")
        if idx is None or idx < 0 or idx >= len(records):
            return jsonify({"error": "记录索引非法"}), 400

        record = records[idx]
        record["note"] = new_note.strip()
        add_operation_log(record, {
            "type": "add_note",
            "field": "note",
            "old_value": old_note,
            "new_value": new_note,
            "operator": operator,
            "time": now,
        })
        return persist_and_return(record, idx=idx)

    elif action == "mark_not_duplicate":
        # 预览阶段：社工判定该行与已有报名不是同一人 → 本次导入不再标「重复」
        idx = data.get("idx")
        if idx is None or idx < 0 or idx >= len(records):
            return jsonify({"error": "记录索引非法"}), 400

        record = records[idx]
        record["not_duplicate"] = True
        add_operation_log(record, {
            "type": "mark_not_duplicate",
            "message": "人工判定：与已有报名不是同一人，本次不按重复处理",
            "operator": operator,
            "time": now,
        })
        # 联动重算整批：该行退出重复对照后，组内其他行的重复标签同步解除
        recalc_all_preview_records(records, operator=operator, action="mark")
        # 返回全量 records：联动重算改变了组内其他行的标签，前端需要整体刷新
        return persist_and_return(record, idx=idx, records=records, total=len(records))

    elif action == "unmark_not_duplicate":
        idx = data.get("idx")
        if idx is None or idx < 0 or idx >= len(records):
            return jsonify({"error": "记录索引非法"}), 400

        record = records[idx]
        record["not_duplicate"] = False
        add_operation_log(record, {
            "type": "unmark_not_duplicate",
            "message": "取消「非重复」判定，恢复重复检测",
            "operator": operator,
            "time": now,
        })
        recalc_all_preview_records(records, operator=operator, action="mark")
        return persist_and_return(record, idx=idx, records=records, total=len(records))

    elif action == "remove_record":
        # 预览阶段「合并」：确认组内是同一个人，保留一条，丢弃其余行（不入库）
        idx = data.get("idx")
        keep_idx = data.get("keep_idx")
        if idx is None or idx < 0 or idx >= len(records):
            return jsonify({"error": "记录索引非法"}), 400
        if keep_idx is None or keep_idx < 0 or keep_idx >= len(records) or keep_idx == idx:
            return jsonify({"error": "keep_idx 非法（要保留的行）"}), 400

        removed = records.pop(idx)
        keep = records[keep_idx if keep_idx < idx else keep_idx - 1]
        add_operation_log(keep, {
            "type": "merge_preview",
            "message": f"人工确认与第 {idx + 1} 行为同一人，保留本行、丢弃该行（不入库）",
            "operator": operator,
            "time": now,
        })
        # 联动重算整批：组内行数变化后，剩余行的重复标签需要刷新（如两行组删一行后重复解除）
        recalc_all_preview_records(records, operator=operator, action="merge")
        save_preview(preview_id, records, preview["import_batch_id"], preview["source_user"])
        return jsonify({
            "data": keep,
            "removed_idx": idx,
            "keep_idx": keep_idx if keep_idx < idx else keep_idx - 1,
            "total": len(records),
            "records": records,
        })

    elif action == "history_confirm":
        # 任务3：预览阶段人工核对历史命中 → 确认同一年儿童，立即写入历史档案（下次报名复用）
        idx = data.get("idx")
        profile_id = int(data.get("profile_id") or 0)
        if idx is None or idx < 0 or idx >= len(records):
            return jsonify({"error": "记录索引非法"}), 400

        record = records[idx]
        if record.get("history_status") == "confirmed":
            return jsonify({"error": "该记录已确认写入历史库，请勿重复操作"}), 400

        new_pid = apply_history_confirm(db, profile_id, record, operator)
        record["history_profile_id"] = new_pid
        record["history_status"] = "confirmed"
        add_operation_log(record, {
            "type": "history_confirm",
            "message": f"人工核对：确认与历史档案 #{new_pid} 是同一儿童，已写入历史信息库（下次报名自动比对复用）",
            "operator": operator,
            "time": now,
        })
        record = recalculate_preview_record(record, records, operator=operator, action="mark")
        save_preview(preview_id, records, preview["import_batch_id"], preview["source_user"])
        profile = profile_to_dict(db.execute("SELECT * FROM child_profiles WHERE id = ?", (new_pid,)).fetchone())
        return jsonify({"data": record, "profile": profile,
                        "message": f"已确认同一儿童并写入历史档案 #{new_pid}"})

    elif action == "history_ignore":
        # 任务3：人工判定不是同一儿童 → 忽略该历史命中，不再提示（留痕可查）
        idx = data.get("idx")
        if idx is None or idx < 0 or idx >= len(records):
            return jsonify({"error": "记录索引非法"}), 400

        record = records[idx]
        record["history_status"] = "ignored"
        add_operation_log(record, {
            "type": "history_ignore",
            "message": "人工核对：与命中的历史档案不是同一儿童，忽略该提示",
            "operator": operator,
            "time": now,
        })
        record = recalculate_preview_record(record, records, operator=operator, action="mark")
        return persist_and_return(record, idx=idx, records=records, total=len(records))

    elif action == "history_autofill":
        # 任务3.5：手动「按档案补全」——用命中的历史档案补全该行缺失字段（只补空、不覆盖、不合并）。
        # 自动补全只在解析时做一次（once-only），这里是人工按需再次触发；可传 profile_id 指定档案，默认最强命中。
        idx = data.get("idx")
        if idx is None or idx < 0 or idx >= len(records):
            return jsonify({"error": "记录索引非法"}), 400

        record = records[idx]
        matches = record.get("history_matches")
        if matches is None:
            matches = match_child_profiles(record, get_all_profiles(db))
            record["history_matches"] = matches
        if not matches:
            return jsonify({"error": "该记录没有命中的历史档案，无法按档案补全"}), 400

        profile_id = int(data.get("profile_id") or 0)
        hit = next((m for m in matches if m["profile_id"] == profile_id), None) if profile_id else matches[0]
        if not hit:
            return jsonify({"error": f"该记录未命中档案 #{profile_id}"}), 400

        filled = apply_history_autofill(record, hit["profile"])
        if not filled:
            return persist_and_return(record, idx=idx, message="没有可补全的空字段（已有值不会被覆盖）")

        for item2 in filled:
            label = AUTOFILL_FIELD_LABELS.get(item2["field"], item2["field"])
            add_operation_log(record, {
                "type": "history_autofill",
                "field": item2["field"],
                "old_value": "",
                "new_value": item2["value"],
                "tag": f"来自档案#{item2['profile_id']}",
                "message": f"人工按档案补全「{label}」→ {item2['value']}（命中档案 #{item2['profile_id']}）",
                "operator": operator,
            })
        # 补全后信息缺失等系统标签应消失，重算
        record = recalculate_preview_record(record, records, operator=operator, action="mark")
        return persist_and_return(record, idx=idx,
                                  message=f"已按档案 #{hit['profile_id']} 补全 {len(filled)} 个空字段")

    else:
        return jsonify({"error": "未知操作"}), 400


@app.route("/api/import/confirm", methods=["POST"])
@require_perm("manage_enrollments")
def confirm_import():
    """
    确认导入：把人工修正/确认后的预览数据批量写入主表
    接收 { preview_id, records }
    """
    data = request.get_json(force=True) or {}
    preview_id = data.get("preview_id")

    if not preview_id:
        return jsonify({"error": "缺少 preview_id"}), 400

    preview = load_preview(preview_id)
    if not preview:
        return jsonify({"error": "预览已过期或不存在，请重新上传"}), 404

    # records 以服务端持久化的最新版本为准（每次 operate 都已落库，前端传回的仅作兜底，
    # 避免前端状态不同步时把旧数据入库——如历史核对/编辑后的最新标签）
    records = preview.get("records") or data.get("records") or []
    if not records:
        return jsonify({"error": "没有任何记录可导入"}), 400

    import_batch_id = preview.get("import_batch_id")
    db = get_db()
    created = []
    now = now_str()

    # 任务3.5：批次归属活动兜底——任何记录缺 activity_id（如旧版本手动补漏行）都继承批次归属
    batch_activity_id = next((int(r.get("activity_id") or 0) for r in records if r.get("activity_id")), 0)

    for item in records:
        # 基础校验
        source_type = item.get("source_type", "group_signup")
        if source_type not in SOURCE_TYPES:
            source_type = "group_signup"

        # 重新检测异常（用户可能已修正）
        # 注意：已「确认无误」的记录不重算，社工已判定系统标红为误报
        tags = item.get("anomaly_tags", [])
        if not tags and not item.get("confirmed_ok"):
            existing = [row_to_dict(r) for r in db.execute("SELECT * FROM enrollments").fetchall()]
            tags = detect_anomalies(item, existing)

        # 系统标签 / 人工标签 / 确认状态：沿用预览阶段人工兜底的结果
        system_tags = parse_tags(item.get("system_tags")) if item.get("system_tags") is not None else list(tags)
        manual_tags = parse_tags(item.get("manual_tags")) if item.get("manual_tags") is not None else []
        confirmed_ok = 1 if item.get("confirmed_ok") else 0
        not_duplicate = 1 if item.get("not_duplicate") else 0

        status = item.get("status", "待处理")
        if status not in STATUS_TYPES:
            status = "待处理" if tags else "已确认"

        cursor = db.execute(
            """
            INSERT INTO enrollments (
                student_real_name, student_nickname, parent_name, phone,
                id_card, emergency_contact, relation, source_type, source_user,
                source_time, import_batch_id, anomaly_tags, system_tags,
                manual_tags, confirmed_ok, not_duplicate, status, note,
                history_profile_id, history_status, activity_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.get("student_real_name", "").strip(),
                item.get("student_nickname", "").strip(),
                item.get("parent_name", "").strip(),
                item.get("phone", "").strip(),
                item.get("id_card", "").strip(),
                item.get("emergency_contact", "").strip(),
                item.get("relation", "").strip(),
                source_type,
                item.get("source_user", preview.get("source_user", "群接龙导入")),
                item.get("source_time") or now,
                import_batch_id,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(system_tags, ensure_ascii=False),
                json.dumps(manual_tags, ensure_ascii=False),
                confirmed_ok,
                not_duplicate,
                status,
                item.get("note", ""),
                int(item.get("history_profile_id") or 0),
                item.get("history_status") or "",
                int(item.get("activity_id") or batch_activity_id or 0),
                now,
                now,
            ),
        )
        db.commit()
        new_row = db.execute("SELECT * FROM enrollments WHERE id = ?", (cursor.lastrowid,)).fetchone()
        enrollment_id = new_row["id"]
        created.append(row_to_dict(new_row))

        # 任务3：预览阶段已确认写入历史库的记录，把关联报名 id 补进档案（档案更新已在预览确认时完成，不重复计数）
        if item.get("history_status") == "confirmed" and item.get("history_profile_id"):
            p_row = db.execute("SELECT enrollment_ids FROM child_profiles WHERE id = ?", (item["history_profile_id"],)).fetchone()
            if p_row:
                ids = [x for x in str(p_row["enrollment_ids"] or "").split(",") if x.strip().isdigit()]
                if str(enrollment_id) not in ids:
                    ids.append(str(enrollment_id))
                    db.execute("UPDATE child_profiles SET enrollment_ids = ?, updated_at = ? WHERE id = ?",
                               (",".join(ids), now, item["history_profile_id"]))
                    db.commit()

        # 写入操作日志
        for log in item.get("operation_logs", []):
            db.execute(
                """
                INSERT INTO operation_logs (
                    enrollment_id, import_batch_id, operation_type, field_name,
                    old_value, new_value, tag, operator, operated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    enrollment_id,
                    import_batch_id,
                    log.get("type", ""),
                    log.get("field", ""),
                    log.get("old_value", ""),
                    log.get("new_value", ""),
                    log.get("tag", ""),
                    log.get("operator", ""),
                    log.get("time", ""),
                ),
            )
        db.commit()

    # 清理预览缓存
    drop_preview(preview_id)

    return jsonify({"data": created, "count": len(created), "import_batch_id": import_batch_id}), 201


# --------------------------- 预览结果导出 ---------------------------

PREVIEW_EXPORT_COLUMNS = [
    ("idx", "行号"),
    ("student_real_name", "学生真名"),
    ("student_nickname", "学生小名"),
    ("parent_name", "家长姓名"),
    ("phone", "手机号"),
    ("relation", "与孩子关系"),
    ("note", "备注"),
    ("activity_name", "所属活动"),
    ("system_tags", "系统异常标签"),
    ("manual_tags", "人工异常标签"),
    ("confirmed_ok", "已人工确认"),
    ("status", "状态"),
    ("raw_text", "原始接龙行"),
    ("source_user", "来源用户"),
    ("operation_summary", "人工操作留痕"),
]


def preview_row_to_export(idx, record, activity_name=""):
    """把 preview record 转成导出行"""
    logs = record.get("operation_logs") or []
    summary = " | ".join(
        f"{(l.get('time') or '')[:16]} {l.get('operator') or ''}：{describe_log(l)}"
        for l in logs
    )
    return {
        "idx": idx + 1,
        "student_real_name": record.get("student_real_name", ""),
        "student_nickname": record.get("student_nickname", ""),
        "parent_name": record.get("parent_name", ""),
        "phone": record.get("phone", ""),
        "relation": record.get("relation", ""),
        "note": record.get("note", ""),
        "activity_name": activity_name,
        "system_tags": record.get("system_tags", []) or [],
        "manual_tags": record.get("manual_tags", []) or [],
        "confirmed_ok": record.get("confirmed_ok", False),
        "status": record.get("status", "待处理"),
        "raw_text": record.get("raw_text", ""),
        "source_user": record.get("source_user", ""),
        "operation_summary": summary,
    }


def describe_log(log):
    """把一条操作日志转成中文描述"""
    t = log.get("type")
    if t == "edit":
        return f"修改「{log.get('field')}」：{log.get('old_value') or '空'} → {log.get('new_value') or '空'}"
    if t == "add":
        return "手动补漏新增记录"
    if t == "manual_tag":
        return f"手动添加异常标签「{log.get('tag')}」"
    if t == "remove_manual_tag":
        return f"移除手动异常标签「{log.get('tag')}」"
    if t == "confirm_ok":
        return "人工确认无误（解除系统标红）"
    if t == "confirm_revoke":
        return log.get("message", "撤销确认无误")
    if t == "add_note":
        return f"修改备注为「{log.get('new_value') or '空'}」"
    return log.get("message") or t or "操作"


@app.route("/api/import/export", methods=["POST"])
@require_perm("manage_enrollments")
def export_preview():
    """
    导出当前预览的人工兜底处理结果（csv / xlsx）
    前端传 preview_id；若预览已过期，可直接传 records 兜底
    """
    data = request.get_json(force=True) or {}
    preview_id = data.get("preview_id")
    fmt = (data.get("format") or "csv").lower()
    if fmt not in ("csv", "xlsx"):
        return jsonify({"error": "仅支持 csv / xlsx"}), 400

    records = data.get("records")
    import_batch_id = data.get("import_batch_id", "")

    if preview_id and not records:
        preview = load_preview(preview_id)
        if not preview:
            return jsonify({"error": "预览已过期且未提供 records，无法导出"}), 404
        records = preview["records"]
        import_batch_id = preview.get("import_batch_id", import_batch_id)

    if not records:
        return jsonify({"error": "没有可导出的记录"}), 400

    # 任务3.5：导出行附带「所属活动」名称
    activity_names = get_activity_name_map(get_db())

    def act_name(r):
        return activity_names.get(int(r.get("activity_id") or 0), "")

    rows = [preview_row_to_export(i, r, act_name(r)) for i, r in enumerate(records)]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([label for _, label in PREVIEW_EXPORT_COLUMNS])
        for row in rows:
            writer.writerow([
                "；".join(row[col]) if col in ("system_tags", "manual_tags")
                else ("是" if row[col] else "否") if col == "confirmed_ok"
                else row.get(col, "")
                for col, _ in PREVIEW_EXPORT_COLUMNS
            ])
        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode("utf-8-sig")),
            mimetype="text/csv; charset=utf-8-sig",
            as_attachment=True,
            download_name=f"群接龙人工兜底结果_{stamp}.csv",
        )

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "人工兜底结果"

    headers = [label for _, label in PREVIEW_EXPORT_COLUMNS]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2563EB")

    red_fill = PatternFill("solid", fgColor="FEE2E2")
    green_fill = PatternFill("solid", fgColor="DCFCE7")
    for row in rows:
        ws.append([
            "；".join(row[col]) if col in ("system_tags", "manual_tags")
            else ("是" if row[col] else "否") if col == "confirmed_ok"
            else row.get(col, "")
            for col, _ in PREVIEW_EXPORT_COLUMNS
        ])
        if row["system_tags"]:
            ws.cell(row=ws.max_row, column=9).fill = red_fill
        if row["confirmed_ok"]:
            ws.cell(row=ws.max_row, column=11).fill = green_fill

    # 列宽
    for i, (col, _) in enumerate(PREVIEW_EXPORT_COLUMNS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = 14 if col != "operation_summary" else 60

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"群接龙人工兜底结果_{stamp}.xlsx",
    )


# --------------------------- 统计 ---------------------------

@app.route("/api/stats", methods=["GET"])
@require_perm("manage_enrollments")
def get_stats():
    """简单统计，用于前端概览"""
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0]
    anomaly = db.execute("SELECT COUNT(*) FROM enrollments WHERE anomaly_tags != '[]'").fetchone()[0]
    manual = db.execute(
        "SELECT COUNT(*) FROM enrollments WHERE manual_tags IS NOT NULL AND manual_tags != '[]'"
    ).fetchone()[0]
    confirmed = db.execute("SELECT COUNT(*) FROM enrollments WHERE confirmed_ok = 1").fetchone()[0]
    by_source = {}
    for st in SOURCE_TYPES:
        cnt = db.execute("SELECT COUNT(*) FROM enrollments WHERE source_type = ?", (st,)).fetchone()[0]
        by_source[SOURCE_LABELS[st]] = cnt
    # 任务3.5：活动概览
    active_activities = db.execute("SELECT COUNT(*) FROM activities WHERE status = '进行中'").fetchone()[0]
    return jsonify({
        "total": total,
        "anomaly": anomaly,
        "manual": manual,
        "confirmed": confirmed,
        "by_source": by_source,
        "active_activities": active_activities,
    })


# --------------------------- 任务5：登录 / 注册审核 / 账号权限 ---------------------------

@app.route("/api/auth/send_code", methods=["POST"])
def auth_send_code():
    """
    发送手机验证码（家长/志愿者验证码登录用）。
    Demo：验证码直接随响应返回模拟「短信下发」；生产环境应接短信服务且不回显。
    """
    data = request.get_json(force=True) or {}
    phone = (data.get("phone") or "").strip()
    if not re.fullmatch(r"1\d{10}", phone):
        return jsonify({"error": "请输入 11 位手机号"}), 400
    code = f"{int(uuid.uuid4().hex[:6], 16) % 1000000:06d}"
    db = get_db()
    db.execute(
        "INSERT INTO verification_codes (phone, code, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT(phone) DO UPDATE SET code = ?, created_at = ?",
        (phone, code, now_str(), code, now_str()),
    )
    db.commit()
    return jsonify({"data": {"phone": phone, "code": code}, "message": "验证码已发送（Demo 模拟短信，验证码直接回显）"})


def _login_user(user, action="login"):
    session["uid"] = user["id"]
    session.permanent = True
    log_audit(action, "user", user["id"], f"登录成功（角色 {ROLE_LABELS.get(user['role'], user['role'])}）", user=user)
    return jsonify({
        "data": {k: user[k] for k in ("id", "username", "display_name", "role", "phone", "status")},
        "message": f"欢迎，{user['display_name'] or user['username']}",
    })


@app.route("/api/auth/login_password", methods=["POST"])
def auth_login_password():
    """账号 + 口令登录（管理员/社工；账号由后台创建或注册审核通过后开通）"""
    data = request.get_json(force=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username = ? OR (role = 'social_worker' AND staff_no = ?)",
                     (username, username)).fetchone()
    user = dict(row) if row else None
    # 状态检查前置：待审核/已驳回/停用账号无论口令对错都给出对应提示（注册时未设口令，也能明确反馈）
    if user and user["status"] != "启用":
        if user["status"] == "待审核":
            return jsonify({"error": "账号待审核，审核通过后才能登录使用", "status": user["status"]}), 403
        if user["status"] == "已驳回":
            return jsonify({"error": f"注册申请已驳回：{user.get('reject_reason') or '不符合条件'}", "status": user["status"]}), 403
        return jsonify({"error": f"账号状态：{user['status']}，请联系管理员", "status": user["status"]}), 403
    if not user or user["password_hash"] != hash_password(password):
        log_audit("login_failed", "user", username, "口令登录失败（账号或口令错误）", user={"id": 0, "username": username})
        return jsonify({"error": "账号或口令错误"}), 401
    return _login_user(user)


@app.route("/api/auth/login_code", methods=["POST"])
def auth_login_code():
    """
    手机号 + 验证码登录（家长/志愿者）。
    家长：按手机号匹配已有报名记录自动绑定，只看到自己家庭；无匹配记录则拒绝。
    志愿者：注册并审核通过（status=启用）后才赋予志愿者角色。
    """
    data = request.get_json(force=True) or {}
    phone = (data.get("phone") or "").strip()
    code = (data.get("code") or "").strip()
    db = get_db()
    vr = db.execute("SELECT * FROM verification_codes WHERE phone = ?", (phone,)).fetchone()
    valid = vr and vr["code"] == code and (datetime.now() - datetime.strptime(vr["created_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() < 600
    if not valid:
        return jsonify({"error": "验证码错误或已过期（5 分钟内有效）"}), 401

    row = db.execute("SELECT * FROM users WHERE phone = ? ORDER BY id LIMIT 1", (phone,)).fetchone()
    user = dict(row) if row else None

    if not user:
        # 首次验证码登录：家长按报名记录自动绑定建号；其余身份提示先注册
        matched = db.execute("SELECT COUNT(*) FROM enrollments WHERE phone = ?", (phone,)).fetchone()[0]
        if matched:
            db.execute(
                "INSERT INTO users (username, display_name, role, phone, status, created_at, updated_at)"
                " VALUES (?, ?, 'parent', ?, '启用', ?, ?)",
                (phone, f"家长{phone[-4:]}", phone, now_str(), now_str()),
            )
            db.commit()
            user = dict(db.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone())
            log_audit("parent_auto_bind", "user", user["id"],
                      f"家长首次验证码登录，按手机号匹配到 {matched} 条报名记录并自动绑定", user=user)
        else:
            return jsonify({"error": "该手机号暂无报名记录，无法作为家长登录；如需其他身份请先注册并等待审核"}), 403
    if user["status"] == "待审核":
        return jsonify({"error": "账号待审核，审核通过后才能登录使用", "status": user["status"]}), 403
    if user["status"] == "已驳回":
        return jsonify({"error": f"注册申请已驳回：{user.get('reject_reason') or '不符合条件'}", "status": user["status"]}), 403
    if user["status"] != "启用":
        return jsonify({"error": f"账号状态：{user['status']}，请联系管理员", "status": user["status"]}), 403
    return _login_user(user)


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    """
    注册申请（身份只是「申请意向」，实际角色由审核人确认）：
    - 家长/志愿者：姓名 + 手机号
    - 社工/老师：姓名 + 手机号 + 所属机构 + 工号/岗位
    - 管理员：姓名 + 手机号 + 所属机构 + 机构邀请码
    注册后进入「待审核」，不能使用任何管理功能。
    """
    data = request.get_json(force=True) or {}
    apply_role = (data.get("apply_role") or "").strip()
    display_name = (data.get("display_name") or "").strip()
    phone = (data.get("phone") or "").strip()
    org_name = (data.get("org_name") or "").strip()
    staff_no = (data.get("staff_no") or "").strip()
    invite_code = (data.get("invite_code") or "").strip()

    if apply_role not in ("volunteer", "social_worker", "admin"):
        return jsonify({"error": "注册身份仅支持：志愿者 / 社工老师 / 管理员（家长无需注册，用手机验证码直接登录）"}), 400
    if not display_name:
        return jsonify({"error": "请填写姓名"}), 400
    if not re.fullmatch(r"1\d{10}", phone):
        return jsonify({"error": "请输入 11 位手机号"}), 400
    if apply_role == "social_worker" and (not org_name or not staff_no):
        return jsonify({"error": "社工/老师注册需填写所属机构与工号/岗位"}), 400
    if apply_role == "admin" and not org_name:
        return jsonify({"error": "管理员注册需填写所属机构"}), 400

    db = get_db()
    if apply_role == "admin":
        # Demo 邀请码：RAINBOW-2026（README 中说明）；无效邀请码直接拒绝
        if invite_code != "RAINBOW-2026":
            return jsonify({"error": "机构邀请码无效，请联系机构负责人获取"}), 400
    if db.execute("SELECT id FROM users WHERE username = ?", (phone,)).fetchone():
        return jsonify({"error": "该手机号已提交过注册或已存在账号"}), 400

    db.execute(
        "INSERT INTO users (username, display_name, role, phone, status, apply_role, org_name, staff_no, created_at, updated_at)"
        " VALUES (?, ?, '', ?, '待审核', ?, ?, ?, ?, ?)",
        (phone, display_name, phone, apply_role, org_name, staff_no, now_str(), now_str()),
    )
    db.commit()
    uid = db.execute("SELECT id FROM users WHERE username = ?", (phone,)).fetchone()["id"]
    log_audit("register", "user", uid,
              f"提交注册申请：姓名 {display_name}，手机 {phone}，申请身份 {ROLE_LABELS.get(apply_role, apply_role)}"
              + (f"，机构 {org_name}" + (f"，工号 {staff_no}" if staff_no else "") if org_name else ""),
              user={"id": uid, "username": phone})
    return jsonify({"data": {"id": uid}, "message": "注册申请已提交，等待管理员审核（审核通过后才能登录使用）"}), 201


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    """当前登录用户 + 角色权限（未登录返回匿名；待审核账号可查自己的状态）"""
    user = current_user()
    if not user:
        return jsonify({"data": {"authenticated": False}})
    perms = {k: has_perm(user, k) for k, _ in PERMISSION_KEYS}
    return jsonify({
        "data": {
            "authenticated": user["status"] == "启用",
            "id": user["id"], "username": user["username"], "display_name": user["display_name"],
            "role": user["role"], "role_label": ROLE_LABELS.get(user["role"], user["role"]),
            "phone": user["phone"], "status": user["status"],
            "reject_reason": user.get("reject_reason", ""),
            "permissions": perms,
        }
    })


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    user = current_user()
    if user:
        log_audit("logout", "user", user["id"], "退出登录", user=user)
    session.clear()
    return jsonify({"message": "已退出登录"})


# --------------------------- 任务5：账号管理 / 权限矩阵 / 审计（管理员） ---------------------------

def _user_dict(u):
    d = dict(u)
    d.pop("password_hash", None)
    return d


@app.route("/api/users", methods=["GET"])
@require_perm("manage_users")
def list_users():
    """账号列表 + 待审核队列（管理员）"""
    status = request.args.get("status", "").strip()
    db = get_db()
    if status:
        rows = db.execute("SELECT * FROM users WHERE status = ? ORDER BY id DESC", (status,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    pending = db.execute("SELECT COUNT(*) FROM users WHERE status = '待审核'").fetchone()[0]
    return jsonify({"data": [_user_dict(r) for r in rows], "total": len(rows), "pending": pending})


@app.route("/api/users/<int:user_id>/review", methods=["POST"])
@require_perm("manage_users")
def review_user(user_id):
    """
    审核注册申请：通过（审核人确认实际角色，可与申请意向不同）/ 驳回（必填原因）。
    写审计：register_review / role_change。
    """
    data = request.get_json(force=True) or {}
    action = (data.get("action") or "").strip()
    reason = (data.get("reason") or "").strip()
    role = (data.get("role") or "").strip()
    reviewer = g.current_user

    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return jsonify({"error": "账号不存在"}), 404
    target = dict(row)
    if target["status"] not in ("待审核", "已驳回"):
        return jsonify({"error": f"该账号当前状态为「{target['status']}」，无需审核"}), 400

    if action == "approve":
        new_role = role or target["apply_role"]
        if new_role not in ROLES:
            return jsonify({"error": "请确认实际角色"}), 400
        username = target["username"] if target["username"] else f"u{user_id}"
        db.execute(
            "UPDATE users SET status = '启用', role = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ? WHERE id = ?",
            (new_role, reviewer["display_name"] or reviewer["username"], now_str(), now_str(), user_id),
        )
        db.commit()
        log_audit("register_review", "user", user_id,
                  f"审核通过：{target['display_name']}（申请 {ROLE_LABELS.get(target['apply_role'], target['apply_role'])}"
                  f" → 确认角色 {ROLE_LABELS.get(new_role, new_role)}）", user=reviewer)
        if new_role != target["apply_role"]:
            log_audit("role_change", "user", user_id,
                      f"审核时调整角色：申请 {ROLE_LABELS.get(target['apply_role'], target['apply_role'])}"
                      f" → 实际授予 {ROLE_LABELS.get(new_role, new_role)}", user=reviewer)
        u = dict(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
        return jsonify({"data": _user_dict(u), "message": f"已通过审核，授予角色「{ROLE_LABELS.get(new_role, new_role)}」"})
    elif action == "reject":
        if not reason:
            return jsonify({"error": "驳回必须填写原因"}), 400
        db.execute(
            "UPDATE users SET status = '已驳回', reject_reason = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ? WHERE id = ?",
            (reason, reviewer["display_name"] or reviewer["username"], now_str(), now_str(), user_id),
        )
        db.commit()
        log_audit("register_review", "user", user_id,
                  f"驳回注册申请：{target['display_name']}，原因：{reason}", user=reviewer)
        return jsonify({"message": "已驳回注册申请"})
    return jsonify({"error": "action 仅支持 approve / reject"}), 400


@app.route("/api/users/<int:user_id>", methods=["PUT"])
@require_perm("manage_users")
def update_user(user_id):
    """
    账号管理（管理员）：改角色 / 启停 / 重置口令 / 改姓名与手机。
    角色变更与停用为关键操作：前端二次确认 + 后端写审计（role_change / user_status / password_reset）。
    """
    data = request.get_json(force=True) or {}
    reviewer = g.current_user
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return jsonify({"error": "账号不存在"}), 404
    target = dict(row)
    updates, audits = [], []

    if "role" in data and data["role"] and data["role"] != target["role"]:
        if data["role"] not in ROLES:
            return jsonify({"error": "角色非法"}), 400
        if target["id"] == reviewer["id"]:
            return jsonify({"error": "不能修改自己的角色"}), 400
        updates.append(("role", data["role"]))
        audits.append(("role_change", f"角色变更：{target['display_name']} {ROLE_LABELS.get(target['role'], target['role'])}"
                                      f" → {ROLE_LABELS.get(data['role'], data['role'])}"))
    if "status" in data and data["status"] and data["status"] != target["status"]:
        if data["status"] not in ("启用", "停用"):
            return jsonify({"error": "状态仅支持 启用/停用（待审核/已驳回走审核流程）"}), 400
        if target["id"] == reviewer["id"]:
            return jsonify({"error": "不能停用自己的账号"}), 400
        updates.append(("status", data["status"]))
        audits.append(("user_status", f"账号{data['status']}：{target['display_name']}（{target['username']}）"))
    if data.get("password"):
        updates.append(("password_hash", hash_password(data["password"])))
        audits.append(("password_reset", f"重置口令：{target['display_name']}（{target['username']}）"))
    if data.get("display_name"):
        updates.append(("display_name", data["display_name"].strip()))
    if "phone" in data and data["phone"]:
        updates.append(("phone", data["phone"].strip()))
    if not updates:
        return jsonify({"error": "没有需要更新的字段"}), 400

    set_sql = ", ".join(f"{k} = ?" for k, _ in updates)
    db.execute(f"UPDATE users SET {set_sql}, updated_at = ? WHERE id = ?",
               [v for _, v in updates] + [now_str(), user_id])
    db.commit()
    for action, detail in audits:
        log_audit(action, "user", user_id, detail, user=reviewer)
    u = dict(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
    return jsonify({"data": _user_dict(u), "message": "已保存" + ("；" + "；".join(d for _, d in audits) if audits else "")})


@app.route("/api/users", methods=["POST"])
@require_perm("manage_users")
def create_user():
    """后台直接开通账号（如机构内部名单创建社工；管理员由后台开通）"""
    data = request.get_json(force=True) or {}
    username = (data.get("username") or "").strip()
    role = (data.get("role") or "").strip()
    display_name = (data.get("display_name") or "").strip()
    if not username or role not in ROLES or not display_name:
        return jsonify({"error": "用户名/角色/姓名必填"}), 400
    if role == "parent":
        return jsonify({"error": "家长无需开通账号：家长用手机验证码登录，系统按手机号自动绑定报名"}), 400
    db = get_db()
    if db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
        return jsonify({"error": "用户名已存在"}), 400
    db.execute(
        "INSERT INTO users (username, password_hash, display_name, role, phone, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, '启用', ?, ?)",
        (username, hash_password(data.get("password") or DEFAULT_NEW_USER_PASSWORD), display_name,
         role, data.get("phone", ""), now_str(), now_str()),
    )
    db.commit()
    uid = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    log_audit("user_created", "user", uid,
              f"后台开通账号：{display_name}（{username}，角色 {ROLE_LABELS.get(role, role)}）", user=g.current_user)
    return jsonify({"data": _user_dict(db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()),
                    "message": "账号已开通"}), 201


@app.route("/api/role_permissions", methods=["GET"])
@require_perm("manage_users")
def get_role_permissions():
    db = get_db()
    rows = db.execute("SELECT * FROM role_permissions").fetchall()
    matrix = {role: {k: False for k, _ in PERMISSION_KEYS} for role in ROLES}
    for r in rows:
        if r["role"] in matrix and r["perm"] in matrix[r["role"]]:
            matrix[r["role"]][r["perm"]] = bool(r["allowed"])
    return jsonify({"data": matrix})


@app.route("/api/role_permissions", methods=["PUT"])
@require_perm("manage_users")
def update_role_permissions():
    """权限矩阵配置（关键操作：写审计 + 前端二次确认）"""
    data = request.get_json(force=True) or {}
    matrix = data.get("matrix") or {}
    db = get_db()
    changes = []
    for role, perms in matrix.items():
        if role not in ROLES:
            continue
        for perm, allowed in perms.items():
            if perm not in dict(PERMISSION_KEYS):
                continue
            row = db.execute("SELECT allowed FROM role_permissions WHERE role = ? AND perm = ?", (role, perm)).fetchone()
            old = bool(row["allowed"]) if row else False
            if old != bool(allowed):
                changes.append((role, perm, 1 if allowed else 0,
                                f"{ROLE_LABELS.get(role, role)} · {dict(PERMISSION_KEYS).get(perm, perm)}：{'开' if old else '关'} → {'开' if allowed else '关'}"))
            db.execute(
                "INSERT INTO role_permissions (role, perm, allowed) VALUES (?, ?, ?)"
                " ON CONFLICT(role, perm) DO UPDATE SET allowed = ?",
                (role, perm, 1 if allowed else 0, 1 if allowed else 0),
            )
    db.commit()
    if changes:
        log_audit("permission_change", "role_permissions", "",
                  f"权限矩阵更新 {len(changes)} 项：" + "；".join(c[3] for c in changes), user=g.current_user)
    return jsonify({"message": f"权限矩阵已保存（变更 {len(changes)} 项）" if changes else "权限矩阵无变化"})


@app.route("/api/audit_logs", methods=["GET"])
@require_perm("manage_users")
def list_audit_logs():
    """审计日志查询（管理员）：注册/审核/登录/权限变更/敏感字段查看与修改/合并复核"""
    action = request.args.get("action", "").strip()
    page = max(1, int(request.args.get("page", "1")))
    per_page = min(100, max(1, int(request.args.get("per_page", "50"))))
    db = get_db()
    where, params = "", []
    if action:
        where = "WHERE action = ?"
        params.append(action)
    total = db.execute(f"SELECT COUNT(*) FROM audit_logs {where}", params).fetchone()[0]
    rows = db.execute(
        f"SELECT * FROM audit_logs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    actions = [r["action"] for r in db.execute("SELECT DISTINCT action FROM audit_logs").fetchall()]
    return jsonify({"data": [dict(r) for r in rows], "total": total, "actions": actions})


# --------------------------- 任务5：敏感字段（分级可见 + 查看审计） ---------------------------

@app.route("/api/enrollments/<int:enrollment_id>/sensitive", methods=["GET"])
@require_perm("view_sensitive")
def get_sensitive(enrollment_id):
    """
    敏感字段查看（过敏原/情绪触发点/如厕习惯）：仅社工/管理员。
    每次查看写审计（谁、何时、看了谁的）——志愿者前端无此入口，直接调接口也 403。
    """
    db = get_db()
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not row:
        return jsonify({"error": "记录不存在"}), 404
    d = row_to_dict(row)
    log_audit("view_sensitive", "enrollment", enrollment_id,
              f"查看敏感字段：{d.get('student_real_name') or d.get('student_nickname') or '#' + str(enrollment_id)}"
              f"（过敏原/情绪触发点/如厕习惯）", user=g.current_user)
    data = {col: d.get(col, "") for col, _label in SENSITIVE_FIELDS}
    try:
        data["service_tips"] = json.loads(d.get("service_tips") or "{}")  # 解析为对象，供前端直接编辑
    except Exception:
        data["service_tips"] = {}
    return jsonify({"data": data})


@app.route("/api/enrollments/<int:enrollment_id>/sensitive", methods=["PUT"])
@require_perm("view_sensitive")
def update_sensitive(enrollment_id):
    """敏感字段修改（仅社工/管理员），写审计。service_tips（服务提示）同级别管理。"""
    data = request.get_json(force=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not row:
        return jsonify({"error": "记录不存在"}), 404
    old = row_to_dict(row)
    updates, audited = [], []
    for col, label in SENSITIVE_FIELDS:
        if col in data:
            updates.append((col, (data.get(col) or "").strip()))
            audited.append(f"{label}")
    if "service_tips" in data:
        tips = data.get("service_tips")
        if not isinstance(tips, dict):
            return jsonify({"error": "service_tips 需为对象"}), 400
        clean = {k: str(v)[:200] for k, v in tips.items() if k in SERVICE_TIP_FIELDS and str(v).strip()}
        updates.append(("service_tips", json.dumps(clean, ensure_ascii=False)))
        audited.append("服务提示")
    if not updates:
        return jsonify({"error": "没有需要更新的字段"}), 400
    set_sql = ", ".join(f"{k} = ?" for k, _ in updates)
    db.execute(f"UPDATE enrollments SET {set_sql}, updated_at = ? WHERE id = ?",
               [v for _, v in updates] + [now_str(), enrollment_id])
    write_log(db, enrollment_id, old.get("import_batch_id", ""), "update",
              field_name="sensitive", old_value="（原值略）", new_value="、".join(audited),
              tag="敏感/提示字段更新", operator=g.current_user["display_name"] or g.current_user["username"])
    db.commit()
    log_audit("update_sensitive", "enrollment", enrollment_id,
              f"修改 {'、'.join(audited)}：{old.get('student_real_name') or old.get('student_nickname') or '#' + str(enrollment_id)}",
              user=g.current_user)
    return jsonify({"message": f"已更新：{'、'.join(audited)}"})


# --------------------------- 任务5：志愿者-孩子配对管理（社工/管理员） ---------------------------

@app.route("/api/assignments", methods=["GET"])
@require_perm("attendance_callback")
def list_assignments():
    """配对列表（按活动查看；管理员/社工）"""
    activity_id = request.args.get("activity_id", "").strip()
    db = get_db()
    where, params = "", []
    if activity_id:
        where = "WHERE activity_id = ?"
        params.append(int(activity_id))
    rows = db.execute(f"SELECT * FROM volunteer_assignments {where} ORDER BY id DESC", params).fetchall()
    users_map = {u["id"]: dict(u) for u in db.execute("SELECT * FROM users").fetchall()}
    enr_map = {e["id"]: dict(e) for e in db.execute("SELECT * FROM enrollments").fetchall()}
    data = []
    for a in rows:
        u = users_map.get(a["volunteer_user_id"], {})
        e = enr_map.get(a["enrollment_id"], {})
        data.append({
            "id": a["id"], "activity_id": a["activity_id"],
            "volunteer_user_id": a["volunteer_user_id"],
            "volunteer_name": u.get("display_name", "") or u.get("username", ""),
            "enrollment_id": a["enrollment_id"],
            "child_name": (e.get("student_real_name") or e.get("student_nickname") or f"#{a['enrollment_id']}"),
            "note": a["note"], "created_at": a["created_at"],
        })
    return jsonify({"data": data, "total": len(data)})


@app.route("/api/assignments", methods=["POST"])
@require_perm("attendance_callback")
def create_assignment():
    """给志愿者分配孩子（配对后志愿者端才能看到该孩子的服务提示并签到）"""
    data = request.get_json(force=True) or {}
    activity_id = data.get("activity_id")
    volunteer_user_id = data.get("volunteer_user_id")
    enrollment_id = data.get("enrollment_id")
    note = (data.get("note") or "").strip()
    db = get_db()
    if not get_activity(db, activity_id):
        return jsonify({"error": "活动不存在"}), 404
    vol = db.execute("SELECT * FROM users WHERE id = ?", (volunteer_user_id,)).fetchone()
    if not vol or vol["role"] != "volunteer" or vol["status"] != "启用":
        return jsonify({"error": "志愿者账号不存在或未启用（需审核通过的志愿者才能配对）"}), 400
    enr = db.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not enr:
        return jsonify({"error": "报名记录不存在"}), 404
    if (enr["activity_id"] or 0) != int(activity_id):
        return jsonify({"error": "报名不属于该活动，无法配对"}), 400
    dup = db.execute(
        "SELECT id FROM volunteer_assignments WHERE activity_id = ? AND volunteer_user_id = ? AND enrollment_id = ?",
        (activity_id, volunteer_user_id, enrollment_id),
    ).fetchone()
    if dup:
        return jsonify({"error": "该配对已存在"}), 400
    db.execute(
        "INSERT INTO volunteer_assignments (activity_id, volunteer_user_id, enrollment_id, note, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (activity_id, volunteer_user_id, enrollment_id, note,
         g.current_user["display_name"] or g.current_user["username"], now_str()),
    )
    db.commit()
    aid = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    log_audit("assignment_created", "assignment", aid,
              f"配对志愿者：{vol['display_name']} ↔ {enr['student_real_name'] or enr['student_nickname']}"
              f"（活动 #{activity_id}）", user=g.current_user)
    return jsonify({"data": {"id": aid}, "message": "配对成功，志愿者端即可看到该孩子的服务提示"}), 201


@app.route("/api/assignments/<int:assignment_id>", methods=["DELETE"])
@require_perm("attendance_callback")
def delete_assignment(assignment_id):
    db = get_db()
    row = db.execute("SELECT * FROM volunteer_assignments WHERE id = ?", (assignment_id,)).fetchone()
    if not row:
        return jsonify({"error": "配对不存在"}), 404
    db.execute("DELETE FROM volunteer_assignments WHERE id = ?", (assignment_id,))
    db.commit()
    log_audit("assignment_deleted", "assignment", assignment_id,
              f"解除配对 #{assignment_id}（活动 #{row['activity_id']}）", user=g.current_user)
    return jsonify({"message": "已解除配对"})


# --------------------------- 任务5：志愿者端（配对 + 服务提示 + 签到） ---------------------------

def _volunteer_enrollment_view(db, assignment, activity):
    """志愿者视角的配对孩子视图：白名单字段 + 按活动配置过滤服务提示；敏感字段绝不下发"""
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (assignment["enrollment_id"],)).fetchone()
    if not row:
        return None
    e = row_to_dict(row)
    enabled = parse_tags(activity.get("tip_fields") or "[]")
    tips_all = {}
    try:
        tips_all = json.loads(e.get("service_tips") or "{}")
    except Exception:
        tips_all = {}
    return {
        "assignment_id": assignment["id"],
        "enrollment_id": e["id"],
        "student_real_name": e.get("student_real_name") or "（空）",
        "student_nickname": e.get("student_nickname") or "—",
        "parent_name": e.get("parent_name") or "—",
        "parent_phone_masked": (e.get("phone") or "")[:3] + "****" + (e.get("phone") or "")[-4:] if e.get("phone") else "—",
        "attendance_status": e.get("attendance_status") or "",
        "attendance_note": e.get("attendance_note") or "",
        "service_tips": {k: tips_all.get(k, "") for k in enabled if k in SERVICE_TIP_FIELDS},
        "sensitive_locked": True,
    }


@app.route("/api/volunteer/my_assignments", methods=["GET"])
@require_perm("volunteer_portal")
def volunteer_my_assignments():
    """志愿者：我的活动安排 + 配对孩子的服务提示（只看活动启用的提示维度）"""
    user = g.current_user
    db = get_db()
    rows = db.execute(
        "SELECT * FROM volunteer_assignments WHERE volunteer_user_id = ? ORDER BY activity_id, id",
        (user["id"],),
    ).fetchall()
    grouped = {}
    for a in rows:
        activity = get_activity(db, a["activity_id"])
        if not activity:
            continue
        key = activity["id"]
        if key not in grouped:
            grouped[key] = {"activity": activity_to_dict(activity), "children": []}
        view = _volunteer_enrollment_view(db, a, activity)
        if view:
            view["note"] = a["note"]
            grouped[key]["children"].append(view)
    return jsonify({"data": list(grouped.values()), "total": len(rows)})


@app.route("/api/volunteer/attendance", methods=["POST"])
@require_perm("volunteer_portal")
def volunteer_attendance():
    """志愿者签到（限本人配对孩子）：到场/未到场/临时到场；写报名留痕 + 审计"""
    data = request.get_json(force=True) or {}
    assignment_id = data.get("assignment_id")
    status = (data.get("status") or "").strip()
    note = (data.get("note") or "").strip()
    user = g.current_user
    if status not in ATTENDANCE_STATUSES:
        return jsonify({"error": f"签到状态非法，仅支持：{'、'.join(ATTENDANCE_STATUSES)}"}), 400
    db = get_db()
    a = db.execute("SELECT * FROM volunteer_assignments WHERE id = ? AND volunteer_user_id = ?",
                   (assignment_id, user["id"])).fetchone()
    if not a:
        return jsonify({"error": "配对不存在（只能操作分配给你的孩子）"}), 403
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (a["enrollment_id"],)).fetchone()
    if not row:
        return jsonify({"error": "报名记录不存在"}), 404
    enr = row_to_dict(row)
    now = now_str()
    db.execute(
        "UPDATE enrollments SET attendance_status = ?, attendance_note = ?, attendance_time = ?, attendance_operator = ? WHERE id = ?",
        (status, note, now, f"{user['display_name']}(志愿者)", a["enrollment_id"]),
    )
    write_log(db, a["enrollment_id"], enr.get("import_batch_id", ""), "attendance",
              field_name="attendance_status",
              old_value=enr.get("attendance_status", "") or "（未记录）", new_value=status,
              tag=note, operator=f"{user['display_name']}(志愿者)")
    db.commit()
    log_audit("volunteer_attendance", "enrollment", a["enrollment_id"],
              f"志愿者签到回传：{enr.get('student_real_name') or enr.get('student_nickname')} → {status}", user=user)
    return jsonify({"message": f"已记录签到结果：{status}"})


# --------------------------- 任务5：家长端（本人报名 + 确认 + 授权） ---------------------------

@app.route("/api/parent/my_enrollments", methods=["GET"])
@require_perm("parent_portal")
def parent_my_enrollments():
    """家长：只看本人（手机号匹配）的报名信息，含活动与服务提示；敏感字段家长可见（信息由家长提供）"""
    user = g.current_user
    db = get_db()
    rows = db.execute("SELECT * FROM enrollments WHERE phone = ? ORDER BY id DESC", (user["phone"],)).fetchall()
    data = []
    activity_names = get_activity_name_map(db)
    for r in rows:
        d = row_to_dict(r)
        d["activity_name"] = activity_names.get(d.get("activity_id") or 0, "")
        data.append(d)
    return jsonify({"data": data, "total": len(data)})


@app.route("/api/parent/confirm", methods=["POST"])
@require_perm("parent_portal")
def parent_confirm():
    """家长确认报名信息无误 / 授权（如照片使用授权）；校验报名手机号与登录家长一致；写留痕 + 审计"""
    data = request.get_json(force=True) or {}
    eid = data.get("id")
    confirmed = bool(data.get("confirmed"))
    authorized = bool(data.get("authorized"))
    user = g.current_user
    db = get_db()
    row = db.execute("SELECT * FROM enrollments WHERE id = ?", (eid,)).fetchone()
    if not row:
        return jsonify({"error": "记录不存在"}), 404
    enr = row_to_dict(row)
    if (enr.get("phone") or "") != user["phone"]:
        return jsonify({"error": "只能操作本人家庭的报名记录"}), 403
    now = now_str()
    if confirmed:
        db.execute("UPDATE enrollments SET parent_confirmed = 1, parent_confirmed_at = ? WHERE id = ?", (now, eid))
    if authorized:
        db.execute("UPDATE enrollments SET parent_authorized = 1, parent_authorized_at = ? WHERE id = ?", (now, eid))
    write_log(db, eid, enr.get("import_batch_id", ""), "parent_confirm",
              field_name="parent_confirm",
              old_value="", new_value=("确认" if confirmed else "") + ("+授权" if authorized else ""),
              tag=f"家长 {user['display_name']}", operator=user["display_name"] or user["phone"])
    db.commit()
    log_audit("parent_confirm", "enrollment", eid,
              f"家长{'确认报名信息' if confirmed else ''}{'并授权' if authorized else ''}：{enr.get('student_real_name') or enr.get('student_nickname')}",
              user=user)
    return jsonify({"message": "已确认" + ("并完成授权" if authorized else "") if confirmed else "已完成授权"})


# --------------------------- 集成模块（门户 / 归档台 / 童颜守护） ---------------------------

from modules_ext import register_modules  # noqa: E402

register_modules(app)


# --------------------------- 演示账号（仅显式开启时可用） ---------------------------

@app.route("/api/demo-accounts")
def demo_accounts():
    """返回演示账号；凭据全部来自配置/环境变量，源码内不硬编码。"""
    if not ENABLE_DEMO_LOGIN:
        return jsonify({"enabled": False, "accounts": []})
    return jsonify({
        "enabled": True,
        "accounts": [
            {"kind": "admin", "username": "admin", "password": SEED_ADMIN_PASSWORD},
            {"kind": "social_worker", "username": "worker", "password": SEED_WORKER_PASSWORD},
            {"kind": "volunteer", "phone": DEMO_VOLUNTEER_PHONE},
            {"kind": "parent", "phone": DEMO_PARENT_PHONE},
        ],
    })

# --------------------------- 启动入口 ---------------------------

if __name__ == "__main__":
    init_db()
    if ENABLE_DEMO_LOGIN:
        print("演示登录已开启；账号见 /api/demo-accounts（生产请设置 ENABLE_DEMO_LOGIN=0 并覆盖演示口令）")

    # 清理 24 小时前遗留的未入库预览
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "DELETE FROM import_previews WHERE updated_at < datetime('now', '-1 day')"
    )
    conn.commit()
    conn.close()

    # use_reloader=False：避免项目目录里新增文件（导出、日志等）触发重启
    # PORT：云端部署时由平台注入，本地默认 5000
    app.run(host="0.0.0.0",
            port=int(os.environ.get("PORT", 5000)),
            debug=False,
            use_reloader=False,
            threaded=True)

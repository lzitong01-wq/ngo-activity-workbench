# -*- coding: utf-8 -*-
"""
示例机构报名 Demo 模拟数据脚本
直接写入 SQLite，生成覆盖 4 类来源 + 多种异常的测试数据
"""

import json
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from app import DB_PATH, ROLES, SOURCE_TYPES, detect_anomalies, hash_password, init_db
from config import (
    DEMO_ADMIN_PHONE, DEMO_PARENT_PHONE, DEMO_PENDING_PHONE, DEMO_VOLUNTEER_PHONE,
    DEMO_WORKER_PHONE, ORG_NAME, SEED_ADMIN_PASSWORD, SEED_WORKER_PASSWORD,
)

SEED_DATA = [
    {
        "student_real_name": "示例儿童A",
        "student_nickname": "小A",
        "parent_name": "家长A",
        "phone": "10000000011",
        "relation": "母亲",
        "source_type": "miniapp",
        "source_user": "家长群消息",
        "source_time": "2026-10-07 09:12:00",
    },
    {
        "student_real_name": "示例儿童B",
        "student_nickname": "小B",
        "parent_name": "",  # 缺失，触发待补全+信息缺失
        "phone": "10000000013",
        "relation": "",
        "source_type": "group_signup",
        "source_user": "代报老师",
        "source_time": "2026-10-06 20:30:00",
        "import_batch_id": "BATCH-20261006-001",
    },
    {
        "student_real_name": "",  # 只有小名
        "student_nickname": "小C",
        "parent_name": "家长B",
        "phone": "10000000008",
        "relation": "母亲",
        "source_type": "teacher_proxy",
        "source_user": "代报老师",
        "source_time": "2026-10-07 10:05:00",
    },
    {
        "student_real_name": "示例儿童A",
        "student_nickname": "",
        "parent_name": "家长A",
        "phone": "10000000011",  # 与 #1 重复
        "relation": "母亲",
        "source_type": "phone_private",
        "source_user": "社工录入",
        "source_time": "2026-10-07 11:00:00",
    },
    {
        "student_real_name": "示例儿童C",
        "student_nickname": "小E",
        "parent_name": "家长C",
        "phone": "10000000004",
        "relation": "母亲",
        "source_type": "miniapp",
        "source_user": "家长群消息",
        "source_time": "2026-10-07 09:40:00",
    },
    {
        "student_real_name": "",  # 只有小名
        "student_nickname": "小D",
        "parent_name": "",  # 缺失
        "phone": "10000000003",
        "relation": "",
        "source_type": "group_signup",
        "source_user": "群接龙消息",
        "source_time": "2026-10-06 21:10:00",
        "import_batch_id": "BATCH-20261006-001",
    },
    {
        "student_real_name": "示例儿童D",
        "student_nickname": "小F",
        "parent_name": "家长D",
        "phone": "10000000002",
        "relation": "父亲",
        "source_type": "teacher_proxy",
        "source_user": "代报老师",
        "source_time": "2026-10-07 10:30:00",
    },
    {
        "student_real_name": "示例儿童B",
        "student_nickname": "小B",
        "parent_name": "家长E",
        "phone": "10000000013",  # 与 #2 重复
        "relation": "父亲",
        "source_type": "miniapp",
        "source_user": "家长群消息",
        "source_time": "2026-10-07 08:50:00",
    },
]


# 任务3.5：预置活动——8 条模拟报名默认归属「演示活动」，另备一个已结束活动演示状态流转
SEED_ACTIVITIES = [
    {
        "name": "2026 秋季融合体验营",
        "activity_time": "2026-10-20 09:30 ~ 11:30",
        "location": "示例活动场地",
        "capacity": 20,
        "deadline": "2026-10-18 18:00:00",
        "status": "进行中",
        "note": "本次 8 条模拟报名默认归属该活动",
        # 任务5：本活动启用的志愿者服务提示维度（未勾选的维度志愿者端不显示）
        "tip_fields": ["陪护重点", "沟通方式", "安全注意事项"],
    },
    {
        "name": "2026 春季亲子运动会",
        "activity_time": "2026-04-18 09:00 ~ 12:00",
        "location": "中心一楼多功能厅",
        "capacity": 30,
        "deadline": "2026-04-16 18:00:00",
        "status": "已结束",
        "note": "已结束活动：不能再接入新报名（用于演示状态校验）",
        "tip_fields": [],
    },
]

DEMO_ACTIVITY_NAME = SEED_ACTIVITIES[0]["name"]


def seed_activities():
    """写入预置活动（可重复执行，先清空），返回演示活动 id"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM activities")
    cursor.execute("DELETE FROM sqlite_sequence WHERE name='activities'")
    conn.commit()

    now = "2026-10-01 10:00:00"
    ids = {}
    for a in SEED_ACTIVITIES:
        cursor.execute(
            """
            INSERT INTO activities (name, activity_time, location, capacity, deadline, status, note, tip_fields, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                a["name"], a["activity_time"], a["location"], a["capacity"],
                a["deadline"], a["status"], a["note"],
                json.dumps(a.get("tip_fields", []), ensure_ascii=False),
                now, now,
            ),
        )
        ids[a["name"]] = cursor.lastrowid
    conn.commit()
    conn.close()
    print(f"✅ 已写入 {len(SEED_ACTIVITIES)} 个活动（演示活动：{DEMO_ACTIVITY_NAME}）")
    return ids[DEMO_ACTIVITY_NAME]


def seed():
    init_db()
    demo_activity_id = seed_activities()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 先清空已有数据（演示用，可重复执行）
    cursor.execute("DELETE FROM enrollments")
    cursor.execute("DELETE FROM sqlite_sequence WHERE name='enrollments'")
    conn.commit()

    existing = []
    now = "2026-10-07 16:25:00"

    # 任务4：预置签到回传演示数据（按小名匹配；体验营现场已誊录部分结果，供签到表/台账回显演示）
    SEED_ATTENDANCE = {
        "小A": ("到场", "", "现场工作人员"),  # 示例儿童A
        "小B": ("临时到场", "奶奶临时送来，现场补登记", "现场工作人员"),  # 示例儿童B
        "小E": ("未到场", "当天感冒请假，已电话告知", "现场工作人员"),  # 示例儿童C
    }

    for item in SEED_DATA:
        tags = detect_anomalies(item, existing)
        status = "待处理" if tags else "已确认"

        cursor.execute(
            """
            INSERT INTO enrollments (
                student_real_name, student_nickname, parent_name, phone,
                id_card, emergency_contact, relation, source_type, source_user,
                source_time, import_batch_id, anomaly_tags, system_tags, status, note,
                activity_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.get("student_real_name", ""),
                item.get("student_nickname", ""),
                item.get("parent_name", ""),
                item.get("phone", ""),
                item.get("id_card", ""),
                item.get("emergency_contact", ""),
                item.get("relation", ""),
                item["source_type"],
                item.get("source_user", ""),
                item.get("source_time", now),
                item.get("import_batch_id"),
                json.dumps(tags, ensure_ascii=False),
                json.dumps(tags, ensure_ascii=False),
                status,
                item.get("note", ""),
                demo_activity_id,
                now,
                now,
            ),
        )
        conn.commit()

        # 任务4：写入预置签到回传
        att = SEED_ATTENDANCE.get(item.get("student_nickname", ""))
        if att:
            cursor.execute(
                """
                UPDATE enrollments
                SET attendance_status = ?, attendance_note = ?, attendance_time = ?, attendance_operator = ?
                WHERE id = ?
                """,
                (att[0], att[1], "2026-10-07 09:40:00", att[2], cursor.lastrowid),
            )
            conn.commit()

        # 任务5：写入敏感字段与服务提示演示值（敏感字段仅社工/管理员可见，志愿者端不下发）
        sens = SEED_SENSITIVE.get(item.get("student_nickname", ""))
        if sens:
            cursor.execute(
                """
                UPDATE enrollments
                SET allergy_info = ?, emotion_triggers = ?, toilet_habits = ?, service_tips = ?
                WHERE id = ?
                """,
                (
                    sens.get("allergy_info", ""),
                    sens.get("emotion_triggers", ""),
                    sens.get("toilet_habits", ""),
                    json.dumps(sens.get("service_tips", {}), ensure_ascii=False),
                    cursor.lastrowid,
                ),
            )
            conn.commit()

        # 同步到 existing 用于后续重复检测
        existing.append({
            "student_real_name": item.get("student_real_name", ""),
            "parent_name": item.get("parent_name", ""),
            "phone": item.get("phone", ""),
        })

    conn.close()
    print(f"✅ 已写入 {len(SEED_DATA)} 条模拟报名数据到 {DB_PATH}")


# 任务5：敏感字段 + 服务提示演示值（service_tips 仅含活动启用维度时志愿者端才会显示）
SEED_SENSITIVE = {
    "小A": {
        "allergy_info": "花生及坚果类（随身携带肾上腺素笔，家长放在书包外侧口袋）",
        "emotion_triggers": "突然的巨大声响（气球爆炸、扩音器啸叫）；提前告知即可缓解",
        "toilet_habits": "可自主如厕，但需每 2 小时提醒一次；不熟悉马桶时会紧张",
        "service_tips": {
            "陪护重点": "分组活动时紧随其后，避免脱离视线；加餐环节核对零食成分表",
            "沟通方式": "指令简短分步说，先叫小名再看眼睛；不用突然音量",
            "安全注意事项": "示例饮食提示社工",
        },
    },
    "小B": {
        "allergy_info": "无已知食物过敏",
        "emotion_triggers": "陌生封闭环境开始时会不安，需家长陪同适应约 10 分钟",
        "toilet_habits": "可自主如厕，无需提醒",
        "service_tips": {
            "陪护重点": "开场环节允许家长陪同，适应后引导孩子独立参与",
            "沟通方式": "多用图卡和手势提示流程",
            "安全注意事项": "注意教室门边碰撞；运动环节提前热身",
        },
    },
    "小C": {
        "allergy_info": "鸡蛋及其制品",
        "emotion_triggers": "争抢玩具时易激动，会大声哭闹",
        "toilet_habits": "如厕训练中，偶有意外，书包内有备用衣物",
        "service_tips": {
            "陪护重点": "轮流环节提前介入引导，避免争抢升级",
            "沟通方式": "平静语气+转移注意力效果好，不讲大道理",
            "安全注意事项": "加餐避开鸡蛋；意外弄脏衣物时用书包备用衣物更换",
        },
    },
    "小E": {
        "allergy_info": "牛奶",
        "emotion_triggers": "",
        "toilet_habits": "仍穿拉拉裤，需家长协助更换",
        "service_tips": {},
    },
}


# 任务3：预置儿童档案（历史信息库），与群接龙示例文本第 2/3 行形成命中闭环
SEED_PROFILES = [
    {
        "student_real_name": "钱小多",
        "student_nickname": "小多",
        "parent_name": "钱女士",
        "phone": "10000000016",
        "relation": "母亲",
        "enrollment_count": 2,
        "note": "春季班老学员",
    },
    {
        "student_real_name": "示例儿童E",
        "student_nickname": "小D",
        "parent_name": "家长F",
        "phone": "10000000013",
        "relation": "父亲",
        "enrollment_count": 1,
        "note": "",
    },
    {
        "student_real_name": "示例儿童A",
        "student_nickname": "小A",
        "parent_name": "家长A",
        "phone": "10000000011",
        "relation": "母亲",
        "enrollment_count": 2,
        "enrollment_ids": "1,4",
        "note": "小程序+电话各报过一次",
    },
]


def seed_profiles():
    """写入预置历史档案（可重复执行，先清空）"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM child_profiles")
    cursor.execute("DELETE FROM profile_logs")
    cursor.execute("DELETE FROM sqlite_sequence WHERE name IN ('child_profiles','profile_logs')")
    conn.commit()

    now = "2026-10-01 10:00:00"
    for p in SEED_PROFILES:
        cursor.execute(
            """
            INSERT INTO child_profiles (
                student_real_name, student_nickname, parent_name, phone, relation, note,
                enrollment_count, enrollment_ids, history_status, confirmed_by, confirmed_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', '历史导入', ?, ?, ?)
            """,
            (
                p["student_real_name"], p["student_nickname"], p["parent_name"],
                p["phone"], p["relation"], p.get("note", ""),
                p.get("enrollment_count", 0), p.get("enrollment_ids", ""),
                now, now, now,
            ),
        )
        pid = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO profile_logs (profile_id, operation_type, field_name, old_value, new_value, tag, operator, operated_at)
            VALUES (?, 'create', '', '', ?, '种子档案', '历史导入', ?)
            """,
            (pid, f"建档：{p['student_real_name']}（{p['student_nickname']}）/ {p['parent_name']}", now),
        )
    conn.commit()
    conn.close()
    print(f"✅ 已写入 {len(SEED_PROFILES)} 条儿童历史档案到 {DB_PATH}")


# ============================== 任务5：账号 / 权限 / 配对演示数据 ==============================

# 演示账号：全部为构造值，口令与手机号来自 config.py（环境变量可覆盖）
SEED_USERS = [
    {"username": "admin", "password": SEED_ADMIN_PASSWORD, "display_name": "管理员",
     "role": "admin", "phone": DEMO_ADMIN_PHONE, "status": "启用", "org_name": ORG_NAME, "staff_no": ""},
    {"username": "worker", "password": SEED_WORKER_PASSWORD, "display_name": "社工",
     "role": "social_worker", "phone": DEMO_WORKER_PHONE, "status": "启用",
     "org_name": ORG_NAME, "staff_no": "DEMO-001"},
    {"username": DEMO_VOLUNTEER_PHONE, "password": "", "display_name": "志愿者",
     "role": "volunteer", "phone": DEMO_VOLUNTEER_PHONE, "status": "启用",
     "org_name": "", "staff_no": "", "reviewed_by": "管理员", "reviewed_at": "2026-10-05 14:00:00"},
    {"username": DEMO_PARENT_PHONE, "password": "", "display_name": "家长（演示）",
     "role": "parent", "phone": DEMO_PARENT_PHONE, "status": "启用", "org_name": "", "staff_no": ""},
    {"username": DEMO_PENDING_PHONE, "password": "", "display_name": "待审核申请人",
     "role": "", "apply_role": "volunteer", "phone": DEMO_PENDING_PHONE,
     "status": "待审核", "org_name": "", "staff_no": ""},
]


def seed_users():
    """写入演示账号（可重复执行，先清空账号相关表）"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users")
    cursor.execute("DELETE FROM volunteer_assignments")
    cursor.execute("DELETE FROM audit_logs")
    cursor.execute("DELETE FROM verification_codes")
    cursor.execute("DELETE FROM sqlite_sequence WHERE name IN ('users','volunteer_assignments','audit_logs')")
    conn.commit()

    now = "2026-10-05 12:00:00"
    ids = {}
    for u in SEED_USERS:
        cursor.execute(
            """
            INSERT INTO users (
                username, password_hash, display_name, role, phone, status,
                apply_role, org_name, staff_no, reviewed_by, reviewed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                u["username"],
                hash_password(u["password"]) if u.get("password") else "",
                u["display_name"], u.get("role", ""), u["phone"], u["status"],
                u.get("apply_role", ""), u.get("org_name", ""), u.get("staff_no", ""),
                u.get("reviewed_by", ""), u.get("reviewed_at", ""), now, now,
            ),
        )
        ids[u["display_name"]] = cursor.lastrowid
    conn.commit()
    conn.close()
    print(f"✅ 已写入 {len(SEED_USERS)} 个演示账号（含 1 条待审核申请）到 {DB_PATH}")


def seed_assignments():
    """写入志愿者-孩子配对演示（志愿者志愿者 ↔ 体验营小A/小C）"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    act = cursor.execute("SELECT id FROM activities WHERE name = ?", (DEMO_ACTIVITY_NAME,)).fetchone()
    vol = cursor.execute("SELECT id FROM users WHERE username = '10000000006'").fetchone()
    if not act or not vol:
        conn.close()
        print("⚠️ 未找到演示活动或志愿者，跳过配对写入")
        return
    activity_id, volunteer_id = act[0], vol[0]

    # 配对目标按小名查找（体验营名下）
    pairs = [
        ("小A", "负责全程陪同与如厕提醒（每2小时）"),
        ("小C", "负责课堂轮流环节引导"),
    ]
    now = "2026-10-08 10:00:00"
    count = 0
    for nickname, note in pairs:
        enr = cursor.execute(
            "SELECT id FROM enrollments WHERE student_nickname = ? AND activity_id = ?",
            (nickname, activity_id),
        ).fetchone()
        if not enr:
            print(f"⚠️ 未找到孩子「{nickname}」，跳过")
            continue
        dup = cursor.execute(
            "SELECT id FROM volunteer_assignments WHERE activity_id = ? AND volunteer_user_id = ? AND enrollment_id = ?",
            (activity_id, volunteer_id, enr[0]),
        ).fetchone()
        if dup:
            continue
        cursor.execute(
            """
            INSERT INTO volunteer_assignments (activity_id, volunteer_user_id, enrollment_id, note, created_by, created_at)
            VALUES (?, ?, ?, ?, '社工', ?)
            """,
            (activity_id, volunteer_id, enr[0], note, now),
        )
        count += 1
    conn.commit()
    conn.close()
    print(f"✅ 已写入 {count} 条志愿者配对（志愿者 ↔ 小A/小C）到 {DB_PATH}")


if __name__ == "__main__":
    seed()
    seed_profiles()
    seed_users()
    seed_assignments()

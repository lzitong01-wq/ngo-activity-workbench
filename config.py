# -*- coding: utf-8 -*-
"""演示环境配置。

所有可变值均可通过环境变量覆盖；源码内不含真实机构信息、联系方式或凭证。
本地使用：复制 config.example.env 为 .env 后按需修改（本仓库不提交 .env）。
"""

import os
import secrets

# 会话签名密钥：未配置时每次启动随机生成（重启后会话失效，适合演示）
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# 演示账号口令：仅为本地默认值，任何非本机部署必须通过环境变量覆盖
SEED_ADMIN_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD", "demo-admin-change-me")
SEED_WORKER_PASSWORD = os.environ.get("SEED_WORKER_PASSWORD", "demo-worker-change-me")
# 管理员新建/重置账号时留空的默认口令
DEFAULT_NEW_USER_PASSWORD = os.environ.get("DEFAULT_NEW_USER_PASSWORD", "demo-user-change-me")

# 演示手机号：使用 100000000NN 测试号段，不是真实号码
DEMO_ADMIN_PHONE = os.environ.get("DEMO_ADMIN_PHONE", "10000000001")
DEMO_WORKER_PHONE = os.environ.get("DEMO_WORKER_PHONE", "10000000010")
DEMO_VOLUNTEER_PHONE = os.environ.get("DEMO_VOLUNTEER_PHONE", "10000000006")
DEMO_PARENT_PHONE = os.environ.get("DEMO_PARENT_PHONE", "10000000011")
DEMO_PENDING_PHONE = os.environ.get("DEMO_PENDING_PHONE", "10000000014")

# 是否开启「演示账号一键登录」。生产环境务必设为 0
ENABLE_DEMO_LOGIN = os.environ.get("ENABLE_DEMO_LOGIN", "1") == "1"

# 机构显示名称
ORG_NAME = os.environ.get("ORG_NAME", "示例公益机构")

# 跨域白名单；未配置时不开放跨域（同源部署无需 CORS）
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]

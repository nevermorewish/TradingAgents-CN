"""启动期引导：保证系统里至少有一个可用管理员账号。

设计意图：
- 通过 DEFAULT_ADMIN_USERNAME / DEFAULT_ADMIN_PASSWORD / DEFAULT_ADMIN_EMAIL
  环境变量配置默认管理员，部署时直接写在 .env / docker-compose 即可。
- 仅当 users 集合中没有任何管理员（is_admin=True）时才创建，
  保证不会覆盖已存在的管理员账号。
- 若 DEFAULT_ADMIN_PASSWORD 为空字符串，跳过 — 避免在不需要默认账号
  的环境（例如沿用旧库）里意外建出弱口令账号。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("app.bootstrap")


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


async def ensure_default_admin() -> None:
    """如系统内尚无管理员，依据环境变量创建一个默认管理员。"""
    from app.core.config import settings
    from app.core.database import get_mongo_db

    username = (settings.DEFAULT_ADMIN_USERNAME or "").strip()
    password = settings.DEFAULT_ADMIN_PASSWORD or ""
    email = (settings.DEFAULT_ADMIN_EMAIL or "").strip()

    if not username or not password:
        logger.info("跳过默认管理员创建：DEFAULT_ADMIN_USERNAME / DEFAULT_ADMIN_PASSWORD 未配置")
        return

    try:
        db = get_mongo_db()
    except Exception as e:
        logger.warning(f"跳过默认管理员创建：MongoDB 未就绪 ({e})")
        return

    users = db["users"]

    try:
        existing_admin = await users.find_one({"is_admin": True})
    except Exception as e:
        logger.warning(f"查询管理员失败，跳过默认管理员创建: {e}")
        return

    if existing_admin:
        logger.info(
            f"已有管理员账号 ({existing_admin.get('username')})，跳过默认管理员创建"
        )
        return

    now = datetime.utcnow()
    doc: dict[str, Any] = {
        "username": username,
        "email": email or f"{username}@tradingagents.cn",
        "name": username,
        "hashed_password": _hash(password),
        "is_admin": True,
        "is_active": True,
        "roles": ["admin"],
        "preferences": {
            "default_market": "A股",
            "default_depth": "深度",
            "default_analysts": ["市场分析师", "基本面分析师"],
            "auto_refresh": True,
            "refresh_interval": 30,
            "ui_theme": "light",
            "sidebar_width": 240,
            "language": "zh-CN",
            "notifications_enabled": True,
            "email_notifications": False,
            "desktop_notifications": True,
            "analysis_complete_notification": True,
            "system_maintenance_notification": True,
        },
        "created_at": now,
        "updated_at": now,
    }

    try:
        # username 也必须唯一；用 upsert 兜底（万一别处已建同名非 admin 账号也能就地升权）
        await users.update_one(
            {"username": username},
            {"$set": doc},
            upsert=True,
        )
        logger.info(f"✅ 已创建默认管理员账号：{username}")
    except Exception as e:
        logger.error(f"❌ 创建默认管理员失败: {e}")

"""Flora Court 面膜 ERP — 設定層（地基）

紅線 R2：獨立 schema flora_court。
- 正式環境（Postgres）：DB_SCHEMA = "flora_court"
- 本機測試（SQLite）：DB_SCHEMA = None（SQLite 不支援 schema）

切換邏輯：依 DATABASE_URL 是否為 sqlite 自動決定 schema。
"""
import os


def _detect_schema(database_url: str) -> str | None:
    """SQLite 不支援 schema → None；Postgres → flora_court。"""
    if database_url.startswith("sqlite"):
        return None
    return os.environ.get("DB_SCHEMA", "flora_court")


class Config:
    # ---- 資料庫 ----
    # 本機測試預設 SQLite（schema=None）；正式佈署設 DATABASE_URL=postgresql://... 即切 flora_court schema
    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "sqlite:///" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "flora_court.db"),
    )
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # schema：SQLite=None、Postgres=flora_court（R2）
    DB_SCHEMA = _detect_schema(DATABASE_URL)

    # ---- 安全 ----
    SECRET_KEY = os.environ.get("SECRET_KEY", "flora-court-dev-secret-change-me")

    # ---- 功能旗標（R2：愛啪啪入口可逆隱藏）----
    ENABLE_EARWAX_ENTRY = os.environ.get("ENABLE_EARWAX_ENTRY", "false").lower() == "true"
    ENABLE_MOBILE = os.environ.get("ENABLE_MOBILE", "true").lower() == "true"

    # ---- 併發策略（§4.3，全案統一）----
    # "for_update" = SELECT..FOR UPDATE；"atomic_update" = 條件式原子更新
    # SQLite 不支援 FOR UPDATE，本機測試走 atomic_update；Postgres 正式可用 for_update
    CONCURRENCY_STRATEGY = os.environ.get(
        "CONCURRENCY_STRATEGY",
        "atomic_update" if DATABASE_URL.startswith("sqlite") else "for_update",
    )

    @staticmethod
    def is_sqlite() -> bool:
        return Config.DATABASE_URL.startswith("sqlite")

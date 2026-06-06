import os

# SECRET_KEY：從環境變數讀取，未設定時使用開發用預設值（正式環境必須設定）
SECRET_KEY = os.environ.get('SECRET_KEY', 'earwax-hualin-dev-secret-change-me')

# DATABASE_URL：必須從環境變數讀取，不得寫死密碼
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError(
        "環境變數 DATABASE_URL 未設定。\n"
        "請在 .env 或 Render Dashboard 設定：\n"
        "DATABASE_URL=postgresql://USER:PASS@HOST:PORT/DB"
    )

import os
from flask import Flask, redirect, url_for, session, jsonify
from sqlalchemy import text

from config import SECRET_KEY, DATABASE_URL
from db import db, SCHEMA

# ── Blueprints ──────────────────────────────────────────────────────────────
from blueprints.auth import auth_bp
from blueprints.service import service_bp
from blueprints.customers import customers_bp
from blueprints.inventory import inventory_bp
from blueprints.purchases import purchases_bp
from blueprints.dashboard import dashboard_bp
from blueprints.mask import mask_bp

# ── App 初始化 ───────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = SECRET_KEY

# PostgreSQL 連線（DATABASE_URL 缺失時 config.py 已拋出明確錯誤，不會到達這裡）
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# 啟動時確保資料表存在（非破壞性：既有表 create_all 自動略過）
# DB 暫時不可達時包 try/except，不阻擋 app 啟動
with app.app_context():
    try:
        # 先確保 earwax schema 存在（與泰安 public 隔離），再建表
        db.session.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        db.session.commit()
        db.create_all()
    except Exception as e:
        db.session.rollback()
        print(f"[init] schema/create_all 跳過（DB 暫時不可達）: {e}")

# ── 註冊 Blueprints ──────────────────────────────────────────────────────────
# 注意：dashboard_bp 已掛載 / 路由（owner 用），必須最後註冊以免衝突
app.register_blueprint(auth_bp)
app.register_blueprint(service_bp)
app.register_blueprint(customers_bp)
app.register_blueprint(inventory_bp)
app.register_blueprint(purchases_bp)
app.register_blueprint(mask_bp)        # 面膜模組（url_prefix=/mask，blueprint 內已定義）
app.register_blueprint(dashboard_bp)   # 掛 /（dashboard.index）


# ── 健康檢查 ─────────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    """Render 健康檢查端點；同時驗證 DB 連線是否正常。"""
    try:
        from db import User
        count = User.query.count()
        return jsonify({'status': 'ok', 'users': count}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

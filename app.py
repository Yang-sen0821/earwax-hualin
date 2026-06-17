"""Flora Court 面膜 ERP — Flask 入口（地基）

註冊 auth + 6 個業務 blueprint；加 /health。
R2/D1：ENABLE_EARWAX_ENTRY=false ⇒ 不 register 任何 earwax blueprint（本 app 完全不 import earwax）。
"""
from flask import Flask, jsonify, g

from config import Config
from auth import auth_bp
from blueprints.products import products_bp
from blueprints.inventory import inventory_bp
from blueprints.orders import orders_bp
from blueprints.customers import customers_bp
from blueprints.reports import reports_bp
from blueprints.mobile import mobile_bp
from display_labels import register_display_helpers
from db import SessionLocal


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.secret_key = Config.SECRET_KEY

    # ---- 全站顯示中文化（共用對照：filter + context_processor）----
    register_display_helpers(app)

    # ---- 註冊 blueprint（auth + 6 業務）----
    app.register_blueprint(auth_bp)
    app.register_blueprint(products_bp)    # /products
    app.register_blueprint(inventory_bp)   # /inventory
    app.register_blueprint(orders_bp)      # /orders
    app.register_blueprint(customers_bp)   # /customers
    app.register_blueprint(reports_bp)     # /reports
    if Config.ENABLE_MOBILE:
        app.register_blueprint(mobile_bp)  # /m

    # ---- D1：愛啪啪入口可逆隱藏（false 時完全不註冊 earwax，不 import earwax）----
    if Config.ENABLE_EARWAX_ENTRY:
        # 預留掛回點（地基階段不掛；回復僅改 env，不動 DB）
        pass

    # ---- /health ----
    @app.route("/health")
    def health():
        return jsonify({
            "status": "ok",
            "app": "flora_court",
            "schema": Config.DB_SCHEMA,
            "db": "sqlite" if Config.is_sqlite() else "postgres",
            "earwax_entry": Config.ENABLE_EARWAX_ENTRY,
        })

    # ---- session 清理 ----
    @app.teardown_appcontext
    def cleanup(exc=None):
        SessionLocal.remove()

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

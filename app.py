"""Flora Court 面膜 ERP — Flask 入口（地基）

註冊 auth + 6 個業務 blueprint；加 /health。
R2/D1：ENABLE_EARWAX_ENTRY=false ⇒ 不 register 任何 earwax blueprint（本 app 完全不 import earwax）。
"""
from flask import Flask, jsonify, g, render_template

from config import Config
from auth import auth_bp
from blueprints.products import products_bp
from blueprints.inventory import inventory_bp
from blueprints.orders import orders_bp
from blueprints.customers import customers_bp
from blueprints.reports import reports_bp
from blueprints.earwax_sales import earwax_sales_bp
from blueprints.mobile import mobile_bp
from blueprints.audit_log import audit_bp, mobile_audit_bp
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
    app.register_blueprint(earwax_sales_bp)  # /earwax-sales（甲案：外泌體獨立紀錄）
    app.register_blueprint(audit_bp)         # /admin/audit（CR-8 操作紀錄；owner/accounting）
    if Config.ENABLE_MOBILE:
        app.register_blueprint(mobile_bp)  # /m
        app.register_blueprint(mobile_audit_bp)  # /m/audit（CR-8 手機簡版）

    # ---- D1：外泌體入口可逆隱藏（false 時完全不註冊 earwax，不 import earwax）----
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

    # ---- 中文錯誤頁（森哥 2026-08-19：員工撞到權限時看到英文 Forbidden 白頁，像網站壞掉）----
    #      手機/桌面共用 base.html；403 一律導向中文說明頁，不洩漏內部訊息。
    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html", section=""), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html", section=""), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template("errors/500.html", section=""), 500

    # ---- session 清理 ----
    @app.teardown_appcontext
    def cleanup(exc=None):
        SessionLocal.remove()

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

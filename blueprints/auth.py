# blueprints/auth.py
# 登入/登出 blueprint（auth_bp）
# 路由：/login、/logout
#
# 權限裝飾器（login_required / owner_required）定義在根目錄 auth.py，
# 此處 re-export 供其他 blueprint 從 blueprints.auth import。

from flask import (
    Blueprint, render_template, request,
    redirect, url_for, session, flash
)
from werkzeug.security import check_password_hash
from auth import login_required, owner_required  # noqa: F401（re-export）
from db import db, User

auth_bp = Blueprint("auth", __name__)


# ──────────────────────────────────────────────
# 路由
# ──────────────────────────────────────────────

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """登入頁。POST 時驗證帳密，成功後依角色導向。"""
    if "user" in session:
        # 已登入 → 直接導向首頁
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            # 將用戶資訊（含 role）寫入 session
            session["user"] = {
                "id":       user.id,
                "username": user.username,
                "name":     user.name,
                "role":     user.role,
            }
            flash(f"歡迎回來，{user.name}！")
            # owner 進儀表板，staff 進施作登錄
            if user.role == "owner":
                return redirect(url_for("dashboard.index"))
            return redirect(url_for("service.new_service"))

        flash("帳號或密碼錯誤，請再試一次。")

    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    """登出，清除 session 後回登入頁。"""
    session.clear()
    flash("已登出。")
    return redirect(url_for("auth.login"))

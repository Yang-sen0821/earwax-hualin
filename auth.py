"""Flora Court 面膜 ERP — 認證 / 授權層（地基）

- 登入 / 登出
- @role_required 裝飾器（§1.4 / §六 權限矩陣）
- 5 角色：owner / staff / warehouse / accounting / viewer

權限矩陣（§六）由各 blueprint 用 @role_required 套用對應角色集合。
"""
from functools import wraps

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session as flask_session, flash, abort, g,
)
from werkzeug.security import check_password_hash

from db import get_session, User

auth_bp = Blueprint("auth", __name__)

ROLES = ("owner", "staff", "warehouse", "accounting", "viewer")


# -------------------------------------------------------------------------
# session 使用者載入
# -------------------------------------------------------------------------
def current_user():
    if getattr(g, "_current_user", None) is not None:
        return g._current_user
    uid = flask_session.get("user_id")
    if not uid:
        g._current_user = None
        return None
    db = get_session()
    g._current_user = db.query(User).filter_by(id=uid, active=True).first()
    return g._current_user


# -------------------------------------------------------------------------
# 裝飾器
# -------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def role_required(*allowed_roles):
    """限制存取角色。owner 永遠通過（最高權限）。

    用法：@role_required("owner", "warehouse")
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("auth.login", next=request.path))
            if user.role == "owner":
                return view(*args, **kwargs)
            if allowed_roles and user.role not in allowed_roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


# -------------------------------------------------------------------------
# 登入 / 登出
# -------------------------------------------------------------------------
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_session()
        user = db.query(User).filter_by(username=username, active=True).first()
        if user and check_password_hash(user.password_hash, password):
            flask_session["user_id"] = user.id
            flask_session["role"] = user.role
            nxt = request.args.get("next") or url_for("auth.home")
            return redirect(nxt)
        flash("帳號或密碼錯誤")
    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    flask_session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/")
@login_required
def home():
    return render_template("base.html", user=current_user())

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
def _audit_login(action, user_id, username, ip):
    """登入成功／失敗留痕（CR-8）。用獨立短命 session 寫入並 commit，不碰 request 的 scoped session
    （避免 commit 把同 thread 既有 ORM 物件 expire 掉）；留痕失敗不影響登入流程。"""
    from audit_util import write_audit  # 延遲 import（audit_util 依賴本模組 current_user）
    from db import SessionLocal
    s = SessionLocal.session_factory()
    try:
        write_audit(s, action, "users", (user_id if user_id is not None else username),
                    {"username": username[:64], "ip": ip},
                    actor_id=user_id, actor_name=username[:64] or "(空)")
        s.commit()
    except Exception:  # noqa: BLE001
        s.rollback()
    finally:
        s.close()


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_session()
        user = db.query(User).filter_by(username=username, active=True).first()
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        ip = (ip or "").split(",")[0].strip() or None
        if user and check_password_hash(user.password_hash, password):
            flask_session["user_id"] = user.id
            flask_session["role"] = user.role
            # CR-8：登入成功留痕（actor = 該帳號；不寫任何密碼相關值）
            _audit_login("login_ok", user.id, username, ip)
            nxt = request.args.get("next") or url_for("auth.home")
            return redirect(nxt)
        # CR-8：登入失敗留痕（actor_name = 輸入帳號、actor_id 空；不記密碼）
        _audit_login("login_fail", None, username, ip)
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

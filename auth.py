from functools import wraps
from flask import session, redirect, url_for, flash
from werkzeug.security import check_password_hash


def check_login(username: str, password: str):
    """
    比對 users 表的 username 與 password_hash。
    登入成功回傳 dict（含 role），失敗回傳 None。
    """
    # 延遲 import，避免在 app context 外觸發 db 操作
    from db import db, User

    user = User.query.filter_by(username=username).first()
    if user and check_password_hash(user.password_hash, password):
        return {
            'id': user.id,
            'username': user.username,
            'name': user.name,
            'role': user.role,
        }
    return None


def login_required(f):
    """確認已登入，否則導向 /login。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            flash('請先登入')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


def owner_required(f):
    """
    確認已登入且 role == 'owner'。
    非 owner（staff）導向 /service/new，不顯示後台。
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            flash('請先登入')
            return redirect(url_for('auth.login'))
        if session['user'].get('role') != 'owner':
            flash('權限不足，僅老闆可查看後台')
            return redirect(url_for('service.new_service'))
        return f(*args, **kwargs)
    return decorated

"""audit_log blueprint（/admin/audit、/m/audit）— 操作紀錄查閱頁（CR-8，2026-08-31）。

森哥原話：「另外要有後台顯示哪個帳號在甚麼時候做過什麼編輯」。
- 資料來源：audit_logs（各 blueprint 經 audit_util.write_audit 寫入；本頁只讀，不寫）。
- 權限：owner / accounting（role_required；其他角色 403）。
- 桌面：列表（時間降冪、每頁 50）＋ 篩選（日期 from/to、帳號、動作類型、對象/關鍵字）＋ 每列人話摘要
  ＋ <details> 展開原始 JSON。訂單／商品／客戶類對象可連到明細頁。
- 手機：/m/audit 簡版（最近 100 筆，同權限）。
- 時間顯示：audit_logs.created_at 存 UTC；本頁以台北時間（UTC+8）顯示、日期篩選亦以台北日界線解讀。
"""
import json
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, url_for
from sqlalchemy import or_

from auth import role_required, current_user
from db import get_session, AuditLog, User
from display_labels import action_label, target_type_label, ACTION_LABELS
from audit_util import parse_detail, summarize, to_local, fmt_ts

audit_bp = Blueprint("audit_log", __name__, url_prefix="/admin/audit")
mobile_audit_bp = Blueprint("mobile_audit", __name__, url_prefix="/m")

READ_ROLES = ("accounting",)   # owner 由 role_required 永遠通過
PAGE_SIZE = 50
MOBILE_LIMIT = 100

# 對象類型 → 明細頁（target_id 為整數才連）
_TARGET_ENDPOINTS = {
    "orders": ("orders.detail", "order_id"),
    "products": ("products.detail", "pid"),
    "customers": ("customers.detail", "customer_id"),
}
_TARGET_STATIC = {
    "inventory": "inventory.index",
    "extra_expenses": "reports.expenses",
    "settings": "products.settings",
}


def _parse_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def _filters():
    """讀 query string；回 dict（含正規化後的值與錯誤訊息）。"""
    f = {
        "from": (request.args.get("from") or "").strip(),
        "to": (request.args.get("to") or "").strip(),
        "actor": (request.args.get("actor") or "").strip(),
        "action": (request.args.get("action") or "").strip(),
        "q": (request.args.get("q") or "").strip()[:64],
        "page": 1,
        "error": None,
    }
    try:
        f["page"] = max(1, int(request.args.get("page", "1")))
    except ValueError:
        f["page"] = 1
    if (f["from"] and _parse_date(f["from"]) is None) or (f["to"] and _parse_date(f["to"]) is None):
        f["error"] = "日期格式需為 YYYY-MM-DD"
        f["from"] = f["to"] = ""
    return f


def build_query(db, f):
    """依篩選組 query（時間降冪）。日期以台北日界線解讀後轉 UTC 比對。"""
    q = db.query(AuditLog)
    d_from = _parse_date(f.get("from"))
    d_to = _parse_date(f.get("to"))
    if d_from:
        q = q.filter(AuditLog.created_at >= to_local(d_from, reverse=True))
    if d_to:
        q = q.filter(AuditLog.created_at < to_local(d_to + timedelta(days=1), reverse=True))
    actor = f.get("actor")
    if actor:
        user = db.get(User, int(actor)) if actor.isdigit() else None
        if user is not None:
            names = [n for n in (user.username, user.display_name) if n]
            q = q.filter(or_(AuditLog.actor_id == user.id, AuditLog.actor_name.in_(names)))
        else:
            q = q.filter(AuditLog.actor_name == actor)
    if f.get("action"):
        q = q.filter(AuditLog.action == f["action"])
    if f.get("q"):
        kw = f"%{f['q']}%"
        q = q.filter(or_(AuditLog.target_id.like(kw), AuditLog.detail.like(kw),
                         AuditLog.actor_name.like(kw)))
    return q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())


def _target_url(target_type, target_id):
    ep = _TARGET_ENDPOINTS.get(target_type)
    if ep and target_id and str(target_id).isdigit():
        try:
            return url_for(ep[0], **{ep[1]: int(target_id)})
        except Exception:  # noqa: BLE001 — endpoint 未註冊（如 mobile 關閉）不崩
            return None
    ep2 = _TARGET_STATIC.get(target_type)
    if ep2:
        try:
            return url_for(ep2)
        except Exception:  # noqa: BLE001
            return None
    return None


def render_rows(db, logs):
    """AuditLog → 顯示列 dict（時間台北／帳號／動作中文／對象／摘要／原始 JSON）。"""
    users = {u.id: u for u in db.query(User).all()}
    rows = []
    for a in logs:
        u = users.get(a.actor_id) if a.actor_id else None
        actor = a.actor_name or (u.display_name if u else None) or (u.username if u else None) or "system"
        if u and u.username and u.username != actor:
            actor = f"{actor}（{u.username}）"
        d = parse_detail(a.detail)
        rows.append({
            "id": a.id,
            "ts": fmt_ts(a.created_at),
            "actor": actor,
            "action": a.action,
            "action_label": action_label(a.action),
            "target_type": a.target_type,
            "target_type_label": target_type_label(a.target_type),
            "target_id": a.target_id,
            "target_label": (d.get("order_no") or d.get("name") or d.get("product_name")
                             or d.get("customer_name") or d.get("key") or d.get("item") or ""),
            "target_url": _target_url(a.target_type, a.target_id),
            "summary": summarize(a.action, d),
            "detail_json": json.dumps(d, ensure_ascii=False, indent=2) if d else "",
        })
    return rows


def _action_options(db):
    seen = {r[0] for r in db.query(AuditLog.action).distinct().all() if r[0]}
    return sorted(seen, key=lambda a: (a not in ACTION_LABELS, action_label(a)))


@audit_bp.route("/")
@role_required(*READ_ROLES)
def index():
    db = get_session()
    f = _filters()
    q = build_query(db, f)
    total = q.count()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(f["page"], pages)
    logs = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    rows = render_rows(db, logs)

    base_args = {k: v for k, v in f.items() if k in ("from", "to", "actor", "action", "q") and v}

    def page_url(p):
        return url_for("audit_log.index", page=p, **base_args)

    return render_template(
        "audit/index.html", section="audit", user=current_user(),
        rows=rows, filters=f, total=total, page=page, pages=pages, page_size=PAGE_SIZE,
        prev_url=(page_url(page - 1) if page > 1 else None),
        next_url=(page_url(page + 1) if page < pages else None),
        users=db.query(User).order_by(User.id).all(),
        actions=[(a, action_label(a)) for a in _action_options(db)],
    )


@mobile_audit_bp.route("/audit")
@role_required(*READ_ROLES)
def mobile_index():
    """手機簡版：最近 100 筆（同權限；只支援動作類型 / 帳號快篩）。"""
    db = get_session()
    f = _filters()
    f["from"] = f["to"] = f["q"] = ""
    logs = build_query(db, f).limit(MOBILE_LIMIT).all()
    return render_template(
        "mobile/audit.html", section="mobile", user=current_user(),
        rows=render_rows(db, logs), filters=f, limit=MOBILE_LIMIT,
        users=db.query(User).order_by(User.id).all(),
        actions=[(a, action_label(a)) for a in _action_options(db)],
    )

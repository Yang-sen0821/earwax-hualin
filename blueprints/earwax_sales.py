"""earwax_sales blueprint（/earwax-sales）— 外泌體銷售/使用紀錄（甲案 2026-07-12）。

獨立核算：收入完全不進面膜訂單與報表。
寫入＝同一交易：INSERT earwax_sales ＋ 扣 earwax.consumables.qty_on_hand（不足擋下）
＋ audit_logs 留痕。R2 邊界：不 import earwax model、不建 cross-schema FK，
只用參數化 raw SQL 讀寫 earwax.consumables（表名常數見 blueprints.inventory）。

權限：寫＝owner/staff/warehouse；讀（清單）＝owner/accounting/staff/warehouse（viewer 不可）。
品項僅限 consumable（消耗品）；equipment（儀器）不可售。
"""

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)
from sqlalchemy import text

from auth import login_required, role_required, current_user
from blueprints.inventory import EARWAX_TABLE, _earwax_enabled
from db import get_session, EarwaxSale
from audit_util import write_audit

earwax_sales_bp = Blueprint("earwax_sales", __name__, url_prefix="/earwax-sales")

WRITE_ROLES = ("staff", "warehouse")          # owner 由 role_required 自動通過
READ_ROLES = ("accounting", "staff", "warehouse")


def _operator_name():
    u = current_user()
    if u is None:
        return "system"
    return u.display_name or u.username


def _sellable_items(db):
    """可售品項＝earwax.consumables 的 consumable 類（equipment 不可售）。"""
    return db.execute(text(
        f"SELECT id, name, qty_on_hand FROM {EARWAX_TABLE} "
        f"WHERE category = 'consumable' ORDER BY id"
    )).mappings().all()


def create_sale(db, item_id, qty, amount, note, operator, created_by):
    """建立一筆外泌體銷售：同交易扣庫存＋寫紀錄＋留痕。回 (ok, msg)。

    呼叫端負責 commit/rollback。
    """
    if qty is None or qty <= 0:
        return False, "數量必須大於 0"
    if amount is None or amount < 0:
        return False, "金額不可為負"
    row = db.execute(text(
        f"SELECT id, category, name, qty_on_hand FROM {EARWAX_TABLE} WHERE id = :i"),
        {"i": item_id}).mappings().first()
    if row is None:
        return False, "找不到品項"
    if row["category"] != "consumable":
        return False, "儀器設備不可銷售"
    if (row["qty_on_hand"] or 0) < qty:
        return False, f"庫存不足（{row['name']} 現有 {row['qty_on_hand']}，需 {qty}）"

    db.execute(text(
        f"UPDATE {EARWAX_TABLE} SET qty_on_hand = qty_on_hand - :q WHERE id = :i"),
        {"q": qty, "i": item_id})
    sale = EarwaxSale(item_id=item_id, item_name=row["name"], qty=qty,
                      amount=amount, operator=operator, note=note or None,
                      created_by=created_by)
    db.add(sale)
    u = current_user()
    write_audit(db, "earwax_sale_create", "earwax_sales", item_id,
                {"item": row["name"], "qty": qty, "amount": amount,
                 "qty_before": row["qty_on_hand"],
                 "qty_after": row["qty_on_hand"] - qty,
                 "note": note or ""},
                actor_id=(u.id if u else None), actor_name=operator)   # CR-8：走共用寫入口
    return True, f"已記錄：{row['name']} × {qty}"


@earwax_sales_bp.route("/")
@role_required(*READ_ROLES)
def index():
    if not _earwax_enabled():
        abort(404)
    db = get_session()
    rows = (db.query(EarwaxSale)
              .order_by(EarwaxSale.created_at.desc(), EarwaxSale.id.desc())
              .limit(300).all())
    total_amount = sum(r.amount or 0 for r in rows)
    return render_template(
        "earwax_sales/index.html", section="earwax_sales",
        rows=rows, total_amount=total_amount,
    )


@earwax_sales_bp.route("/new", methods=["GET", "POST"])
@role_required(*WRITE_ROLES)
def new():
    if not _earwax_enabled():
        abort(404)
    db = get_session()
    if request.method == "POST":
        try:
            item_id = int(request.form["item_id"])
            qty = int(request.form["qty"])
            amount = float(request.form.get("amount", "0") or 0)
        except (KeyError, ValueError):
            flash("品項/數量/金額格式錯誤", "error")
            return redirect(url_for("earwax_sales.new"))
        note = (request.form.get("note") or "").strip()
        u = current_user()
        try:
            ok, msg = create_sale(db, item_id, qty, amount, note,
                                  _operator_name(), u.id if u else None)
            if not ok:
                db.rollback()
                flash(f"未記錄：{msg}", "error")
                return redirect(url_for("earwax_sales.new"))
            db.commit()
        except Exception:
            db.rollback()
            flash("記錄失敗（資料庫錯誤），已全數回復", "error")
            return redirect(url_for("earwax_sales.new"))
        flash(msg, "ok")
        return redirect(url_for("earwax_sales.index"))

    return render_template(
        "earwax_sales/new.html", section="earwax_sales",
        items=_sellable_items(db),
    )

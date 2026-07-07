"""inventory blueprint（/inventory）— 庫存模組 UI（D6-D9）。

對應 DoD：
- D6 庫存總覽：盒裝/裸片/紙袋三池 × 5 分類（normal/reserved/pr/trial/scrap）
- D7 拆盒調撥（1 盒 → 5 片，2 筆 movement 同 group_id 留痕）
- D8 庫存異動紀錄查詢（入庫 2 + 業務出庫 9 + 系統內部 6）+ 補貨入庫 + 通用出庫
- D9 低水位警示（門檻讀 inventory_thresholds，扣減後 remaining<=threshold）

權限（§六 權限矩陣）：
- 讀：全角色（login_required）
- 寫（補貨/拆盒/出庫）：owner / warehouse（role_required；owner 永遠通過）
- 手動調整核可：限 owner（approve）

鐵律：任何改庫存量一律呼叫 inventory_service 的契約函式；本檔禁止自寫
inventory_balances / inventory_movements。session commit/rollback 在本層管理（同一 tx）。
"""
import json
import os
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)
from sqlalchemy import text

from auth import login_required, role_required, current_user
from config import Config
from db import (
    get_session, Product, InventoryBalance, InventoryMovement,
    InventoryThreshold, AuditLog,
    INVENTORY_POOLS, STOCK_CATEGORIES, MOVEMENT_TYPES,
)
import inventory_service as inv
# 顯示用標籤：全站單一來源（display_labels）
from display_labels import POOL_LABELS, CAT_LABELS, MTYPE_LABELS

inventory_bp = Blueprint("inventory", __name__, url_prefix="/inventory")

# 愛啪啪耗材表（R2 邊界：不 import earwax model、不建 FK，僅參數化 raw SQL）。
# EARWAX_TABLE 為固定常數/測試 env 覆寫，非使用者輸入，無注入面。
EARWAX_TABLE = os.environ.get("EARWAX_TABLE", "earwax.consumables")
EARWAX_CATEGORIES = ("consumable", "equipment")


def _earwax_enabled():
    """正式 Postgres 一律啟用；本機 SQLite 僅在測試以 EARWAX_TABLE 指向同構表時啟用。"""
    return (not Config.is_sqlite()) or bool(os.environ.get("EARWAX_TABLE"))
# 出庫類（§4.6 通用出庫）
OUT_TYPES = ["GIFT", "TRIAL", "PR", "KOL_SAMPLE", "STAFF_USE",
             "INSTORE_USE", "SCRAP_LOSS", "SCRAP"]
RESTOCK_TYPES = ["PURCHASE", "RESTOCK"]


def _can_write():
    """寫權限：owner / warehouse。"""
    u = current_user()
    return u is not None and u.role in ("owner", "warehouse")


def _operator_name():
    u = current_user()
    if u is None:
        return "system"
    return u.display_name or u.username


# =========================================================================
# 庫存總覽（D6）：三池 × 5 分類矩陣
# =========================================================================
@inventory_bp.route("/")
@login_required
def index():
    db = get_session()
    products = db.query(Product).filter_by(active=True).order_by(Product.id).all()
    balances = db.query(InventoryBalance).all()

    # 以 product_id -> {(pool,cat): qty} 建矩陣
    bal_map = {}
    for b in balances:
        bal_map.setdefault(b.product_id, {})[(b.inventory_pool, b.stock_category)] = b.qty

    # 低水位旗標：product_id -> {pool: bool}
    low_map = {}
    for p in products:
        for pool in INVENTORY_POOLS:
            r = inv.check_low_water(db, p.id, pool, "normal")
            if r.low_water:
                low_map.setdefault(p.id, {})[pool] = r.threshold

    # 愛啪啪庫存區（森哥 2026-07-07：庫存頁分區顯示；同日升級為可編輯，含品名）
    # R2 邊界：不 import earwax model、不建 cross-schema FK、僅參數化 raw SQL
    earwax_items = None
    earwax_error = False
    if _earwax_enabled():
        try:
            earwax_items = db.execute(text(
                f"SELECT id, category, name, qty_on_hand, unit_cost, COALESCE(note, '') AS note "
                f"FROM {EARWAX_TABLE} ORDER BY category, id"
            )).mappings().all()
        except Exception:
            db.rollback()
            earwax_error = True

    u = current_user()
    return render_template(
        "inventory/index.html", section="inventory",
        products=products, bal_map=bal_map, low_map=low_map,
        pools=INVENTORY_POOLS, cats=STOCK_CATEGORIES,
        pool_labels=POOL_LABELS, cat_labels=CAT_LABELS,
        can_write=_can_write(),
        is_owner=(u is not None and u.role == "owner"),
        earwax_items=earwax_items, earwax_error=earwax_error,
    )


# =========================================================================
# 愛啪啪耗材編輯 / 新增（森哥 2026-07-07 授權；owner/warehouse；audit_logs 留痕）
# =========================================================================
def _earwax_form_values():
    """共用表單驗證：回 (values, error)。"""
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "consumable").strip()
    note = request.form.get("note", "").strip()
    try:
        qty = int(request.form.get("qty_on_hand", "0"))
        unit_cost = float(request.form.get("unit_cost", "0") or 0)
    except ValueError:
        return None, "數量／單位成本格式錯誤"
    if not name:
        return None, "品名不可空白"
    if qty < 0:
        return None, "數量不可為負"
    if unit_cost < 0:
        return None, "單位成本不可為負"
    if category not in EARWAX_CATEGORIES:
        return None, "類別不合法"
    return {"name": name, "category": category, "qty_on_hand": qty,
            "unit_cost": unit_cost, "note": note}, None


def _audit(db, action, target_id, detail_obj):
    u = current_user()
    db.add(AuditLog(
        actor_id=(u.id if u else None), actor_name=_operator_name(),
        action=action, target_type="earwax.consumables",
        target_id=str(target_id),
        detail=json.dumps(detail_obj, ensure_ascii=False, default=str),
    ))


@inventory_bp.route("/earwax/<int:cid>/edit", methods=["POST"])
@role_required("owner", "warehouse")
def earwax_edit(cid):
    if not _earwax_enabled():
        abort(404)
    db = get_session()
    values, err = _earwax_form_values()
    if err:
        flash(f"愛啪啪品項未更新：{err}", "error")
        return redirect(url_for("inventory.index"))
    before = db.execute(text(
        f"SELECT category, name, qty_on_hand, unit_cost, COALESCE(note,'') AS note "
        f"FROM {EARWAX_TABLE} WHERE id = :cid"), {"cid": cid}).mappings().first()
    if before is None:
        flash("找不到該愛啪啪品項", "error")
        return redirect(url_for("inventory.index"))
    try:
        db.execute(text(
            f"UPDATE {EARWAX_TABLE} SET category=:category, name=:name, "
            f"qty_on_hand=:qty_on_hand, unit_cost=:unit_cost, note=:note "
            f"WHERE id=:cid"), {**values, "cid": cid})
        _audit(db, "earwax_consumable_edit", cid,
               {"before": dict(before), "after": values})
        db.commit()
    except Exception:
        db.rollback()
        flash("愛啪啪品項更新失敗（資料庫錯誤）", "error")
        return redirect(url_for("inventory.index"))
    flash(f"愛啪啪品項「{values['name']}」已更新", "ok")
    return redirect(url_for("inventory.index"))


@inventory_bp.route("/earwax/new", methods=["POST"])
@role_required("owner", "warehouse")
def earwax_new():
    if not _earwax_enabled():
        abort(404)
    db = get_session()
    values, err = _earwax_form_values()
    if err:
        flash(f"愛啪啪品項未新增：{err}", "error")
        return redirect(url_for("inventory.index"))
    try:
        new_id = db.execute(text(
            f"INSERT INTO {EARWAX_TABLE} (category, name, qty_on_hand, unit_cost, note, created_at) "
            f"VALUES (:category, :name, :qty_on_hand, :unit_cost, :note, :ts) RETURNING id"),
            {**values, "ts": datetime.utcnow()}).scalar()
        _audit(db, "earwax_consumable_create", new_id, {"after": values})
        db.commit()
    except Exception:
        db.rollback()
        flash("愛啪啪品項新增失敗（資料庫錯誤）", "error")
        return redirect(url_for("inventory.index"))
    flash(f"愛啪啪品項「{values['name']}」已新增", "ok")
    return redirect(url_for("inventory.index"))


# =========================================================================
# 補貨入庫（D8 入庫 2 類）
# =========================================================================
@inventory_bp.route("/restock", methods=["GET", "POST"])
@role_required("owner", "warehouse")
def restock():
    db = get_session()
    products = db.query(Product).order_by(Product.id).all()
    if request.method == "POST":
        try:
            product_id = int(request.form["product_id"])
            pool = request.form["inventory_pool"]
            category = request.form.get("stock_category", "normal")
            mtype = request.form["movement_type"]
            qty = int(request.form["qty"])
        except (KeyError, ValueError):
            flash("欄位不完整或數量格式錯誤", "error")
            return redirect(url_for("inventory.restock"))
        note = request.form.get("note", "")
        res = inv.restock(db, product_id, pool, category, mtype, qty,
                          _operator_name(), note=note)
        if res.ok:
            db.commit()
            flash(f"入庫成功：{POOL_LABELS.get(pool, pool)} +{qty}，現存 {res.qty_after}", "ok")
            return redirect(url_for("inventory.index"))
        db.rollback()
        flash(f"入庫失敗：{res.error}", "error")
        return redirect(url_for("inventory.restock"))

    return render_template(
        "inventory/restock.html", section="inventory",
        products=products, pools=INVENTORY_POOLS, cats=STOCK_CATEGORIES,
        pool_labels=POOL_LABELS, cat_labels=CAT_LABELS,
        restock_types=RESTOCK_TYPES, mtype_labels=MTYPE_LABELS,
    )


# =========================================================================
# 通用出庫（D8 業務出庫 9 類，扣 normal 以外或贈品/試用等）
# =========================================================================
@inventory_bp.route("/deduct", methods=["GET", "POST"])
@role_required("owner", "warehouse")
def deduct():
    db = get_session()
    products = db.query(Product).order_by(Product.id).all()
    if request.method == "POST":
        try:
            product_id = int(request.form["product_id"])
            pool = request.form["inventory_pool"]
            category = request.form.get("stock_category", "normal")
            mtype = request.form["movement_type"]
            qty = int(request.form["qty"])
        except (KeyError, ValueError):
            flash("欄位不完整或數量格式錯誤", "error")
            return redirect(url_for("inventory.deduct"))
        note = request.form.get("note", "")
        res = inv.deduct_out(db, product_id, pool, category, mtype, qty,
                             _operator_name(), note=note)
        if res.ok:
            db.commit()
            # §7.10 扣減後查低水位
            lw = inv.check_low_water(db, product_id, pool, category)
            msg = f"出庫成功：{POOL_LABELS.get(pool, pool)} -{qty}，現存 {res.qty_after}"
            if lw.low_water:
                msg += f"（低水位警示！門檻 {lw.threshold}）"
            flash(msg, "ok" if not lw.low_water else "warn")
            return redirect(url_for("inventory.index"))
        db.rollback()
        flash(f"出庫失敗：{res.error}", "error")
        return redirect(url_for("inventory.deduct"))

    return render_template(
        "inventory/deduct.html", section="inventory",
        products=products, pools=INVENTORY_POOLS, cats=STOCK_CATEGORIES,
        pool_labels=POOL_LABELS, cat_labels=CAT_LABELS,
        out_types=OUT_TYPES, mtype_labels=MTYPE_LABELS,
    )


# =========================================================================
# 拆盒調撥（D7）：1 盒 → 5 片，2 筆 movement 同 group_id
# =========================================================================
@inventory_bp.route("/split", methods=["GET", "POST"])
@role_required("owner", "warehouse")
def split():
    db = get_session()
    # 只有面膜類（非包材）可拆盒
    products = db.query(Product).filter_by(is_packaging=False, active=True)\
        .order_by(Product.id).all()
    if request.method == "POST":
        try:
            product_id = int(request.form["product_id"])
            box_qty = int(request.form["box_qty"])
        except (KeyError, ValueError):
            flash("欄位不完整或數量格式錯誤", "error")
            return redirect(url_for("inventory.split"))
        note = request.form.get("note", "")
        res = inv.split_box(db, product_id, box_qty, _operator_name(), note=note)
        if res.ok:
            db.commit()
            lw = inv.check_low_water(db, product_id, "boxed", "normal")
            msg = (f"拆盒成功：盒 -{box_qty}、片 +{box_qty*5}；"
                   f"盒裝現存 {res.qty_after}")
            if lw.low_water:
                msg += f"（盒裝低水位！門檻 {lw.threshold}）"
            flash(msg, "ok" if not lw.low_water else "warn")
            return redirect(url_for("inventory.index"))
        db.rollback()
        flash(f"拆盒失敗：{res.error}", "error")
        return redirect(url_for("inventory.split"))

    return render_template(
        "inventory/split.html", section="inventory", products=products,
    )


# =========================================================================
# 手動調整 / 盤點（§7.9）：ADJUSTMENT + reason 必填 + owner 核可
# =========================================================================
@inventory_bp.route("/adjust", methods=["GET", "POST"])
@role_required("owner")  # 調整核可限 owner（approve）
def adjust():
    db = get_session()
    products = db.query(Product).order_by(Product.id).all()
    balances = db.query(InventoryBalance).all()
    bal_map = {}
    for b in balances:
        bal_map[(b.product_id, b.inventory_pool, b.stock_category)] = b.qty

    if request.method == "POST":
        try:
            product_id = int(request.form["product_id"])
            pool = request.form["inventory_pool"]
            category = request.form.get("stock_category", "normal")
            target_qty = int(request.form["target_qty"])
        except (KeyError, ValueError):
            flash("欄位不完整或數量格式錯誤", "error")
            return redirect(url_for("inventory.adjust"))
        reason = request.form.get("reason", "").strip()
        note = request.form.get("note", "")
        if not reason:
            flash("reason（調整原因）必填（§7.9）", "error")
            return redirect(url_for("inventory.adjust"))

        approver = current_user()  # 已由 role_required('owner') 保證為 owner
        res = inv.adjust_inventory(
            db, product_id, pool, category, target_qty, reason,
            _operator_name(), approved_by=approver.id, note=note,
        )
        if res.ok:
            db.commit()
            flash(f"調整成功：{POOL_LABELS.get(pool, pool)}/{CAT_LABELS.get(category, category)} "
                  f"{res.qty_before} → {res.qty_after}", "ok")
            return redirect(url_for("inventory.index"))
        db.rollback()
        flash(f"調整失敗：{res.error}", "error")
        return redirect(url_for("inventory.adjust"))

    # GET 預填（庫存總覽格子點進來帶 product_id/pool/category；POST 邏輯不受影響）
    prefill = {
        "product_id": request.args.get("product_id", ""),
        "pool": request.args.get("pool", ""),
        "category": request.args.get("category", ""),
    }
    return render_template(
        "inventory/adjust.html", section="inventory", prefill=prefill,
        products=products, pools=INVENTORY_POOLS, cats=STOCK_CATEGORIES,
        pool_labels=POOL_LABELS, cat_labels=CAT_LABELS, bal_map=bal_map,
    )


# =========================================================================
# 庫存異動紀錄查詢（D8）
# =========================================================================
@inventory_bp.route("/movements")
@login_required
def movements():
    db = get_session()
    q = db.query(InventoryMovement)

    # 篩選
    f_product = request.args.get("product_id", "")
    f_pool = request.args.get("inventory_pool", "")
    f_type = request.args.get("movement_type", "")
    if f_product:
        try:
            q = q.filter(InventoryMovement.product_id == int(f_product))
        except ValueError:
            pass
    if f_pool in INVENTORY_POOLS:
        q = q.filter(InventoryMovement.inventory_pool == f_pool)
    if f_type in MOVEMENT_TYPES:
        q = q.filter(InventoryMovement.movement_type == f_type)

    rows = q.order_by(InventoryMovement.movement_id.desc()).limit(300).all()
    products = db.query(Product).order_by(Product.id).all()
    pname = {p.id: p.name for p in products}

    return render_template(
        "inventory/movements.html", section="inventory",
        rows=rows, products=products, pname=pname,
        pool_labels=POOL_LABELS, cat_labels=CAT_LABELS, mtype_labels=MTYPE_LABELS,
        pools=INVENTORY_POOLS, mtypes=MOVEMENT_TYPES,
        f_product=f_product, f_pool=f_pool, f_type=f_type,
    )


# =========================================================================
# 低水位警示（D9）
# =========================================================================
@inventory_bp.route("/alerts")
@login_required
def alerts():
    db = get_session()
    thresholds = db.query(InventoryThreshold).all()
    products = db.query(Product).order_by(Product.id).all()
    pname = {p.id: p.name for p in products}

    items = []
    for t in thresholds:
        r = inv.check_low_water(db, t.product_id, t.inventory_pool, "normal")
        items.append({
            "product_id": t.product_id,
            "product_name": pname.get(t.product_id, f"#{t.product_id}"),
            "pool": t.inventory_pool,
            "pool_label": POOL_LABELS.get(t.inventory_pool, t.inventory_pool),
            "remaining": r.remaining,
            "threshold": r.threshold,
            "low_water": r.low_water,
        })
    # 低水位在前
    items.sort(key=lambda x: (not x["low_water"], x["product_id"]))

    return render_template(
        "inventory/alerts.html", section="inventory", items=items,
    )

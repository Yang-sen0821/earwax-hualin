# blueprints/purchases.py
# 補貨 blueprint（purchases_bp）
# 路由：
#   /purchases/voucher     — 補券（加 voucher_inventory + 記 VoucherPurchase）
#   /purchases/consumable  — 補消耗品（加 consumables.qty_on_hand + 記 ConsumablePurchase）

import datetime
from flask import (
    Blueprint, render_template, request,
    redirect, url_for, flash
)
from db import (
    db, Product, VoucherInventory, VoucherPurchase,
    Consumable, ConsumablePurchase
)
from auth import login_required

purchases_bp = Blueprint("purchases", __name__, url_prefix="/purchases")


# ──────────────────────────────────────────────
# 補券
# ──────────────────────────────────────────────

@purchases_bp.route("/voucher", methods=["GET", "POST"])
@login_required
def voucher():
    """補券頁面。
    POST：建立 VoucherPurchase 記錄，並將數量加進 VoucherInventory。
    """
    products = Product.query.filter_by(active=True).all()

    def _voucher_context(**extra):
        """組裝模板共用 context：目前券庫存 + 最近補貨記錄。"""
        first_product = products[0] if products else None
        stock_row = None
        if first_product:
            stock_row = VoucherInventory.query.filter_by(
                product_id=first_product.id
            ).first()
        recent = (
            VoucherPurchase.query
            .order_by(VoucherPurchase.created_at.desc())
            .limit(10)
            .all()
        )
        ctx = {
            "products": products,
            "today": datetime.date.today().isoformat(),
            "current_stock": stock_row.qty_on_hand if stock_row else 0,
            "purchases": recent,
            "form": {},
        }
        ctx.update(extra)
        return ctx

    if request.method == "POST":
        f          = request.form
        product_id = int(f.get("product_id", 0))
        qty        = int(f.get("qty", 0))
        unit_cost  = float(f.get("unit_cost", 0))
        date_str   = f.get("date", datetime.date.today().isoformat())
        note       = f.get("note", "").strip()

        if qty <= 0:
            flash("數量必須大於 0。")
            return render_template(
                "purchases/voucher.html",
                **_voucher_context(form=f),
            )

        total_cost = qty * unit_cost

        # ── 建立補券記錄 ──────────────────────
        purchase = VoucherPurchase(
            date       = date_str,
            qty        = qty,
            unit_cost  = unit_cost,
            total_cost = total_cost,
            note       = note,
            created_at = datetime.datetime.utcnow(),
        )
        db.session.add(purchase)

        # ── 加券庫存 ──────────────────────────
        voucher_inv = VoucherInventory.query.filter_by(product_id=product_id).first()
        if not voucher_inv:
            voucher_inv = VoucherInventory(
                product_id  = product_id,
                qty_on_hand = 0,
            )
            db.session.add(voucher_inv)
            db.session.flush()

        voucher_inv.qty_on_hand += qty
        db.session.commit()

        flash(
            f"補券成功！加入 {qty} 張，"
            f"目前庫存：{voucher_inv.qty_on_hand} 張。"
        )
        return redirect(url_for("inventory.index"))

    return render_template(
        "purchases/voucher.html",
        **_voucher_context(),
    )


# ──────────────────────────────────────────────
# 補消耗品
# ──────────────────────────────────────────────

@purchases_bp.route("/consumable", methods=["GET", "POST"])
@login_required
def consumable():
    """補消耗品頁面（針筒、耳塞、安瓶、洗卸品、洗臉巾、防曬等 category='consumable'）。
    POST：建立 ConsumablePurchase 記錄，並將數量加進對應 Consumable.qty_on_hand。
    """
    # 只列 category='consumable'（儀器設備不補貨）
    consumables = (
        Consumable.query
        .filter_by(category="consumable")
        .order_by(Consumable.id.asc())
        .all()
    )

    def _consumable_context(**extra):
        """組裝模板共用 context：消耗品清單 + 最近補貨記錄。"""
        recent = (
            ConsumablePurchase.query
            .order_by(ConsumablePurchase.created_at.desc())
            .limit(10)
            .all()
        )
        ctx = {
            "consumables": consumables,
            "today": datetime.date.today().isoformat(),
            "purchases": recent,
            "form": {},
        }
        ctx.update(extra)
        return ctx

    if request.method == "POST":
        f              = request.form
        consumable_id  = int(f.get("consumable_id", 0))
        qty            = int(f.get("qty", 0))
        unit_cost      = float(f.get("unit_cost", 0))
        date_str       = f.get("date", datetime.date.today().isoformat())
        note           = f.get("note", "").strip()

        if qty <= 0:
            flash("數量必須大於 0。")
            return render_template(
                "purchases/consumable.html",
                **_consumable_context(form=f),
            )

        total_cost = qty * unit_cost

        # ── 建立補貨記錄 ──────────────────────
        purchase = ConsumablePurchase(
            date          = date_str,
            consumable_id = consumable_id,
            qty           = qty,
            unit_cost     = unit_cost,
            total_cost    = total_cost,
            note          = note,
            created_at    = datetime.datetime.utcnow(),
        )
        db.session.add(purchase)

        # ── 加消耗品庫存 ──────────────────────
        item = db.session.get(Consumable, consumable_id)
        if not item or item.category != "consumable":
            flash("找不到指定的消耗品，請聯絡管理員。")
            db.session.rollback()
            return redirect(url_for("purchases.consumable"))

        item.qty_on_hand += qty
        db.session.commit()

        flash(
            f"補貨成功！{item.name} 加入 {qty} 件，"
            f"目前庫存：{item.qty_on_hand} 件。"
        )
        return redirect(url_for("inventory.index"))

    return render_template(
        "purchases/consumable.html",
        **_consumable_context(),
    )

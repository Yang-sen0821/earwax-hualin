# blueprints/inventory.py
# 庫存總覽 blueprint（inventory_bp）
# 路由：/inventory — 顯示券庫存 + 消耗品庫存 + 儀器設備清單

from flask import Blueprint, render_template
from db import (
    db, VoucherInventory, Consumable, Product,
    VoucherPurchase, ConsumablePurchase,
)
from auth import login_required

inventory_bp = Blueprint("inventory", __name__, url_prefix="/inventory")


@inventory_bp.route("/")
@login_required
def index():
    """庫存總覽頁：券庫存 + 消耗品庫存（逐項）+ 儀器設備清單。"""

    # ── 券庫存（每個產品一筆；模板用 vi.product.name 取產品名）──
    voucher_inventory = (
        VoucherInventory.query
        .order_by(VoucherInventory.product_id.asc())
        .all()
    )

    # ── 消耗品（category='consumable'）：管庫存，施作扣用 ──
    consumables = (
        Consumable.query
        .filter_by(category="consumable")
        .order_by(Consumable.id.asc())
        .all()
    )

    # ── 儀器設備（category='equipment'）：固定資產，只列清單 ──
    equipment = (
        Consumable.query
        .filter_by(category="equipment")
        .order_by(Consumable.id.asc())
        .all()
    )

    # ── 最近補貨記錄（各取最近 10 筆，依建立時間倒序）──
    voucher_purchases = (
        VoucherPurchase.query
        .order_by(VoucherPurchase.created_at.desc())
        .limit(10)
        .all()
    )
    consumable_purchases = (
        ConsumablePurchase.query
        .order_by(ConsumablePurchase.created_at.desc())
        .limit(10)
        .all()
    )

    return render_template(
        "inventory/index.html",
        voucher_inventory=voucher_inventory,
        consumables=consumables,
        equipment=equipment,
        voucher_purchases=voucher_purchases,
        consumable_purchases=consumable_purchases,
    )

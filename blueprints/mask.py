# blueprints/mask.py
# 面膜模組 blueprint（mask_bp）
# 路由：/mask（owner ROI）、/mask/sales/new、/mask/sales、/mask/cost、/mask/inventory
#
# 業務規則（SPEC §8.2~8.7）：
#   1. 收入 = general_box_amount + general_piece_amount（後端計算，不信前端）
#   2. 已投入成本 = Σ mask_cost_items.amount
#   3. 面膜淨利 = 收入 − 成本；回本進度 = 收入 / 成本
#   4. POST /mask/sales/new 副作用（同一 transaction，不足要擋並 flash）：
#      general_box_qty → 扣 mask_inventory[general_box]
#      pr_box_qty      → 扣 mask_inventory[pr_box]
#      general_piece_qty → 扣 mask_inventory[general_piece]
#      pr_piece_qty    → 扣 mask_inventory[pr_piece]
#      bag_qty         → 扣 mask_inventory[bag]
#   5. 面膜數據完全不污染愛啪啪損益（§4 全店總覽不含面膜）

import datetime
from flask import (
    Blueprint, render_template, request,
    redirect, url_for, flash, session
)
from db import (
    db, MaskSale, MaskCostItem, MaskInventory, MaskPurchase
)
from auth import login_required, owner_required

mask_bp = Blueprint("mask", __name__, url_prefix="/mask")

# 固定 item_key 順序（與 SPEC §8.2 一致）
MASK_ITEM_KEYS = [
    ("general_box",    "一般盒裝"),
    ("pr_box",         "公關盒裝"),
    ("general_piece",  "一般單片"),
    ("pr_piece",       "公關單片"),
    ("bag",            "包裝袋"),
]


# ──────────────────────────────────────────────
# /mask — 面膜 ROI 儀表板（owner 限定）
# ──────────────────────────────────────────────

@mask_bp.route("/")
@owner_required
def dashboard():
    """面膜 ROI 儀表板：累積收入、已投入成本、淨利、回本進度；各項銷量；期間篩選。"""
    start = request.args.get("start", "").strip()
    end   = request.args.get("end",   "").strip()

    # ── 銷售資料查詢（支援 start/end 篩選）────
    query = MaskSale.query
    if start:
        query = query.filter(MaskSale.date >= start)
    if end:
        query = query.filter(MaskSale.date <= end)
    sales = query.all()

    # ── 後端計算累積收入（不信前端傳入值，直接加總欄位）──
    total_revenue = sum(
        (s.general_box_amount or 0) + (s.general_piece_amount or 0)
        for s in sales
    )

    # ── 各項銷量（含公關贈送）────────────────
    total_general_box_qty   = sum(s.general_box_qty   or 0 for s in sales)
    total_pr_box_qty        = sum(s.pr_box_qty        or 0 for s in sales)
    total_general_piece_qty = sum(s.general_piece_qty or 0 for s in sales)
    total_pr_piece_qty      = sum(s.pr_piece_qty      or 0 for s in sales)
    total_bag_qty           = sum(s.bag_qty           or 0 for s in sales)

    # ── 已投入成本（不受期間篩選影響，為全部成本總額）──
    cost_items  = MaskCostItem.query.order_by(MaskCostItem.created_at.asc()).all()
    total_cost  = sum(item.amount or 0 for item in cost_items)

    # ── 衍生指標（後端計算）──────────────────
    net_profit = total_revenue - total_cost
    # 回本進度（百分比）：若成本 = 0，避免除以零
    roi_pct = (total_revenue / total_cost * 100) if total_cost > 0 else 0.0

    # 各項銷量打包成 dict（模板以 qty_stats.<key> 取用）
    qty_stats = {
        "general_box_qty":   total_general_box_qty,
        "pr_box_qty":        total_pr_box_qty,
        "general_piece_qty": total_general_piece_qty,
        "pr_piece_qty":      total_pr_piece_qty,
        "bag_qty":           total_bag_qty,
    }

    return render_template(
        "mask/dashboard.html",
        start=start,
        end=end,
        total_revenue=total_revenue,
        total_cost=total_cost,
        net_profit=net_profit,
        roi_pct=roi_pct,
        qty_stats=qty_stats,
        cost_items=cost_items,
    )


# ──────────────────────────────────────────────
# /mask/sales — 銷售列表（staff 可）
# ──────────────────────────────────────────────

@mask_bp.route("/sales")
@login_required
def sales_list():
    """面膜銷售列表，按日期倒序。"""
    sales = (
        MaskSale.query
        .order_by(MaskSale.date.desc(), MaskSale.id.desc())
        .all()
    )
    return render_template("mask/sales_list.html", sales=sales)


# ──────────────────────────────────────────────
# /mask/sales/new — 銷售 KEY 單（staff 可）
# ──────────────────────────────────────────────

@mask_bp.route("/sales/new", methods=["GET", "POST"])
@login_required
def sales_new():
    """面膜銷售 KEY 單：5 項品項各填數量；一般盒裝/一般單片額外填金額（實收）。
    POST 副作用：各項數量扣對應 mask_inventory.qty_on_hand（同一 transaction）。
    """
    if request.method == "GET":
        return render_template(
            "mask/sales_new.html",
            today=datetime.date.today().isoformat(),
        )

    # ── POST：解析表單 ───────────────────────
    f    = request.form
    date = f.get("date", datetime.date.today().isoformat()).strip()
    note = f.get("note", "").strip()

    # 數量（不足或空白時預設為 0）
    def _int(key):
        try:
            return max(0, int(f.get(key, 0) or 0))
        except (ValueError, TypeError):
            return 0

    # 金額（一般品項的實收金額，後端權威計算收入用）
    def _float(key):
        try:
            return max(0.0, float(f.get(key, 0) or 0))
        except (ValueError, TypeError):
            return 0.0

    general_box_qty       = _int("general_box_qty")
    general_box_amount    = _float("general_box_amount")
    pr_box_qty            = _int("pr_box_qty")
    general_piece_qty     = _int("general_piece_qty")
    general_piece_amount  = _float("general_piece_amount")
    pr_piece_qty          = _int("pr_piece_qty")
    bag_qty               = _int("bag_qty")

    # ── 庫存不足檢查 + 扣減（同一 transaction）──
    # 建立 item_key → 扣減數量 的對應
    deductions = [
        ("general_box",   general_box_qty),
        ("pr_box",        pr_box_qty),
        ("general_piece", general_piece_qty),
        ("pr_piece",      pr_piece_qty),
        ("bag",           bag_qty),
    ]

    # 預先取出所有 5 個庫存物件，建立 dict 供快速查找
    inv_records = {
        inv.item_key: inv
        for inv in MaskInventory.query.all()
    }

    for item_key, qty in deductions:
        if qty <= 0:
            continue
        inv = inv_records.get(item_key)
        if inv is None:
            flash(f"找不到庫存品項（{item_key}），請聯絡管理員。", "error")
            db.session.rollback()
            return redirect(url_for("mask.sales_new"))
        if inv.qty_on_hand < qty:
            flash(
                f"「{inv.name}」庫存不足！"
                f"目前剩餘 {inv.qty_on_hand}，本次需扣 {qty}。"
                f"請先補貨後再 KEY 單。",
                "error"
            )
            db.session.rollback()
            return redirect(url_for("mask.sales_new"))
        inv.qty_on_hand -= qty

    # ── 建立銷售記錄 ─────────────────────────
    sale = MaskSale(
        date                 = date,
        general_box_qty      = general_box_qty,
        general_box_amount   = general_box_amount,
        pr_box_qty           = pr_box_qty,
        general_piece_qty    = general_piece_qty,
        general_piece_amount = general_piece_amount,
        pr_piece_qty         = pr_piece_qty,
        bag_qty              = bag_qty,
        note                 = note,
        created_at           = datetime.datetime.utcnow(),
    )
    db.session.add(sale)

    # ── 全部成功 → commit ────────────────────
    db.session.commit()

    # 後端計算本筆收入（供 flash 訊息顯示）
    this_revenue = general_box_amount + general_piece_amount
    flash(
        f"銷售記錄已建立！本筆收入 {this_revenue:,.0f} 元。",
        "success"
    )
    return redirect(url_for("mask.sales_list"))


# ──────────────────────────────────────────────
# /mask/cost — 成本頁（owner 限定）
# ──────────────────────────────────────────────

@mask_bp.route("/cost", methods=["GET", "POST"])
@owner_required
def cost():
    """成本頁：列出 mask_cost_items + 新增（name, amount）+ 顯示總額。
    刪除由獨立路由 /mask/cost/<id>/delete 處理（對應 cost.html 的刪除表單）。
    """
    if request.method == "POST":
        # ── 新增成本品項（cost.html 新增表單 POST /mask/cost）──
        name       = request.form.get("name",   "").strip()
        amount_str = request.form.get("amount", "0").strip()
        if not name:
            flash("請填寫成本品項名稱。", "error")
            return redirect(url_for("mask.cost"))
        try:
            amount = float(amount_str)
            if amount < 0:
                raise ValueError
        except (ValueError, TypeError):
            flash("金額必須為有效的非負數字。", "error")
            return redirect(url_for("mask.cost"))

        item = MaskCostItem(
            name       = name,
            amount     = amount,
            note       = request.form.get("note", "").strip(),
            created_at = datetime.datetime.utcnow(),
        )
        db.session.add(item)
        db.session.commit()
        flash(f"已新增成本品項「{name}」，金額 {amount:,.0f} 元。", "success")
        return redirect(url_for("mask.cost"))

    # ── GET：列出所有成本品項 ─────────────────
    cost_items = (
        MaskCostItem.query
        .order_by(MaskCostItem.created_at.asc())
        .all()
    )
    total_cost = sum(item.amount or 0 for item in cost_items)

    return render_template(
        "mask/cost.html",
        cost_items=cost_items,
        total_cost=total_cost,
    )


@mask_bp.route("/cost/<int:item_id>/delete", methods=["POST"])
@owner_required
def cost_delete(item_id):
    """刪除單一成本品項（對應 cost.html 的刪除表單 /mask/cost/<id>/delete）。"""
    item = db.session.get(MaskCostItem, item_id)
    if not item:
        flash("找不到該成本品項。", "error")
        return redirect(url_for("mask.cost"))
    name = item.name
    db.session.delete(item)
    db.session.commit()
    flash(f"已刪除成本品項「{name}」。", "success")
    return redirect(url_for("mask.cost"))


# ──────────────────────────────────────────────
# /mask/inventory — 庫存頁 + 補貨
# ──────────────────────────────────────────────

@mask_bp.route("/inventory", methods=["GET"])
@login_required
def inventory():
    """庫存頁（GET）：顯示 5 項 qty_on_hand + 最近補貨記錄。
    補貨由獨立路由 /mask/inventory/restock 處理（對應 inventory.html 的補貨表單）。
    """
    # 依固定順序排列（與 MASK_ITEM_KEYS 順序一致）
    inv_map = {
        inv.item_key: inv
        for inv in MaskInventory.query.all()
    }
    inventory_items = [
        inv_map.get(key)
        for key, _ in MASK_ITEM_KEYS
        if inv_map.get(key) is not None
    ]

    recent_purchases = (
        MaskPurchase.query
        .order_by(MaskPurchase.created_at.desc())
        .limit(20)
        .all()
    )

    return render_template(
        "mask/inventory.html",
        inventory=inventory_items,
        recent_purchases=recent_purchases,
        mask_item_keys=MASK_ITEM_KEYS,
        today=datetime.date.today().isoformat(),
    )


@mask_bp.route("/inventory/restock", methods=["POST"])
@login_required
def inventory_restock():
    """補貨入庫：選 item_key + qty → 寫 mask_purchases 並加 mask_inventory.qty_on_hand。"""
    item_key  = request.form.get("item_key",  "").strip()
    qty_str   = request.form.get("qty",       "0").strip()
    date_str  = request.form.get("date",      datetime.date.today().isoformat()).strip()
    note      = request.form.get("note",      "").strip()

    # 驗證 item_key
    valid_keys = {k for k, _ in MASK_ITEM_KEYS}
    if item_key not in valid_keys:
        flash("請選擇有效的品項。", "error")
        return redirect(url_for("mask.inventory"))

    try:
        qty = int(qty_str)
        if qty <= 0:
            raise ValueError
    except (ValueError, TypeError):
        flash("補貨數量必須為正整數。", "error")
        return redirect(url_for("mask.inventory"))

    # 取得庫存物件並更新
    inv = MaskInventory.query.filter_by(item_key=item_key).first()
    if inv is None:
        flash(f"找不到庫存品項（{item_key}），請聯絡管理員。", "error")
        return redirect(url_for("mask.inventory"))

    inv.qty_on_hand += qty

    # 記錄補貨歷史
    purchase = MaskPurchase(
        date       = date_str,
        item_key   = item_key,
        qty        = qty,
        note       = note,
        created_at = datetime.datetime.utcnow(),
    )
    db.session.add(purchase)
    db.session.commit()

    flash(
        f"已為「{inv.name}」補貨 {qty}，目前庫存 {inv.qty_on_hand}。",
        "success"
    )
    return redirect(url_for("mask.inventory"))

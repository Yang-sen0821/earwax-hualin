"""reports blueprint（/reports）— 報表 + 財務 + 匯出 Excel（D10 / D11 / D12 / D13）。

本檔只讀資料、做聚合與匯出，不變動任何庫存量（不碰 inventory_balances / inventory_movements 寫入）。
- D10 報表：銷售日/月/年報、商品銷售排行（依 order_items.combo_code 聚合）、庫存報表（目前/預留/已售/公關品）。
- D11 匯出 Excel：訂單 / 庫存 / 庫存異動 / 銷售報表 四類（openpyxl）。
- D12/D13 財務：毛利 = 銷售金額 − 商品成本；淨利 = 毛利 − 包材 − 行銷 − 其他支出。
  來源：financial_entries（sale / product_cost / packaging / marketing 等分錄）+ extra_expenses（其他支出）。

權限（§六）：reports / finance 讀取 → 全角色可讀（viewer 以上）；故僅 @login_required。
"""
from datetime import datetime
from io import BytesIO
from decimal import Decimal

from flask import (
    Blueprint, render_template, request, send_file,
    redirect, url_for, flash, abort,
)
from sqlalchemy import func

from auth import login_required, role_required, current_user
from db import (
    get_session,
    Order, OrderItem, Product, SalesPlan, Customer,
    InventoryBalance, InventoryMovement,
    FinancialEntry, ExtraExpense, Setting,
    COMBO_CODES,
)
from display_labels import (
    combo_label, pool_label, payment_label, shipping_label, mtype_label,
)

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")

# =========================================================================
# 支出管理：唯一成本來源 = extra_expenses by category
# 類別固定下拉（單一成本來源；ROI/財務一律依此加總，禁止第二套來源）
# =========================================================================
EXPENSE_CATEGORIES = ("設備", "面膜進貨成本", "建檔費", "包材", "行銷", "其他")

# ROI/財務成本側對照：類別 → 聚合鍵
CATEGORY_PRODUCT = "面膜進貨成本"   # 商品成本
CATEGORY_PACKAGING = "包材"         # 包材成本
CATEGORY_MARKETING = "行銷"         # 行銷成本
CATEGORY_SETUP = "建檔費"           # 建檔額外支出
CATEGORY_EQUIPMENT = "設備"         # 設備（投資，計入總投入與回本門檻）
CATEGORY_OTHER = "其他"             # 其他支出


def _uid():
    u = current_user()
    return u.id if u else None


# =========================================================================
# 共用：combo_code 顯示名稱（依 sales_plans seed；查不到退回 code 本身）
# =========================================================================
def _combo_label_map(db):
    rows = db.query(SalesPlan.combo_code, SalesPlan.name).all()
    m = {code: name for code, name in rows}
    # 確保四個閉集 code 都有 key
    fallback = {"SINGLE": "單片體驗組", "BOX1": "經典盒裝",
                "BOX3": "植萃養膚組", "BOX10": "尊寵囤貨組"}
    for c in COMBO_CODES:
        m.setdefault(c, fallback.get(c, c))
    return m


def _to_float(v):
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


# =========================================================================
# 共用：銷售期間分組標籤（SQLite / Postgres 皆相容，於 Python 端分組）
# =========================================================================
def _period_key(dt, granularity):
    if dt is None:
        return "(未知)"
    if granularity == "day":
        return dt.strftime("%Y-%m-%d")
    if granularity == "year":
        return dt.strftime("%Y")
    return dt.strftime("%Y-%m")  # month（預設）


def _sales_by_period(db, granularity="month"):
    """以 orders 為基礎，依期間聚合訂單數與銷售金額。

    銷售金額採 orders.total_amount（下單當下總額快照）。
    """
    rows = db.query(Order.created_at, Order.total_amount).all()
    bucket = {}
    for created_at, total in rows:
        key = _period_key(created_at, granularity)
        b = bucket.setdefault(key, {"period": key, "order_count": 0, "amount": 0.0})
        b["order_count"] += 1
        b["amount"] += _to_float(total)
    return sorted(bucket.values(), key=lambda r: r["period"])


# =========================================================================
# 共用：商品銷售排行（依 order_items.combo_code 聚合 單片/盒裝/3盒/10盒）
# =========================================================================
def _sales_ranking(db):
    label = _combo_label_map(db)
    rows = (
        db.query(
            OrderItem.combo_code,
            func.count(OrderItem.id),
            func.coalesce(func.sum(OrderItem.qty), 0),
            func.coalesce(func.sum(OrderItem.subtotal), 0),
        )
        .group_by(OrderItem.combo_code)
        .all()
    )
    result = []
    for combo_code, line_count, total_qty, total_amount in rows:
        result.append({
            "combo_code": combo_code,
            "combo_name": label.get(combo_code, combo_code),
            "line_count": int(line_count or 0),
            "total_qty": int(total_qty or 0),
            "total_amount": _to_float(total_amount),
        })
    result.sort(key=lambda r: r["total_qty"], reverse=True)
    return result


# =========================================================================
# 共用：庫存報表（目前 / 預留 / 已售 / 公關品）
# =========================================================================
def _inventory_report(db):
    """彙整各 (商品 × 池) 的庫存切片：
      - 目前(normal) / 預留(reserved) / 公關品(pr) / 試用(trial) / 損耗(scrap)
      - 已售：由 inventory_movements movement_type=SALE 的 |qty_delta| 累計
    """
    # balances：依 product 聚合各 category
    bal_rows = (
        db.query(
            InventoryBalance.product_id,
            InventoryBalance.inventory_pool,
            InventoryBalance.stock_category,
            InventoryBalance.qty,
            InventoryBalance.unit,
        )
        .all()
    )
    # 已售：SALE 異動累計（qty_delta 為負，取絕對值）
    sold_rows = (
        db.query(
            InventoryMovement.product_id,
            func.coalesce(func.sum(InventoryMovement.qty_delta), 0),
        )
        .filter(InventoryMovement.movement_type == "SALE")
        .group_by(InventoryMovement.product_id)
        .all()
    )
    sold_map = {pid: abs(int(s or 0)) for pid, s in sold_rows}

    # 商品名稱
    prods = {p.id: p for p in db.query(Product).all()}

    # 依 (product_id, pool) 組合彙整
    grid = {}
    for pid, pool, cat, qty, unit in bal_rows:
        key = (pid, pool)
        row = grid.setdefault(key, {
            "product_id": pid,
            "product_name": prods[pid].name if pid in prods else f"#{pid}",
            "sku": prods[pid].sku if pid in prods else "",
            "inventory_pool": pool,
            "unit": unit,
            "normal": 0, "reserved": 0, "pr": 0, "trial": 0, "scrap": 0,
        })
        if cat in row:
            row[cat] += int(qty or 0)

    result = list(grid.values())
    # 已售掛在商品層（不分池）→ 標在該商品第一個池列，其餘填 0 避免重複計
    counted = set()
    for row in result:
        pid = row["product_id"]
        if pid not in counted:
            row["sold"] = sold_map.get(pid, 0)
            counted.add(pid)
        else:
            row["sold"] = 0
    result.sort(key=lambda r: (r["product_id"], r["inventory_pool"]))
    return result


# =========================================================================
# 共用：成本聚合（唯一來源）= extra_expenses 依 category 加總
# ROI 與財務的成本側全部走這裡，絕不再從 settings cost_*_test 或
# financial_entries(product_cost/packaging/marketing) 取成本，避免重複計算。
# =========================================================================
def _cost_by_category(db):
    """回傳各成本類別小計（dict）+ 衍生的彙整欄位。

    商品成本 = Σ(面膜進貨成本)
    包材成本 = Σ(包材)
    行銷成本 = Σ(行銷)
    建檔額外支出 = Σ(建檔費)
    設備 = Σ(設備)  ← 投資，計入總投入成本與回本門檻
    其他支出 = Σ(其他)
    總投入成本 = 以上全部加總
    """
    rows = (
        db.query(
            ExtraExpense.category,
            func.coalesce(func.sum(ExtraExpense.amount), 0),
        )
        .group_by(ExtraExpense.category)
        .all()
    )
    by_cat = {cat: 0.0 for cat in EXPENSE_CATEGORIES}
    uncategorized = 0.0
    for cat, amt in rows:
        a = _to_float(amt)
        if cat in by_cat:
            by_cat[cat] += a
        else:
            # 類別空白/非閉集者，歸入「其他」以免漏算（單一來源不丟資料）
            uncategorized += a
    by_cat[CATEGORY_OTHER] += uncategorized

    product_cost = by_cat[CATEGORY_PRODUCT]
    packaging_cost = by_cat[CATEGORY_PACKAGING]
    marketing_cost = by_cat[CATEGORY_MARKETING]
    setup_cost = by_cat[CATEGORY_SETUP]
    equipment_cost = by_cat[CATEGORY_EQUIPMENT]
    other_cost = by_cat[CATEGORY_OTHER]
    total_cost = (product_cost + packaging_cost + marketing_cost
                  + setup_cost + equipment_cost + other_cost)
    return {
        "by_cat": by_cat,
        "product_cost": product_cost,
        "packaging_cost": packaging_cost,
        "marketing_cost": marketing_cost,
        "setup_cost": setup_cost,
        "equipment_cost": equipment_cost,
        "other_cost": other_cost,
        "total_cost": total_cost,
    }


# =========================================================================
# 共用：財務報表（毛利 / 淨利）D12 / D13
# 成本來源：唯一 = extra_expenses by category（不再讀 financial_entries 成本分錄）
# 收入來源：financial_entries(sale) 優先，無則回退 orders.total_amount（維持不動）
# =========================================================================
def _financial_report(db):
    """毛利 = 銷售金額 − 商品成本
       淨利 = 銷售金額 − 總投入成本

    成本側單一來源 = extra_expenses by category：
      商品成本=面膜進貨成本、包材=包材、行銷=行銷、
      建檔=建檔費、設備=設備、其他=其他 → 總投入成本為全部加總。
    收入側維持原口徑（financial_entries sale 或 orders 總額）。
    """
    cost = _cost_by_category(db)

    # ---- 收入側（不動）----
    sale_entries = _to_float(
        db.query(func.coalesce(func.sum(FinancialEntry.amount), 0))
        .filter(FinancialEntry.entry_type == "sale")
        .scalar()
    )
    orders_total = _to_float(
        db.query(func.coalesce(func.sum(Order.total_amount), 0)).scalar()
    )
    sales_amount = sale_entries if sale_entries > 0 else orders_total

    product_cost = cost["product_cost"]
    total_cost = cost["total_cost"]

    gross_profit = sales_amount - product_cost
    net_profit = sales_amount - total_cost

    return {
        "sales_amount": sales_amount,
        "orders_total": orders_total,
        "product_cost": product_cost,
        "packaging_cost": cost["packaging_cost"],
        "marketing_cost": cost["marketing_cost"],
        "setup_cost": cost["setup_cost"],
        "equipment_cost": cost["equipment_cost"],
        "other_expense": cost["other_cost"],
        "total_cost": total_cost,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
        "by_cat": cost["by_cat"],
    }


# =========================================================================
# 共用：成本與利潤（投報率 ROI）— 限 owner / accounting
# =========================================================================
def _roi_report(db):
    """成本與利潤（投報率 ROI）聚合，唯讀。

    累積總收入口徑（不動）：
      - 優先採 financial_entries 的 sale 分錄加總；
      - 若無 sale 分錄，則以「已認列營收」訂單為準＝orders 中
        payment_status='paid' 或 shipping_status ∈ (shipped, delivered) 的 total_amount 加總。

    成本各項口徑（唯一來源 = extra_expenses by category）：
      - 商品成本     = Σ(面膜進貨成本)
      - 包材成本     = Σ(包材)
      - 行銷成本     = Σ(行銷)
      - 建檔額外支出 = Σ(建檔費)
      - 設備         = Σ(設備)  ← 投資，計入總投入成本與回本門檻
      - 其他支出     = Σ(其他)
      總投入成本 ＝ 以上全部加總

    淨利 ＝ 總收入 − 總投入成本
    毛利 ＝ 總收入 − 商品成本
    ROI% ＝ 淨利 ÷ 總投入成本 × 100（總成本為 0 時回 None → 前台顯示 N/A，不除以零）
    """
    # ---- 累積總收入（收入側維持不動）----
    sale_entries = _to_float(
        db.query(func.coalesce(func.sum(FinancialEntry.amount), 0))
        .filter(FinancialEntry.entry_type == "sale")
        .scalar()
    )
    recognized_orders_total = _to_float(
        db.query(func.coalesce(func.sum(Order.total_amount), 0))
        .filter(
            (Order.payment_status == "paid")
            | (Order.shipping_status.in_(("shipped", "delivered")))
        )
        .scalar()
    )
    if sale_entries > 0:
        total_revenue = sale_entries
        revenue_basis = "financial_entries(sale)"
    else:
        total_revenue = recognized_orders_total
        revenue_basis = "orders(已付款/已出貨)"

    # ---- 成本各項（唯一來源 = extra_expenses by category）----
    cost = _cost_by_category(db)
    product_cost = cost["product_cost"]
    packaging_cost = cost["packaging_cost"]
    marketing_cost = cost["marketing_cost"]
    setup_cost = cost["setup_cost"]
    equipment_cost = cost["equipment_cost"]
    other_expense = cost["other_cost"]
    total_cost = cost["total_cost"]

    gross_profit = total_revenue - product_cost
    net_profit = total_revenue - total_cost
    roi_pct = (net_profit / total_cost * 100) if total_cost > 0 else None

    # ---- 回本計算（沿用既有「總投入成本」與「累積收入」口徑，不重算）----
    # 回本門檻 = 總投入成本；已回收 = 累積收入
    # 回本進度% = 累積收入 ÷ 總投入成本 × 100（總成本 0 → None → 前台 N/A，防除零）
    # 尚需回收 = max(0, 總投入成本 − 累積收入)
    # 累積收入 ≥ 總投入成本 → 已回本，淨賺 = 累積收入 − 總投入成本
    payback_threshold = total_cost          # 回本門檻
    recovered = total_revenue               # 已回收
    payback_pct = (recovered / total_cost * 100) if total_cost > 0 else None
    remaining_to_recover = max(0.0, total_cost - total_revenue)
    paid_back = (total_cost > 0) and (total_revenue >= total_cost)
    net_earned = (total_revenue - total_cost) if paid_back else 0.0

    return {
        "total_revenue": total_revenue,
        "revenue_basis": revenue_basis,
        "sale_entries": sale_entries,
        "recognized_orders_total": recognized_orders_total,
        "product_cost": product_cost,
        "packaging_cost": packaging_cost,
        "marketing_cost": marketing_cost,
        "setup_cost": setup_cost,
        "equipment_cost": equipment_cost,
        "other_expense": other_expense,
        "by_cat": cost["by_cat"],
        "total_cost": total_cost,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
        "roi_pct": roi_pct,
        # ---- 回本計算 ----
        "payback_threshold": payback_threshold,
        "recovered": recovered,
        "payback_pct": payback_pct,
        "remaining_to_recover": remaining_to_recover,
        "paid_back": paid_back,
        "net_earned": net_earned,
    }


# =========================================================================
# 頁面：報表首頁（彙整入口 + 簡要 KPI）
# =========================================================================
@reports_bp.route("/")
@login_required
def index():
    db = get_session()
    fin = _financial_report(db)
    ranking = _sales_ranking(db)
    kpi = {
        "order_count": db.query(func.count(Order.id)).scalar() or 0,
        "customer_count": db.query(func.count(Customer.id)).scalar() or 0,
        "sales_amount": fin["sales_amount"],
        "net_profit": fin["net_profit"],
    }
    return render_template("reports/index.html", section="reports",
                           kpi=kpi, ranking=ranking, fin=fin)


# =========================================================================
# 頁面：銷售報表（日 / 月 / 年）+ 排行
# =========================================================================
@reports_bp.route("/sales")
@login_required
def sales():
    db = get_session()
    granularity = request.args.get("g", "month")
    if granularity not in ("day", "month", "year"):
        granularity = "month"
    periods = _sales_by_period(db, granularity)
    ranking = _sales_ranking(db)
    totals = {
        "order_count": sum(p["order_count"] for p in periods),
        "amount": sum(p["amount"] for p in periods),
    }
    return render_template("reports/sales.html", section="reports",
                           granularity=granularity, periods=periods,
                           ranking=ranking, totals=totals)


# =========================================================================
# 頁面：庫存報表（目前 / 預留 / 已售 / 公關品）
# =========================================================================
@reports_bp.route("/inventory")
@login_required
def inventory():
    db = get_session()
    rows = _inventory_report(db)
    return render_template("reports/inventory.html", section="reports", rows=rows)


# =========================================================================
# 頁面：財務報表（毛利 / 淨利）
# =========================================================================
@reports_bp.route("/finance")
@login_required
def finance():
    db = get_session()
    fin = _financial_report(db)
    return render_template("reports/finance.html", section="reports", fin=fin)


# =========================================================================
# 頁面：成本與利潤（投報率 ROI）— 財務機密，限 owner / accounting
# =========================================================================
@reports_bp.route("/roi")
@role_required("owner", "accounting")
def roi():
    """ROI 頁：唯讀。成本各類別明細只顯示，不在此編輯（編輯走支出管理頁）。"""
    db = get_session()
    roi_data = _roi_report(db)
    return render_template(
        "reports/roi.html", section="reports", roi=roi_data,
        categories=EXPENSE_CATEGORIES,
    )


# =========================================================================
# 支出管理（/reports/expenses）— 唯一成本來源：extra_expenses by category
# 品項列管：name + amount + category + expense_date + note（vendor 不在 schema，置於 note）
# 權限：owner / accounting（@role_required）
# 收入、已售、庫存維持唯讀自動，本模組不碰。
# =========================================================================
def _parse_amount(raw):
    """解析金額字串為 float；回 (ok, value, err)。空/非數值/負數回錯。"""
    raw = (raw or "").strip()
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return False, None, "金額需為數字"
    if v < 0:
        return False, None, "金額不得為負"
    return True, v, None


def _parse_date(raw):
    """解析 YYYY-MM-DD；空字串回 (True, None)；格式錯回 (False, None, err)。"""
    raw = (raw or "").strip()
    if not raw:
        return True, None, None
    try:
        return True, datetime.strptime(raw, "%Y-%m-%d"), None
    except ValueError:
        return False, None, "日期格式需為 YYYY-MM-DD"


@reports_bp.route("/expenses")
@role_required("owner", "accounting")
def expenses():
    db = get_session()
    rows = db.query(ExtraExpense).order_by(
        ExtraExpense.category, ExtraExpense.id).all()
    # 依類別分組（依固定下拉順序），各類別小計 + 總計
    grouped = {cat: [] for cat in EXPENSE_CATEGORIES}
    other_key = CATEGORY_OTHER
    subtotals = {cat: 0.0 for cat in EXPENSE_CATEGORIES}
    grand_total = 0.0
    for e in rows:
        cat = e.category if e.category in grouped else other_key
        grouped[cat].append(e)
        amt = _to_float(e.amount)
        subtotals[cat] += amt
        grand_total += amt
    groups = [
        {"category": cat, "rows": grouped[cat], "subtotal": subtotals[cat]}
        for cat in EXPENSE_CATEGORIES
    ]
    return render_template(
        "reports/expenses.html", section="expenses",
        groups=groups, grand_total=grand_total,
        categories=EXPENSE_CATEGORIES,
    )


@reports_bp.route("/expenses/add", methods=["POST"])
@role_required("owner", "accounting")
def expense_add():
    db = get_session()
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    if not name:
        flash("品項名稱為必填", "error")
        return redirect(url_for("reports.expenses"))
    if category not in EXPENSE_CATEGORIES:
        flash("類別不合法（須為固定下拉之一）", "error")
        return redirect(url_for("reports.expenses"))
    ok, val, err = _parse_amount(request.form.get("amount", ""))
    if not ok:
        flash(f"支出新增失敗：{err}", "error")
        return redirect(url_for("reports.expenses"))
    dok, dt, derr = _parse_date(request.form.get("expense_date", ""))
    if not dok:
        flash(f"支出新增失敗：{derr}", "error")
        return redirect(url_for("reports.expenses"))
    db.add(ExtraExpense(
        name=name, amount=val, category=category,
        expense_date=dt or datetime.utcnow(),
        note=request.form.get("note", "").strip() or None,
        created_by=_uid(),
    ))
    db.commit()
    flash(f"支出「{name}」已新增（{category} {val:.2f}）", "ok")
    return redirect(url_for("reports.expenses"))


@reports_bp.route("/expenses/<int:eid>/update", methods=["POST"])
@role_required("owner", "accounting")
def expense_update(eid):
    db = get_session()
    e = db.query(ExtraExpense).get(eid)
    if not e:
        abort(404)
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    if not name:
        flash("品項名稱為必填", "error")
        return redirect(url_for("reports.expenses"))
    if category not in EXPENSE_CATEGORIES:
        flash("類別不合法（須為固定下拉之一）", "error")
        return redirect(url_for("reports.expenses"))
    ok, val, err = _parse_amount(request.form.get("amount", ""))
    if not ok:
        flash(f"支出更新失敗：{err}", "error")
        return redirect(url_for("reports.expenses"))
    dok, dt, derr = _parse_date(request.form.get("expense_date", ""))
    if not dok:
        flash(f"支出更新失敗：{derr}", "error")
        return redirect(url_for("reports.expenses"))
    e.name = name
    e.amount = val
    e.category = category
    if dt is not None:
        e.expense_date = dt
    e.note = request.form.get("note", "").strip() or None
    db.commit()
    flash(f"支出「{name}」已更新（{category} {val:.2f}）", "ok")
    return redirect(url_for("reports.expenses"))


@reports_bp.route("/expenses/<int:eid>/delete", methods=["POST"])
@role_required("owner", "accounting")
def expense_delete(eid):
    db = get_session()
    e = db.query(ExtraExpense).get(eid)
    if not e:
        abort(404)
    nm = e.name
    db.delete(e)
    db.commit()
    flash(f"支出「{nm}」已刪除", "ok")
    return redirect(url_for("reports.expenses"))


# =========================================================================
# 匯出 Excel（D11）— 單一按鈕、一份 .xlsx、多工作表分頁。
# 分頁：訂單 / 庫存 / 庫存異動 / 銷售報表（期間 + 排行）。
# 內容全中文（沿用 display_labels 對照：狀態 / 組合 / 庫存池 / 異動類別）。
# 權限：@login_required（沿用原匯出頁角色限制：viewer 以上可讀可匯）。
# =========================================================================
def _new_workbook():
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    return wb, Font


def _write_header(ws, headers, Font):
    ws.append(headers)
    bold = Font(bold=True)
    for cell in ws[1]:
        cell.font = bold


def _send_xlsx(wb, filename):
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _ts():
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def _sheet_orders(wb, db, Font):
    """工作表：訂單（首頁，沿用 wb.active，避免留空白 Sheet）。"""
    ws = wb.active
    ws.title = "訂單"
    _write_header(ws, [
        "訂單編號", "客戶", "收件人", "電話", "地址",
        "總金額", "付款狀態", "出貨狀態", "備註", "建立時間",
    ], Font)
    cust = {c.id: c.name for c in db.query(Customer).all()}
    for o in db.query(Order).order_by(Order.id).all():
        ws.append([
            o.order_no,
            cust.get(o.customer_id, ""),
            o.recipient_name or "",
            o.recipient_phone or "",
            o.shipping_address or "",
            _to_float(o.total_amount),
            payment_label(o.payment_status),
            shipping_label(o.shipping_status),
            o.note or "",
            o.created_at.strftime("%Y-%m-%d %H:%M") if o.created_at else "",
        ])


def _sheet_inventory(wb, db, Font):
    """工作表：庫存。"""
    ws = wb.create_sheet("庫存")
    _write_header(ws, [
        "SKU", "商品", "庫存池", "目前", "預留",
        "公關品", "試用品", "損耗報廢", "已售(累計)", "單位",
    ], Font)
    for r in _inventory_report(db):
        ws.append([
            r["sku"], r["product_name"], pool_label(r["inventory_pool"]),
            r["normal"], r["reserved"], r["pr"], r["trial"], r["scrap"],
            r["sold"], r["unit"] or "",
        ])


def _sheet_movements(wb, db, Font):
    """工作表：庫存異動。"""
    ws = wb.create_sheet("庫存異動")
    _write_header(ws, [
        "異動ID", "SKU", "商品", "庫存池", "從分類", "到分類",
        "變動量", "變動前", "變動後", "異動類別", "來源類型", "來源ID",
        "群組ID", "原因", "經手人", "備註", "時間",
    ], Font)
    prods = {p.id: p for p in db.query(Product).all()}
    for m in db.query(InventoryMovement).order_by(InventoryMovement.movement_id).all():
        p = prods.get(m.product_id)
        ws.append([
            m.movement_id,
            p.sku if p else "",
            p.name if p else f"#{m.product_id}",
            pool_label(m.inventory_pool),
            m.stock_category_from or "",
            m.stock_category_to or "",
            m.qty_delta,
            m.qty_before,
            m.qty_after,
            mtype_label(m.movement_type),
            m.ref_type or "",
            m.ref_id or "",
            m.movement_group_id or "",
            m.reason or "",
            m.operator or "",
            m.note or "",
            m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "",
        ])


def _sheet_sales(wb, db, Font, granularity):
    """工作表：銷售報表（期間銷售 + 商品銷售排行，合併同一分頁）。"""
    ws = wb.create_sheet("銷售報表")
    label = {"day": "日報", "month": "月報", "year": "年報"}[granularity]

    # 區塊一：期間銷售
    ws.append([f"期間銷售（{label}）"])
    ws["A1"].font = Font(bold=True)
    _write_header(ws, [f"期間（{label}）", "訂單數", "銷售金額"], Font)
    for p in _sales_by_period(db, granularity):
        ws.append([p["period"], p["order_count"], p["amount"]])

    # 空一列後接區塊二：商品銷售排行
    ws.append([])
    title_row = ws.max_row + 1
    ws.append(["商品銷售排行"])
    ws[f"A{title_row}"].font = Font(bold=True)
    _write_header(ws, ["組合代碼", "組合名稱", "明細筆數", "銷售組數", "銷售金額"], Font)
    for r in _sales_ranking(db):
        ws.append([r["combo_code"], combo_label(r["combo_code"]),
                   r["line_count"], r["total_qty"], r["total_amount"]])


@reports_bp.route("/export.xlsx")
@login_required
def export_all():
    """單鍵匯出：一份 .xlsx，含訂單 / 庫存 / 庫存異動 / 銷售報表 四個工作表。"""
    db = get_session()
    granularity = request.args.get("g", "month")
    if granularity not in ("day", "month", "year"):
        granularity = "month"

    wb, Font = _new_workbook()
    _sheet_orders(wb, db, Font)       # 首頁，用 wb.active（不留空白 Sheet）
    _sheet_inventory(wb, db, Font)
    _sheet_movements(wb, db, Font)
    _sheet_sales(wb, db, Font, granularity)

    return _send_xlsx(wb, f"flora_court_{_ts()}.xlsx")

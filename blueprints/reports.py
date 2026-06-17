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

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")

# 報表頁可編輯的成本 settings key（與 _roi_report 讀取的同一處，不另造來源）
COST_SETTING_KEYS = {
    "product": "cost_product_test",
    "packaging": "cost_packaging_test",
    "marketing": "cost_marketing_test",
}
# financial_entries 中「已知四類」之外者，於 ROI 聚合為「其他支出」(other_expense)
KNOWN_ENTRY_TYPES = ("sale", "product_cost", "packaging", "marketing")


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
# 共用：財務報表（毛利 / 淨利）D12 / D13
# =========================================================================
def _financial_report(db):
    """毛利 = 銷售金額 − 商品成本
       淨利 = 毛利 − 包材 − 行銷 − 其他支出

    來源：
      - financial_entries：entry_type ∈ {sale, product_cost, packaging, marketing, ...}
      - extra_expenses：其他支出（全部加總）
    銷售金額亦回退到 orders.total_amount（若 financial_entries 無 sale 分錄時供參考）。
    """
    fe_rows = (
        db.query(
            FinancialEntry.entry_type,
            func.coalesce(func.sum(FinancialEntry.amount), 0),
        )
        .group_by(FinancialEntry.entry_type)
        .all()
    )
    fe = {etype: _to_float(amt) for etype, amt in fe_rows}

    sales_amount = fe.get("sale", 0.0)
    product_cost = fe.get("product_cost", 0.0)
    packaging_cost = fe.get("packaging", 0.0)
    marketing_cost = fe.get("marketing", 0.0)

    # 銷售分錄缺漏時，以 orders 總額作參考基準
    orders_total = _to_float(
        db.query(func.coalesce(func.sum(Order.total_amount), 0)).scalar()
    )
    if sales_amount == 0.0 and orders_total > 0:
        sales_amount = orders_total

    other_expense = _to_float(
        db.query(func.coalesce(func.sum(ExtraExpense.amount), 0)).scalar()
    )

    gross_profit = sales_amount - product_cost
    net_profit = gross_profit - packaging_cost - marketing_cost - other_expense

    return {
        "sales_amount": sales_amount,
        "orders_total": orders_total,
        "product_cost": product_cost,
        "packaging_cost": packaging_cost,
        "marketing_cost": marketing_cost,
        "other_expense": other_expense,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
        # 其他分錄明細（除已知四類外）
        "other_entries": {k: v for k, v in fe.items()
                          if k not in ("sale", "product_cost", "packaging", "marketing")},
    }


# =========================================================================
# 共用：成本與利潤（投報率 ROI）— 限 owner / accounting
# =========================================================================
def _setting_float(db, key):
    """讀 settings 數值型測試值（§七：成本先用測試值，後台可改）。查無/非數值回 0。"""
    s = db.query(Setting).filter_by(key=key).first()
    if not s or s.value is None:
        return 0.0
    try:
        return float(s.value)
    except (TypeError, ValueError):
        return 0.0


def _roi_report(db):
    """成本與利潤（投報率 ROI）聚合，唯讀。

    累積總收入口徑：
      - 優先採 financial_entries 的 sale 分錄加總；
      - 若無 sale 分錄，則以「已認列營收」訂單為準＝orders 中
        payment_status='paid' 或 shipping_status ∈ (shipped, delivered) 的 total_amount 加總
        （排除未付款且未出貨的草稿/待處理單）。

    成本各項口徑（成本＝分錄優先，無分錄時退回 settings 測試值；§七）：
      - 商品成本 product_cost ：financial_entries(product_cost) 或 settings.cost_product_test
      - 包材成本 packaging     ：financial_entries(packaging)   或 settings.cost_packaging_test
      - 行銷成本 marketing     ：financial_entries(marketing)   或 settings.cost_marketing_test
      - 建檔額外支出 extra     ：extra_expenses 全部加總（PIF/貼紙小卡/紙袋加價/外盒加價/版模費等）
      - 其他支出 other_entries ：financial_entries 中上述四類以外的分錄加總
      總投入成本 ＝ 商品 + 包材 + 行銷 + 建檔額外支出 + 其他

    淨利 ＝ 總收入 − 總投入成本
    毛利 ＝ 總收入 − 商品成本
    ROI% ＝ 淨利 ÷ 總投入成本 × 100（總成本為 0 時回 None → 前台顯示 N/A，不除以零）
    """
    fe_rows = (
        db.query(
            FinancialEntry.entry_type,
            func.coalesce(func.sum(FinancialEntry.amount), 0),
        )
        .group_by(FinancialEntry.entry_type)
        .all()
    )
    fe = {etype: _to_float(amt) for etype, amt in fe_rows}

    # ---- 累積總收入 ----
    sale_entries = fe.get("sale", 0.0)
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

    # ---- 成本各項（分錄優先，無則退回 settings 測試值）----
    # 旗標：若該類 financial_entry 存在且非零，則 settings 測試值被覆寫（前台顯示鎖定，避免編輯後不反映的錯覺）
    fe_product = fe.get("product_cost", 0.0)
    fe_packaging = fe.get("packaging", 0.0)
    fe_marketing = fe.get("marketing", 0.0)
    product_cost = fe_product or _setting_float(db, "cost_product_test")
    packaging_cost = fe_packaging or _setting_float(db, "cost_packaging_test")
    marketing_cost = fe_marketing or _setting_float(db, "cost_marketing_test")
    product_overridden = fe_product != 0.0
    packaging_overridden = fe_packaging != 0.0
    marketing_overridden = fe_marketing != 0.0

    extra_expense = _to_float(
        db.query(func.coalesce(func.sum(ExtraExpense.amount), 0)).scalar()
    )

    known = KNOWN_ENTRY_TYPES
    other_entries = {k: v for k, v in fe.items() if k not in known}
    other_expense = sum(other_entries.values())

    total_cost = product_cost + packaging_cost + marketing_cost + extra_expense + other_expense

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
        "product_overridden": product_overridden,
        "packaging_overridden": packaging_overridden,
        "marketing_overridden": marketing_overridden,
        "extra_expense": extra_expense,
        "other_expense": other_expense,
        "other_entries": other_entries,
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
    db = get_session()
    roi_data = _roi_report(db)
    # 成本三項目前可編輯的 settings 值（與 _roi_report 同一來源）
    cost_settings = {
        "product": _setting_float(db, "cost_product_test"),
        "packaging": _setting_float(db, "cost_packaging_test"),
        "marketing": _setting_float(db, "cost_marketing_test"),
    }
    # 建檔額外支出明細（extra_expenses）— 可新增/編輯/刪除
    extras = db.query(ExtraExpense).order_by(ExtraExpense.id).all()
    # 其他支出明細（financial_entries 中已知四類之外）— 可新增/編輯/刪除
    other_entries_rows = (
        db.query(FinancialEntry)
        .filter(~FinancialEntry.entry_type.in_(KNOWN_ENTRY_TYPES))
        .order_by(FinancialEntry.id)
        .all()
    )
    return render_template(
        "reports/roi.html", section="reports", roi=roi_data,
        cost_settings=cost_settings, extras=extras,
        other_entries_rows=other_entries_rows,
        can_edit=current_user().role in ("owner", "accounting"),
    )


# =========================================================================
# 報表頁成本支出編輯（限 owner / accounting）— 只開放「成本/支出」，收入/庫存維持唯讀
# 來源對齊 _roi_report 讀取處：
#   - 商品/包材/行銷成本 → settings.cost_*_test（沿用既有 key，不另造來源）
#   - 建檔額外支出        → extra_expenses 表（name + amount）
#   - 其他支出            → financial_entries 中「已知四類之外」的分錄
# 存檔後 redirect 回 ROI，重算後的數字即時反映。
# =========================================================================
def _parse_amount(raw):
    """解析金額字串為 float；回 (ok, value, err)。空/非數值/負數視情況回錯。"""
    raw = (raw or "").strip()
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return False, None, "金額需為數字"
    if v < 0:
        return False, None, "金額不得為負"
    return True, v, None


@reports_bp.route("/roi/cost/update", methods=["POST"])
@role_required("owner", "accounting")
def roi_cost_update():
    """更新商品/包材/行銷成本 → 寫回 settings.cost_*_test（與 ROI 讀取同一處）。"""
    db = get_session()
    kind = request.form.get("kind", "").strip()
    key = COST_SETTING_KEYS.get(kind)
    if not key:
        abort(400)
    ok, val, err = _parse_amount(request.form.get("value", ""))
    if not ok:
        flash(f"成本更新失敗：{err}", "error")
        return redirect(url_for("reports.roi"))
    s = db.query(Setting).filter_by(key=key).first()
    if not s:
        # 沿用 init 既有 key；若意外不存在則建立同型別列，不另造新來源命名
        s = Setting(key=key, value_type="float",
                    description="成本測試值（報表頁可編輯）")
        db.add(s)
    s.value = str(val)
    s.value_type = s.value_type or "float"
    s.updated_by = _uid()
    s.updated_at = datetime.utcnow()
    db.commit()
    label = {"product": "商品成本", "packaging": "包材成本", "marketing": "行銷成本"}[kind]
    flash(f"{label} 已更新為 {val:.2f}", "ok")
    return redirect(url_for("reports.roi"))


@reports_bp.route("/roi/extra/add", methods=["POST"])
@role_required("owner", "accounting")
def roi_extra_add():
    """新增一筆建檔額外支出 → extra_expenses。"""
    db = get_session()
    name = request.form.get("name", "").strip()
    if not name:
        flash("建檔額外支出：項目名稱為必填", "error")
        return redirect(url_for("reports.roi"))
    ok, val, err = _parse_amount(request.form.get("amount", ""))
    if not ok:
        flash(f"建檔額外支出：{err}", "error")
        return redirect(url_for("reports.roi"))
    db.add(ExtraExpense(name=name, amount=val, created_by=_uid()))
    db.commit()
    flash(f"建檔額外支出「{name}」已新增（{val:.2f}）", "ok")
    return redirect(url_for("reports.roi"))


@reports_bp.route("/roi/extra/<int:eid>/update", methods=["POST"])
@role_required("owner", "accounting")
def roi_extra_update(eid):
    """編輯一筆建檔額外支出（名稱 + 金額）。"""
    db = get_session()
    e = db.query(ExtraExpense).get(eid)
    if not e:
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        flash("建檔額外支出：項目名稱為必填", "error")
        return redirect(url_for("reports.roi"))
    ok, val, err = _parse_amount(request.form.get("amount", ""))
    if not ok:
        flash(f"建檔額外支出：{err}", "error")
        return redirect(url_for("reports.roi"))
    e.name = name
    e.amount = val
    db.commit()
    flash(f"建檔額外支出「{name}」已更新（{val:.2f}）", "ok")
    return redirect(url_for("reports.roi"))


@reports_bp.route("/roi/extra/<int:eid>/delete", methods=["POST"])
@role_required("owner", "accounting")
def roi_extra_delete(eid):
    """刪除一筆建檔額外支出。"""
    db = get_session()
    e = db.query(ExtraExpense).get(eid)
    if not e:
        abort(404)
    nm = e.name
    db.delete(e)
    db.commit()
    flash(f"建檔額外支出「{nm}」已刪除", "ok")
    return redirect(url_for("reports.roi"))


@reports_bp.route("/roi/other/add", methods=["POST"])
@role_required("owner", "accounting")
def roi_other_add():
    """新增一筆其他支出 → financial_entries（entry_type 不得為已知四類）。"""
    db = get_session()
    etype = request.form.get("entry_type", "").strip()
    if not etype:
        flash("其他支出：分錄類型為必填", "error")
        return redirect(url_for("reports.roi"))
    if etype in KNOWN_ENTRY_TYPES:
        flash(f"其他支出：分錄類型不得為 {etype}（屬收入/成本主類，請用上方欄位）", "error")
        return redirect(url_for("reports.roi"))
    ok, val, err = _parse_amount(request.form.get("amount", ""))
    if not ok:
        flash(f"其他支出：{err}", "error")
        return redirect(url_for("reports.roi"))
    db.add(FinancialEntry(
        entry_type=etype, amount=val,
        description=request.form.get("description", "").strip() or None,
        created_by=_uid(),
    ))
    db.commit()
    flash(f"其他支出「{etype}」已新增（{val:.2f}）", "ok")
    return redirect(url_for("reports.roi"))


@reports_bp.route("/roi/other/<int:fid>/update", methods=["POST"])
@role_required("owner", "accounting")
def roi_other_update(fid):
    """編輯一筆其他支出（financial_entries；禁止改成收入/成本主類）。"""
    db = get_session()
    f = db.query(FinancialEntry).get(fid)
    if not f or f.entry_type in KNOWN_ENTRY_TYPES:
        abort(404)
    etype = request.form.get("entry_type", "").strip()
    if not etype:
        flash("其他支出：分錄類型為必填", "error")
        return redirect(url_for("reports.roi"))
    if etype in KNOWN_ENTRY_TYPES:
        flash(f"其他支出：分錄類型不得改為 {etype}", "error")
        return redirect(url_for("reports.roi"))
    ok, val, err = _parse_amount(request.form.get("amount", ""))
    if not ok:
        flash(f"其他支出：{err}", "error")
        return redirect(url_for("reports.roi"))
    f.entry_type = etype
    f.amount = val
    f.description = request.form.get("description", "").strip() or None
    db.commit()
    flash(f"其他支出「{etype}」已更新（{val:.2f}）", "ok")
    return redirect(url_for("reports.roi"))


@reports_bp.route("/roi/other/<int:fid>/delete", methods=["POST"])
@role_required("owner", "accounting")
def roi_other_delete(fid):
    """刪除一筆其他支出（限 financial_entries 已知四類之外）。"""
    db = get_session()
    f = db.query(FinancialEntry).get(fid)
    if not f or f.entry_type in KNOWN_ENTRY_TYPES:
        abort(404)
    et = f.entry_type
    db.delete(f)
    db.commit()
    flash(f"其他支出「{et}」已刪除", "ok")
    return redirect(url_for("reports.roi"))


# =========================================================================
# 匯出 Excel（D11）— 訂單 / 庫存 / 異動 / 銷售報表 四類
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


@reports_bp.route("/export/orders.xlsx")
@login_required
def export_orders():
    db = get_session()
    wb, Font = _new_workbook()
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
            o.payment_status,
            o.shipping_status,
            o.note or "",
            o.created_at.strftime("%Y-%m-%d %H:%M") if o.created_at else "",
        ])
    return _send_xlsx(wb, f"flora_court_orders_{_ts()}.xlsx")


@reports_bp.route("/export/inventory.xlsx")
@login_required
def export_inventory():
    db = get_session()
    wb, Font = _new_workbook()
    ws = wb.active
    ws.title = "庫存"
    _write_header(ws, [
        "SKU", "商品", "庫存池", "目前(normal)", "預留(reserved)",
        "公關品(pr)", "試用(trial)", "損耗(scrap)", "已售(累計)", "單位",
    ], Font)
    for r in _inventory_report(db):
        ws.append([
            r["sku"], r["product_name"], r["inventory_pool"],
            r["normal"], r["reserved"], r["pr"], r["trial"], r["scrap"],
            r["sold"], r["unit"] or "",
        ])
    return _send_xlsx(wb, f"flora_court_inventory_{_ts()}.xlsx")


@reports_bp.route("/export/movements.xlsx")
@login_required
def export_movements():
    db = get_session()
    wb, Font = _new_workbook()
    ws = wb.active
    ws.title = "庫存異動"
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
            m.inventory_pool,
            m.stock_category_from or "",
            m.stock_category_to or "",
            m.qty_delta,
            m.qty_before,
            m.qty_after,
            m.movement_type,
            m.ref_type or "",
            m.ref_id or "",
            m.movement_group_id or "",
            m.reason or "",
            m.operator or "",
            m.note or "",
            m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "",
        ])
    return _send_xlsx(wb, f"flora_court_movements_{_ts()}.xlsx")


@reports_bp.route("/export/sales.xlsx")
@login_required
def export_sales():
    db = get_session()
    granularity = request.args.get("g", "month")
    if granularity not in ("day", "month", "year"):
        granularity = "month"
    wb, Font = _new_workbook()

    # 工作表 1：期間銷售
    ws1 = wb.active
    ws1.title = "期間銷售"
    label = {"day": "日報", "month": "月報", "year": "年報"}[granularity]
    _write_header(ws1, [f"期間（{label}）", "訂單數", "銷售金額"], Font)
    for p in _sales_by_period(db, granularity):
        ws1.append([p["period"], p["order_count"], p["amount"]])

    # 工作表 2：商品銷售排行
    ws2 = wb.create_sheet("銷售排行")
    _write_header(ws2, ["組合代碼", "組合名稱", "明細筆數", "銷售組數", "銷售金額"], Font)
    for r in _sales_ranking(db):
        ws2.append([r["combo_code"], r["combo_name"],
                    r["line_count"], r["total_qty"], r["total_amount"]])

    return _send_xlsx(wb, f"flora_court_sales_{granularity}_{_ts()}.xlsx")

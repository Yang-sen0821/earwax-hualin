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

from flask import Blueprint, render_template, request, send_file
from sqlalchemy import func

from auth import login_required
from db import (
    get_session,
    Order, OrderItem, Product, SalesPlan, Customer,
    InventoryBalance, InventoryMovement,
    FinancialEntry, ExtraExpense,
    COMBO_CODES,
)

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


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

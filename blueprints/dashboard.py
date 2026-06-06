# blueprints/dashboard.py
# 儀表板 blueprint（dashboard_bp）
# 路由：/（首頁，僅 owner 可看）
#
# 指標（來自 SPEC 第 4 節）：
#   1. 愛啪啪回本進度：累計淨利 / products.total_cost
#   2. 全店總覽：愛啪啪單一主體（累計淨利、累計營收、客數）— 不含任何其他主體
#   3. 會員/非會員：人數、占比、各自累計淨利
#   4. 明星耗材：ServiceConsumable 加總各消耗品用量（含贈品），由多到少排名
#   5. 回本週期推估：依日期區間平均淨利/日 × 推估回本天數
#   6. 期間篩選：start / end

import datetime
from flask import Blueprint, render_template, request
from sqlalchemy import func
from db import db, ServiceRecord, Product, Consumable, ServiceConsumable
from auth import owner_required

dashboard_bp = Blueprint("dashboard", __name__)


# ──────────────────────────────────────────────
# 核心計算函式（可被測試或其他模組直接呼叫）
# ──────────────────────────────────────────────

def compute_dashboard(start=None, end=None):
    """計算儀表板所有指標，回傳 dict。

    參數：
        start (str|None)：篩選開始日期，格式 YYYY-MM-DD
        end   (str|None)：篩選結束日期，格式 YYYY-MM-DD

    回傳 dict 鍵值：
        period_profit        — 期間累計淨利
        period_record_count  — 期間施作筆數
        total_profit         — 全期累計淨利（不受 start/end 影響）
        total_revenue        — 全期累計實收（不受 start/end 影響）
        total_customer_count — 全期累計客數（不受 start/end 影響）
        payback_target       — 回本目標金額（products.total_cost）
        payback_progress     — 全期回本進度 % (0~100)
        payback_remaining    — 剩餘回本金額
        member_count         — 期間會員施作人次
        nonmember_count      — 期間非會員施作人次
        member_profit        — 期間會員累計淨利
        nonmember_profit     — 期間非會員累計淨利
        member_ratio         — 會員占比 % (0~100)
        nonmember_ratio      — 非會員占比 %
        top_consumables      — 期間消耗品排名 list[dict{name, total_qty}]，由多到少
        avg_profit_per_day   — 期間平均每日淨利（用於回本週期推估）
        days_to_payback      — 推估距離回本還需天數（None 表示無法推估）
        span_days            — 期間天數跨度
        start                — 篩選起始（原始輸入）
        end                  — 篩選結束（原始輸入）
    """

    # ── 基礎設定資料（不受篩選影響）─────────────────────────────
    product        = Product.query.first()
    payback_target = product.total_cost if product else 142000

    # ── 全期累計淨利（不受篩選影響，用來算回本進度）─────────────
    total_profit = float(
        db.session.query(
            func.coalesce(func.sum(ServiceRecord.profit), 0)
        ).scalar()
    )

    # ── 全期累計實收（全店總覽用）────────────────────────────────
    total_revenue = float(
        db.session.query(
            func.coalesce(func.sum(ServiceRecord.actual_price), 0)
        ).scalar()
    )

    # ── 全期累計客數（全店總覽用）────────────────────────────────
    total_customer_count = int(
        db.session.query(func.count(ServiceRecord.id)).scalar() or 0
    )

    # ── 回本進度 ─────────────────────────────────────────────────
    payback_progress  = min(100.0, total_profit / payback_target * 100) if payback_target else 0
    payback_remaining = max(0.0, payback_target - total_profit)

    # ── 建立期間查詢（受 start/end 影響）────────────────────────
    q = ServiceRecord.query
    if start:
        q = q.filter(ServiceRecord.date >= start)
    if end:
        q = q.filter(ServiceRecord.date <= end)

    # 期間基礎統計
    period_rows          = q.all()
    period_record_count  = len(period_rows)
    period_ids           = [r.id for r in period_rows]

    period_profit    = sum(float(r.profit or 0) for r in period_rows)
    member_count     = sum(1 for r in period_rows if r.member_type == "member")
    nonmember_count  = sum(1 for r in period_rows if r.member_type == "nonmember")
    member_profit    = sum(float(r.profit or 0) for r in period_rows if r.member_type == "member")
    nonmember_profit = sum(float(r.profit or 0) for r in period_rows if r.member_type == "nonmember")

    # 會員/非會員占比
    total_count    = period_record_count or 1  # 防零除
    member_ratio   = round(member_count / total_count * 100, 1)
    nonmember_ratio = round(nonmember_count / total_count * 100, 1)

    # ── 明星耗材：ServiceConsumable 加總各消耗品用量（含贈品）────
    # 僅統計 category='consumable'（消耗品，不含儀器設備）
    top_consumables = []
    if period_ids:
        rows = (
            db.session.query(
                Consumable.name,
                func.sum(ServiceConsumable.qty).label("total_qty")
            )
            .join(Consumable, ServiceConsumable.consumable_id == Consumable.id)
            .filter(
                ServiceConsumable.service_record_id.in_(period_ids),
                Consumable.category == "consumable"
            )
            .group_by(Consumable.name)
            .order_by(func.sum(ServiceConsumable.qty).desc())
            .all()
        )
        top_consumables = [{"name": r.name, "total_qty": int(r.total_qty)} for r in rows]

    # ── 回本週期推估 ──────────────────────────────────────────────
    avg_profit_per_day = None
    days_to_payback    = None
    span_days          = 0

    if period_rows:
        dates = [r.date for r in period_rows if r.date]
        if dates:
            min_date = min(dates)
            max_date = max(dates)
            try:
                d_min     = datetime.date.fromisoformat(str(min_date))
                d_max     = datetime.date.fromisoformat(str(max_date))
                span_days = max(1, (d_max - d_min).days + 1)
            except (ValueError, TypeError):
                span_days = period_record_count  # fallback：每筆算一天

            avg_profit_per_day = period_profit / span_days
            if avg_profit_per_day > 0 and payback_remaining > 0:
                days_to_payback = int(payback_remaining / avg_profit_per_day)

    return {
        # 期間
        "period_profit":        period_profit,
        "period_record_count":  period_record_count,
        # 全期回本
        "total_profit":         total_profit,
        "payback_target":       payback_target,
        "payback_progress":     round(payback_progress, 1),
        "payback_remaining":    payback_remaining,
        # 全店總覽（愛啪啪單一主體）
        "total_revenue":        total_revenue,
        "total_customer_count": total_customer_count,
        # 會員 vs 非會員
        "member_count":         member_count,
        "nonmember_count":      nonmember_count,
        "member_profit":        member_profit,
        "nonmember_profit":     nonmember_profit,
        "member_ratio":         member_ratio,
        "nonmember_ratio":      nonmember_ratio,
        # 明星耗材
        "top_consumables":      top_consumables,
        # 回本週期
        "span_days":            span_days,
        "avg_profit_per_day":   avg_profit_per_day,
        "days_to_payback":      days_to_payback,
        # 篩選條件（傳回模板）
        "start":                start or "",
        "end":                  end   or "",
    }


# ──────────────────────────────────────────────
# 路由
# ──────────────────────────────────────────────

@dashboard_bp.route("/")
@owner_required
def index():
    """儀表板首頁，僅 owner 可存取。"""
    start = request.args.get("start", "").strip() or None
    end   = request.args.get("end",   "").strip() or None

    stats = compute_dashboard(start, end)

    return render_template("index.html", stats=stats)

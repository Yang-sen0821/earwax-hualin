# blueprints/service.py
# 施作登錄 blueprint（service_bp）
# 路由：/service/new（4 步表單 GET/POST）、/service（列表）
#
# 業務規則（來自 SPEC 第 1~2 節）：
#   1. profit 後端計算（不信前端）— 使用 compute_profit()
#      profit = actual_price − variable_cost_per × voucher_count
#               + owner_shareholder_return × voucher_count
#   2. 非會員 → 扣 voucher_inventory.qty_on_hand -= voucher_count（不足擋下並 flash）
#   3. 會員 → 不扣券庫存
#   4. 逐筆 ServiceConsumable → 扣 consumables.qty_on_hand -= qty
#      （僅 category='consumable'；不足擋下並 rollback）
#   5. 耗材一律走 ServiceConsumable 明細表（一施作可多項消耗品），不再有單一耗材欄位

import datetime
from flask import (
    Blueprint, render_template, request,
    redirect, url_for, flash, session
)
from db import (
    db, compute_profit,
    Product, Params, Customer,
    VoucherInventory, Consumable, ServiceRecord, ServiceConsumable
)
from auth import login_required

service_bp = Blueprint("service", __name__, url_prefix="/service")

PAGE_SIZE = 20


# ──────────────────────────────────────────────
# 列表
# ──────────────────────────────────────────────

@service_bp.route("/")
@login_required
def list_service():
    """施作記錄列表，按日期倒序，支援分頁。"""
    page = int(request.args.get("page", 1))

    # ── 篩選條件（與模板 service/list.html 的欄位對齊）──
    start       = request.args.get("start", "").strip()
    end         = request.args.get("end", "").strip()
    member_type = request.args.get("member_type", "").strip()

    query = ServiceRecord.query
    if start:
        query = query.filter(ServiceRecord.date >= start)
    if end:
        query = query.filter(ServiceRecord.date <= end)
    if member_type in ("member", "nonmember"):
        query = query.filter(ServiceRecord.member_type == member_type)

    total   = query.count()
    pages   = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page    = max(1, min(page, pages))

    records = (
        query
        .order_by(ServiceRecord.date.desc(), ServiceRecord.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )

    # 預載客戶名稱（避免 N+1）
    customer_ids = {r.customer_id for r in records}
    customers    = {
        c.id: c
        for c in Customer.query.filter(Customer.id.in_(customer_ids)).all()
    }

    return render_template(
        "service/list.html",
        records=records,
        customers=customers,
        page=page,
        pages=pages,
        total=total,
    )


# ──────────────────────────────────────────────
# 新增施作（4 步表單）
# ──────────────────────────────────────────────

@service_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_service():
    """4 步施作登錄表單。
    步驟（step 參數控制顯示）：
      1 — 選代理產品（愛啪啪）
      2 — 選/建消費者（有則套用，無則當場建立）
      3 — 會員/非會員
      4 — 張數 + 實收金額（可折扣）+ 耗材明細（可多列消耗品，各填數量、各自可標贈品）→ 送出
    """
    step = int(request.args.get("step", 1))

    # ── 取基礎資料（步驟 1 用）──────────────
    products = Product.query.filter_by(active=True).all()

    # ── GET：顯示表單 ──────────────────────
    if request.method == "GET":
        # 步驟 2：取消費者清單供選擇
        customers = []
        if step == 2:
            customers = Customer.query.order_by(Customer.name.asc()).all()

        # 步驟 4：取選定產品的 params，算預設售價；取消耗品清單
        params        = None
        product       = None
        default_price = 0
        consumables   = []
        if step == 4:
            product_id    = request.args.get("product_id", type=int)
            member_type   = request.args.get("member_type", "nonmember")
            voucher_count = int(request.args.get("voucher_count", 1))
            if product_id:
                product = db.session.get(Product, product_id)
                params  = Params.query.filter_by(product_id=product_id).first()
                if params:
                    base = (
                        params.member_base_price
                        if member_type == "member"
                        else params.nonmember_base_price
                    )
                    default_price = base * voucher_count
            # 僅取 category='consumable'（儀器設備不出現在此）
            consumables = Consumable.query.filter_by(category="consumable").order_by(Consumable.name.asc()).all()

        return render_template(
            "service/new.html",
            step=step,
            products=products,
            customers=customers,
            params=params,
            product=product,
            default_price=default_price,
            consumables=consumables,
            today=datetime.date.today().isoformat(),  # 施作日期預設今天
            form=request.args,   # 把 GET 參數傳給模板保留已選值
        )

    # ── POST：最後一步（step=4）送出整筆記錄 ──
    f             = request.form
    product_id    = int(f.get("product_id",    0))
    customer_id   = f.get("customer_id",   "").strip()
    member_type   = f.get("member_type",   "nonmember")
    voucher_count = int(f.get("voucher_count", 1))
    actual_price  = float(f.get("actual_price", 0))
    note          = f.get("note", "").strip()
    date_str      = f.get("date",  datetime.date.today().isoformat())

    # ── 解析耗材明細（可多列）────────────────
    # 表單欄位命名：consumable_id_N, consumable_qty_N, consumable_gift_N
    consumable_lines = []
    idx = 0
    while True:
        cid_key = f"consumable_id_{idx}"
        if cid_key not in f:
            break
        cid = f.get(cid_key, "").strip()
        qty_str = f.get(f"consumable_qty_{idx}", "0").strip()
        is_gift = f.get(f"consumable_gift_{idx}") == "1"
        if cid and qty_str:
            try:
                cid = int(cid)
                qty = int(qty_str)
                if qty > 0:
                    consumable_lines.append({"consumable_id": cid, "qty": qty, "is_gift": is_gift})
            except ValueError:
                pass
        idx += 1

    # ── 處理「有則套用，無則建立」消費者 ────
    if customer_id:
        # 前端選了現有消費者
        customer_id = int(customer_id)
        customer    = db.session.get(Customer, customer_id)
        if not customer:
            flash("找不到指定的消費者，請重新操作。")
            return redirect(url_for("service.new_service"))
    else:
        # 前端填了新消費者姓名 → 建立（或找同名電話先比對）
        new_name  = f.get("new_name",  "").strip()
        new_phone = f.get("new_phone", "").strip()
        new_is_member = f.get("new_is_member") == "1"

        if not new_name:
            flash("請選擇消費者，或填寫新消費者姓名。")
            return redirect(url_for("service.new_service"))

        # 先以姓名+電話比對，找到就複用
        existing = Customer.query.filter_by(name=new_name, phone=new_phone).first()
        if existing:
            customer = existing
        else:
            customer = Customer(
                name=new_name,
                phone=new_phone,
                is_member=new_is_member,
                created_at=datetime.datetime.utcnow(),
            )
            db.session.add(customer)
            db.session.flush()   # 取得 customer.id（尚未 commit）
        customer_id = customer.id

    # ── 取 params（後端計算淨利用）──────────
    params = Params.query.filter_by(product_id=product_id).first()
    if not params:
        flash("找不到產品參數設定，請聯絡管理員。")
        return redirect(url_for("service.new_service"))

    # ── 後端計算淨利（不信前端傳的值）───────
    profit = compute_profit(member_type, voucher_count, actual_price, params)

    # ── 非會員：檢查並扣券庫存 ──────────────
    if member_type == "nonmember":
        voucher_inv = VoucherInventory.query.filter_by(product_id=product_id).first()
        if not voucher_inv:
            flash("找不到券庫存記錄，請聯絡管理員。")
            db.session.rollback()
            return redirect(url_for("service.new_service"))
        if voucher_inv.qty_on_hand < voucher_count:
            flash(
                f"券庫存不足！目前剩餘 {voucher_inv.qty_on_hand} 張，"
                f"本次需要 {voucher_count} 張。請先補券後再登錄。"
            )
            db.session.rollback()
            return redirect(url_for("service.new_service"))
        voucher_inv.qty_on_hand -= voucher_count

    # ── 建立施作記錄（先 flush 取得 id）─────
    record = ServiceRecord(
        date          = date_str,
        customer_id   = customer_id,
        product_id    = product_id,
        member_type   = member_type,
        voucher_count = voucher_count,
        actual_price  = actual_price,
        profit        = profit,
        note          = note,
        created_at    = datetime.datetime.utcnow(),
    )
    db.session.add(record)
    db.session.flush()  # 取得 record.id，尚未 commit

    # ── 逐筆建立 ServiceConsumable 並扣庫存 ─
    for line in consumable_lines:
        consumable = db.session.get(Consumable, line["consumable_id"])
        if not consumable or consumable.category != "consumable":
            flash(f"找不到耗材（id={line['consumable_id']}），請聯絡管理員。")
            db.session.rollback()
            return redirect(url_for("service.new_service"))
        if consumable.qty_on_hand < line["qty"]:
            flash(
                f"耗材「{consumable.name}」庫存不足！"
                f"目前剩餘 {consumable.qty_on_hand}，本次需扣 {line['qty']}。"
                f"請先補貨後再登錄。"
            )
            db.session.rollback()
            return redirect(url_for("service.new_service"))
        consumable.qty_on_hand -= line["qty"]
        sc = ServiceConsumable(
            service_record_id = record.id,
            consumable_id     = consumable.id,
            qty               = line["qty"],
            is_gift           = line["is_gift"],
            created_at        = datetime.datetime.utcnow(),
        )
        db.session.add(sc)

    # ── 全部成功 → commit ────────────────────
    db.session.commit()

    flash(
        f"施作記錄已建立！"
        f"{'會員' if member_type == 'member' else '非會員'} "
        f"{voucher_count} 張，實收 {actual_price:,.0f} 元，"
        f"淨利 {profit:,.0f} 元。"
    )
    return redirect(url_for("service.list_service"))

"""mobile blueprint（/m）— 手機後台高頻操作（D15 / §1.5）。

路由：
- /m                     手機首頁（高頻入口）
- /m/orders              訂單列表 + 付款/出貨狀態
- /m/orders/<id>         單筆訂單明細 + 改狀態
- /m/orders/new          快速建單（多品項，§4.2 整張 transaction）
- /m/inventory           三池庫存摘要（boxed / loose_piece / paper_bag）
- /m/shipments           待出貨列表 + 出貨（呼叫 deduct_for_shipment，§4.4）
- /m/customers           客戶查詢

模組層鐵律：
- 任何庫存量變動一律呼叫 inventory_service §4 契約函式（deduct_for_sale / deduct_for_shipment）。
- 禁止本檔自寫 inventory_balances / inventory_movements。
- 多品項建單：建單 + 建明細 + 逐項扣庫存全在同一 DB transaction；任一缺貨整張 rollback（§4.2）。
- 版型手機優先；本檔只讀 db.py 實際欄位、用 db.py 閉集常數，勿自寫字串。
"""
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)

from auth import login_required, role_required, current_user
from db import (
    get_session,
    Order, OrderItem, Customer, Product, SalesPlan,
    InventoryBalance, Shipment,
    INVENTORY_POOLS, COMBO_CODES,
)
import inventory_service
from display_labels import combo_label

mobile_bp = Blueprint("mobile", __name__, url_prefix="/m")

# 寫入類角色邊界（§六 權限矩陣，對齊桌面後台 orders/shipping）：
# - 建單 / 改訂單狀態（orders 寫）= owner / staff
# - 出貨（shipment 寫）= owner / staff / warehouse
# owner 由 role_required 永遠通過；viewer / accounting 唯讀（被擋 403）。
ORDER_WRITE_ROLES = ("staff",)
SHIPMENT_WRITE_ROLES = ("staff", "warehouse")

# 建單品項單位閉集（新模型，對齊桌面 orders）：LOOSE=片(扣裸片池)/BOX=盒(扣盒裝池)。
UNIT_CODES = ("LOOSE", "BOX")


# -------------------------------------------------------------------------
# 工具
# -------------------------------------------------------------------------
def _gen_order_no(db):
    """產生訂單編號：FC + YYYYMMDD + 當日流水號（4 碼）。"""
    today = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"FC{today}"
    last = (
        db.query(Order)
        .filter(Order.order_no.like(f"{prefix}%"))
        .order_by(Order.order_no.desc())
        .first()
    )
    seq = 1
    if last and last.order_no and last.order_no.startswith(prefix):
        try:
            seq = int(last.order_no[len(prefix):]) + 1
        except (ValueError, TypeError):
            seq = 1
    return f"{prefix}{seq:04d}"


def _parse_money(raw):
    """解析金額字串為 Decimal；空/非數值/負數一律回 0（建單金額可填可不填）。"""
    from decimal import Decimal, InvalidOperation
    raw = (raw or "").strip()
    if not raw:
        return Decimal("0")
    try:
        v = Decimal(raw)
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return v if v >= 0 else Decimal("0")


def _result_ok(result):
    """service 回傳統一判讀：契約定義 DeductResult.ok；無 ok 屬性時視為成功。"""
    return bool(getattr(result, "ok", True))


def _result_msg(result):
    # service 回傳 Result.error 承載缺貨/非法參數訊息（§4 契約）
    for attr in ("error", "message", "msg", "reason"):
        v = getattr(result, attr, None)
        if v:
            return str(v)
    return ""


# -------------------------------------------------------------------------
# 首頁
# -------------------------------------------------------------------------
@mobile_bp.route("/")
@login_required
def index():
    return render_template("mobile/index.html", section="mobile", user=current_user())


# -------------------------------------------------------------------------
# 訂單列表
# -------------------------------------------------------------------------
@mobile_bp.route("/orders")
@login_required
def orders():
    db = get_session()
    pay = (request.args.get("pay") or "").strip()
    ship = (request.args.get("ship") or "").strip()
    q = db.query(Order)
    if pay:
        q = q.filter(Order.payment_status == pay)
    if ship:
        q = q.filter(Order.shipping_status == ship)
    rows = q.order_by(Order.created_at.desc()).limit(100).all()
    return render_template(
        "mobile/orders.html", section="mobile", user=current_user(),
        orders=rows, pay=pay, ship=ship,
    )


# -------------------------------------------------------------------------
# 訂單明細 + 改狀態
# -------------------------------------------------------------------------
@mobile_bp.route("/orders/<int:order_id>", methods=["GET", "POST"])
@login_required
def order_detail(order_id):
    db = get_session()
    order = db.query(Order).filter_by(id=order_id).first()
    if not order:
        abort(404)

    if request.method == "POST":
        # 改訂單狀態 = orders 寫入（§六）：限 owner/staff；viewer/accounting/warehouse 唯讀。
        u_chk = current_user()
        if u_chk is None or (u_chk.role != "owner" and u_chk.role not in ORDER_WRITE_ROLES):
            abort(403)
        # 僅改狀態欄位；不動庫存（庫存變動走出貨/建單流程）
        new_pay = (request.form.get("payment_status") or "").strip()
        new_ship = (request.form.get("shipping_status") or "").strip()
        changed = False
        if new_pay and new_pay != order.payment_status:
            order.payment_status = new_pay
            changed = True
        if new_ship and new_ship != order.shipping_status:
            order.shipping_status = new_ship
            changed = True
        if changed:
            u = current_user()
            order.updated_by = u.id if u else None
            try:
                db.commit()
                flash("訂單狀態已更新")
            except Exception as e:  # noqa: BLE001
                db.rollback()
                flash(f"更新失敗：{e}")
        return redirect(url_for("mobile.order_detail", order_id=order_id))

    items = db.query(OrderItem).filter_by(order_id=order_id).all()
    # product_id -> name 對照
    pids = {it.product_id for it in items}
    prod_map = {}
    if pids:
        for p in db.query(Product).filter(Product.id.in_(pids)).all():
            prod_map[p.id] = p.name
    customer = None
    if order.customer_id:
        customer = db.query(Customer).filter_by(id=order.customer_id).first()
    return render_template(
        "mobile/order_detail.html", section="mobile", user=current_user(),
        order=order, items=items, prod_map=prod_map, customer=customer,
        combo_codes=COMBO_CODES,
    )


# -------------------------------------------------------------------------
# 快速建單（多品項，§4.2 整張 transaction）
# -------------------------------------------------------------------------
@mobile_bp.route("/orders/new", methods=["GET", "POST"])
@role_required(*ORDER_WRITE_ROLES)
def order_new():
    db = get_session()

    if request.method == "POST":
        u = current_user()
        operator = (u.display_name or u.username) if u else "system"

        recipient_name = (request.form.get("recipient_name") or "").strip()
        recipient_phone = (request.form.get("recipient_phone") or "").strip()
        shipping_address = (request.form.get("shipping_address") or "").strip()
        note = (request.form.get("note") or "").strip()
        customer_id_raw = (request.form.get("customer_id") or "").strip()
        customer_id = int(customer_id_raw) if customer_id_raw.isdigit() else None
        # 建單當場新增客戶（森哥 2026-08-19）：與桌面同規則，客戶在同一 tx 內建立，
        # 訂單整張 rollback 時客戶也不留下。已選既有客戶時以 customer_id 為準。
        new_customer_name = (request.form.get("new_customer_name") or "").strip()
        if customer_id:
            new_customer_name = ""
        if len(new_customer_name) > 64:
            flash("新客戶姓名過長（上限 64 字），訂單未建立")
            return _render_order_new(db)

        # 解析多品項：product_id[]、combo_code[]（單位 LOOSE/BOX）、qty[]、amount[]（實收金額）
        product_ids = request.form.getlist("product_id")
        combo_codes = request.form.getlist("combo_code")
        qtys = request.form.getlist("qty")
        amounts = request.form.getlist("amount")

        line_items = []
        for i, (pid_s, combo, qty_s) in enumerate(zip(product_ids, combo_codes, qtys)):
            pid_s, combo, qty_s = pid_s.strip(), combo.strip(), qty_s.strip()
            amt_s = (amounts[i].strip() if i < len(amounts) else "")
            if not pid_s and not qty_s:
                continue  # 跳過空白列
            if not (pid_s.isdigit() and qty_s.isdigit() and int(qty_s) > 0):
                flash("品項資料不完整或數量無效")
                return _render_order_new(db)
            if combo not in UNIT_CODES:
                flash(f"無效的品項單位：{combo}（限 片 / 盒）")
                return _render_order_new(db)
            amount = _parse_money(amt_s)
            line_items.append((int(pid_s), combo, int(qty_s), amount))

        if not line_items:
            flash("請至少新增一個品項")
            return _render_order_new(db)

        # ---- §4.2：建單 + 建明細 + 逐項扣庫存，全在同一 transaction ----
        created_customer = None
        try:
            if new_customer_name:
                existing = (db.query(Customer)
                              .filter(Customer.name == new_customer_name).first())
                if existing is not None:
                    customer_id = existing.id
                else:
                    cust = Customer(
                        name=new_customer_name,
                        phone=recipient_phone or None,
                        created_by=u.id if u else None,
                        updated_by=u.id if u else None,
                    )
                    db.add(cust)
                    db.flush()
                    customer_id = cust.id
                    created_customer = new_customer_name

            order = Order(
                order_no=_gen_order_no(db),
                customer_id=customer_id,
                recipient_name=recipient_name or None,
                recipient_phone=recipient_phone or None,
                shipping_address=shipping_address or None,
                total_amount=0,
                payment_status="unpaid",
                shipping_status="pending",
                note=note or None,
                created_by=u.id if u else None,
                updated_by=u.id if u else None,
            )
            db.add(order)
            db.flush()  # 取得 order.id，供 movement ref_id / order_items 使用

            total = 0
            for pid, combo, qty, amount in line_items:
                # 庫存扣減：唯一寫入口 = inventory_service（R1 / §4.1）
                result = inventory_service.deduct_for_sale(
                    db, product_id=pid, combo_code=combo, order_qty=qty,
                    operator=operator, order_ref=str(order.id), note=note,
                )
                if not _result_ok(result):
                    # 任一缺貨 → 整張 rollback（§4.2）
                    db.rollback()
                    prod = db.query(Product).filter_by(id=pid).first()
                    pname = prod.name if prod else f"#{pid}"
                    extra = _result_msg(result)
                    flash(f"庫存不足，整張訂單未建立：{pname} / {combo_label(combo)}"
                          + (f"（{extra}）" if extra else ""))
                    return _render_order_new(db)

                # 金額為該列實收金額（可折扣）；unit_price 與 subtotal 同存此金額（無 schema 變更）
                total += amount
                db.add(OrderItem(
                    order_id=order.id,
                    product_id=pid,
                    combo_code=combo,
                    qty=qty,
                    unit_price=amount,
                    subtotal=amount,
                ))

            order.total_amount = total
            db.commit()
            if created_customer:
                flash(f"訂單已建立：{order.order_no}；同時新增客戶「{created_customer}」")
            else:
                flash(f"訂單已建立：{order.order_no}")
            return redirect(url_for("mobile.order_detail", order_id=order.id))

        except Exception as e:  # noqa: BLE001 — 任何錯誤整張 rollback（§4.2 / R1）
            db.rollback()
            flash(f"建單失敗，已全數回復：{e}")
            return _render_order_new(db)

    return _render_order_new(db)


def _render_order_new(db):
    customers = db.query(Customer).order_by(Customer.name).all()
    # 可下單品項：非包材、上架
    products = (
        db.query(Product)
        .filter(Product.is_packaging == False, Product.active == True)  # noqa: E712
        .order_by(Product.name)
        .all()
    )
    return render_template(
        "mobile/order_new.html", section="mobile", user=current_user(),
        customers=customers, products=products,
        unit_codes=UNIT_CODES,
    )


# -------------------------------------------------------------------------
# 三池庫存摘要
# -------------------------------------------------------------------------
@mobile_bp.route("/inventory")
@login_required
def inventory():
    db = get_session()
    rows = (
        db.query(InventoryBalance)
        .order_by(InventoryBalance.product_id,
                  InventoryBalance.inventory_pool,
                  InventoryBalance.stock_category)
        .all()
    )
    pids = {r.product_id for r in rows}
    prod_map = {}
    if pids:
        for p in db.query(Product).filter(Product.id.in_(pids)).all():
            prod_map[p.id] = p.name

    # 依三池分組摘要：pool -> { 'total': x, 'rows': [...] }
    pools = {pool: {"total": 0, "rows": []} for pool in INVENTORY_POOLS}
    for r in rows:
        bucket = pools.setdefault(r.inventory_pool, {"total": 0, "rows": []})
        bucket["total"] += (r.qty or 0)
        bucket["rows"].append(r)

    # 愛啪啪庫存（森哥 2026-07-12：手機版也要看得到；唯讀，編輯走桌面版庫存頁）
    # R2 邊界同桌面版：不 import earwax model、不建 FK，僅參數化 raw SQL
    from sqlalchemy import text

    from blueprints.inventory import EARWAX_TABLE, _earwax_enabled
    earwax_items = None
    earwax_error = False
    if _earwax_enabled():
        try:
            earwax_items = db.execute(text(
                f"SELECT id, category, name, qty_on_hand, COALESCE(note, '') AS note "
                f"FROM {EARWAX_TABLE} ORDER BY category, id"
            )).mappings().all()
        except Exception:
            db.rollback()
            earwax_error = True

    return render_template(
        "mobile/inventory.html", section="mobile", user=current_user(),
        pools=pools, prod_map=prod_map, pool_order=INVENTORY_POOLS,
        earwax_items=earwax_items, earwax_error=earwax_error,
    )


# -------------------------------------------------------------------------
# 待出貨 + 出貨（§4.4 紙袋扣減）
# -------------------------------------------------------------------------
@mobile_bp.route("/shipments")
@login_required
def shipments():
    db = get_session()
    # 待出貨：尚未 shipped/delivered/cancelled
    rows = (
        db.query(Order)
        .filter(Order.shipping_status.in_(["pending"]))
        .order_by(Order.created_at.asc())
        .limit(100)
        .all()
    )
    return render_template(
        "mobile/shipments.html", section="mobile", user=current_user(),
        orders=rows,
    )


@mobile_bp.route("/shipments/<int:order_id>/ship", methods=["POST"])
@role_required(*SHIPMENT_WRITE_ROLES)
def ship_order(order_id):
    db = get_session()
    order = db.query(Order).filter_by(id=order_id).first()
    if not order:
        abort(404)
    if order.shipping_status != "pending":
        flash("此訂單非待出貨狀態，已略過")
        return redirect(url_for("mobile.shipments"))

    u = current_user()
    operator = (u.display_name or u.username) if u else "system"
    tracking_no = (request.form.get("tracking_no") or "").strip()
    carrier = (request.form.get("carrier") or "").strip()

    # ---- §4.4：出貨成立同一 transaction 內扣紙袋（唯一寫入口 = service）----
    try:
        result = inventory_service.deduct_for_shipment(
            db, order_id=order.id, operator=operator,
        )
        if not _result_ok(result):
            db.rollback()
            extra = _result_msg(result)
            flash("紙袋不足，出貨未成立" + (f"（{extra}）" if extra else ""))
            return redirect(url_for("mobile.shipments"))

        # 建立 shipment 記錄 + 更新訂單出貨狀態（同 tx）
        shipment = Shipment(
            order_id=order.id,
            tracking_no=tracking_no or None,
            carrier=carrier or None,
            shipped_at=datetime.utcnow(),
            status="shipped",
            note=None,
            created_by=u.id if u else None,
        )
        db.add(shipment)
        order.shipping_status = "shipped"
        order.updated_by = u.id if u else None
        db.commit()
        flash(f"已出貨：{order.order_no}")
    except Exception as e:  # noqa: BLE001 — 紙袋不足/任何錯誤整張 rollback（§4.4 / R1）
        db.rollback()
        flash(f"出貨失敗，已全數回復：{e}")

    return redirect(url_for("mobile.shipments"))


# -------------------------------------------------------------------------
# 客戶查詢
# -------------------------------------------------------------------------
@mobile_bp.route("/customers")
@login_required
def customers():
    db = get_session()
    kw = (request.args.get("q") or "").strip()
    q = db.query(Customer)
    if kw:
        like = f"%{kw}%"
        q = q.filter((Customer.name.like(like)) | (Customer.phone.like(like)))
    rows = q.order_by(Customer.name).limit(100).all()
    return render_template(
        "mobile/customers.html", section="mobile", user=current_user(),
        customers=rows, kw=kw,
    )


# -------------------------------------------------------------------------
# 客戶建立（森哥 2026-07-12：手機版也要能建立客戶；規則對齊桌面 customers.new）
# -------------------------------------------------------------------------
@mobile_bp.route("/customers/new", methods=["GET", "POST"])
@role_required("staff")  # 對齊桌面 WRITE_ROLES；owner 自動通過
def customer_new():
    if request.method == "POST":
        db = get_session()
        u = current_user()
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("姓名為必填")
            return render_template("mobile/customer_new.html", section="mobile",
                                   user=current_user(), form=request.form)
        c = Customer(
            name=name,
            phone=(request.form.get("phone") or "").strip() or None,
            email=(request.form.get("email") or "").strip() or None,
            note=(request.form.get("note") or "").strip() or None,
            created_by=u.id if u else None,
            updated_by=u.id if u else None,
        )
        db.add(c)
        db.commit()
        flash(f"客戶已建立：{c.name}")
        return redirect(url_for("mobile.customers"))
    return render_template("mobile/customer_new.html", section="mobile",
                           user=current_user(), form={})


# -------------------------------------------------------------------------
# 愛啪啪銷售/使用紀錄（甲案 2026-07-12；邏輯共用 blueprints.earwax_sales.create_sale）
# -------------------------------------------------------------------------
@mobile_bp.route("/earwax-sales")
@role_required("staff", "warehouse", "accounting")
def earwax_sales_list():
    from blueprints.earwax_sales import _earwax_enabled
    from db import EarwaxSale
    if not _earwax_enabled():
        abort(404)
    db = get_session()
    rows = (db.query(EarwaxSale)
              .order_by(EarwaxSale.created_at.desc(), EarwaxSale.id.desc())
              .limit(100).all())
    total_amount = sum(r.amount or 0 for r in rows)
    return render_template(
        "mobile/earwax_sales.html", section="mobile", user=current_user(),
        rows=rows, total_amount=total_amount,
    )


@mobile_bp.route("/earwax-sales/new", methods=["GET", "POST"])
@role_required("staff", "warehouse")
def earwax_sale_new():
    from blueprints.earwax_sales import (
        _earwax_enabled, _sellable_items, create_sale,
    )
    if not _earwax_enabled():
        abort(404)
    db = get_session()
    if request.method == "POST":
        u = current_user()
        operator = (u.display_name or u.username) if u else "system"
        try:
            item_id = int(request.form["item_id"])
            qty = int(request.form["qty"])
            amount = float(request.form.get("amount", "0") or 0)
        except (KeyError, ValueError):
            flash("品項/數量/金額格式錯誤")
            return redirect(url_for("mobile.earwax_sale_new"))
        note = (request.form.get("note") or "").strip()
        try:
            ok, msg = create_sale(db, item_id, qty, amount, note,
                                  operator, u.id if u else None)
            if not ok:
                db.rollback()
                flash(f"未記錄：{msg}")
                return redirect(url_for("mobile.earwax_sale_new"))
            db.commit()
        except Exception:
            db.rollback()
            flash("記錄失敗（資料庫錯誤），已全數回復")
            return redirect(url_for("mobile.earwax_sale_new"))
        flash(msg)
        return redirect(url_for("mobile.earwax_sales_list"))

    return render_template(
        "mobile/earwax_sale_new.html", section="mobile", user=current_user(),
        items=_sellable_items(db),
    )

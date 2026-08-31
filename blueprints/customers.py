"""customers blueprint（/customers）— 客戶管理 CRUD + 多地址（D5）。

權限（§六權限矩陣）：客戶資料屬訂單流前置，沿用 orders 列權限精神：
- 讀：所有登入角色（owner/staff/warehouse/accounting/viewer）
- 寫（建立/編輯/刪除/地址）：owner / staff（warehouse/accounting/viewer 唯讀）
  owner 由 role_required 永遠通過。

本模組不碰庫存量，故不呼叫 inventory_service。
嚴守鐵律：只用本檔 + templates/customers/*；不改 db.py / app.py / base.html / 其他 blueprint。
"""
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    session as flask_session,
)

from auth import login_required, role_required, current_user
from db import (
    get_session, Customer, CustomerAddress, Order, OrderItem, Product,
    active_orders,
)

customers_bp = Blueprint("customers", __name__, url_prefix="/customers")

# 可寫角色（owner 由 role_required 自動通過）
WRITE_ROLES = ("staff",)

# CR-2/CR-3（森哥 2026-08-31 口徑）：客戶頁金額欄（單筆金額、運費、累計金額、排行金額）
# 只給 owner / accounting 看；staff 等其他角色只看訂單數與最近購買。
# 判斷來源 = session role（登入時寫入，與 base.html 導覽同一手法）。
AMOUNT_ROLES = ("owner", "accounting")


def can_see_amount():
    """目前登入者是否可看客戶頁金額欄（session role 判斷）。"""
    return flask_session.get("role") in AMOUNT_ROLES


def customer_orders_with_items(db, customer_id, include_voided=False):
    """CR-2：某客戶的訂單列表 + 每單品項（桌面 / 手機共用）。

    回 (orders, items_map)：
      orders    — 依 id desc；預設排除作廢單（active_orders），include_voided=True 則全列
      items_map — {order_id: [{"product_name", "combo_code", "qty"}]}，供模板列「商品名 片/盒×qty」
    """
    query = db.query(Order).filter(Order.customer_id == customer_id)
    if not include_voided:
        query = active_orders(query)
    orders = query.order_by(Order.id.desc()).all()
    items_map = {}
    ids = [o.id for o in orders]
    if ids:
        rows = (
            db.query(OrderItem.order_id, Product.name, OrderItem.combo_code, OrderItem.qty)
            .join(Product, Product.id == OrderItem.product_id)
            .filter(OrderItem.order_id.in_(ids))
            .order_by(OrderItem.order_id, OrderItem.id)
            .all()
        )
        for oid, pname, combo, qty in rows:
            items_map.setdefault(oid, []).append(
                {"product_name": pname, "combo_code": combo, "qty": qty}
            )
    return orders, items_map


def customer_summary(orders):
    """CR-2 客戶小計（口徑與 reports._customer_ranking 一致）：
      訂單數     = 非作廢單數（含已退款單；作廢單即使被列出也不計）
      累計金額   = 非作廢 且 非 refunded 的 total_amount 合計（不含運費）
      最近購買日 = 最近一筆非作廢單的 created_at
    """
    active = [o for o in orders if o.voided_at is None]
    total = 0.0
    for o in active:
        if o.payment_status != "refunded":
            total += float(o.total_amount or 0)
    last = max((o.created_at for o in active if o.created_at), default=None)
    return {"order_count": len(active), "total_amount": total, "last_order_at": last}


# -------------------------------------------------------------------------
# 列表 / 查詢
# -------------------------------------------------------------------------
@customers_bp.route("/")
@login_required
def index():
    db = get_session()
    q = (request.args.get("q") or "").strip()
    query = db.query(Customer)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Customer.name.like(like))
            | (Customer.phone.like(like))
            | (Customer.email.like(like))
        )
    customers = query.order_by(Customer.id.desc()).all()
    # CR-3：每位客戶 訂單數 / 累計金額 / 最近購買（口徑同 reports._customer_ranking）；
    #        ?sort=rank 依累計金額 desc、次鍵訂單數 desc；預設仍依 id desc。金額欄僅 AMOUNT_ROLES。
    from blueprints.reports import _customer_ranking
    stats = {r["customer_id"]: r for r in _customer_ranking(db) if r["customer_id"] is not None}
    sort = request.args.get("sort") or ""
    if sort == "rank":
        customers.sort(key=lambda c: (
            -(stats[c.id]["total_amount"] if c.id in stats else 0.0),
            -(stats[c.id]["order_count"] if c.id in stats else 0),
            -c.id,
        ))
    return render_template(
        "customers/index.html", section="customers", customers=customers, q=q,
        stats=stats, sort=sort, show_amount=can_see_amount(),
    )


# -------------------------------------------------------------------------
# 明細（含多地址 + 歷史訂單）
# -------------------------------------------------------------------------
@customers_bp.route("/<int:customer_id>")
@login_required
def detail(customer_id):
    db = get_session()
    customer = db.get(Customer, customer_id)
    if not customer:
        abort(404)
    addresses = (
        db.query(CustomerAddress)
        .filter_by(customer_id=customer_id)
        .order_by(CustomerAddress.is_default.desc(), CustomerAddress.id.asc())
        .all()
    )
    # CR-4：作廢單預設不列（與訂單列表同口徑：?show_voided=1 灰字列出、不計入小計）；
    #        刪除保護仍看全部（含作廢），見 delete。
    # CR-2：加日期 / 品項 / 運費欄 + 客戶小計；金額欄僅 owner/accounting（AMOUNT_ROLES）。
    show_voided = request.args.get("show_voided") == "1"
    orders, items_map = customer_orders_with_items(
        db, customer_id, include_voided=show_voided)
    summary = customer_summary(orders)
    return render_template(
        "customers/detail.html", section="customers",
        customer=customer, addresses=addresses, orders=orders,
        items_map=items_map, summary=summary,
        show_voided=show_voided, show_amount=can_see_amount(),
    )


# -------------------------------------------------------------------------
# 新增 / 編輯
# -------------------------------------------------------------------------
@customers_bp.route("/new", methods=["GET", "POST"])
@role_required(*WRITE_ROLES)
def new():
    if request.method == "POST":
        db = get_session()
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("姓名為必填")
            return render_template("customers/form.html", section="customers",
                                   customer=None, form=request.form)
        c = Customer(
            name=name,
            phone=(request.form.get("phone") or "").strip() or None,
            email=(request.form.get("email") or "").strip() or None,
            note=(request.form.get("note") or "").strip() or None,
            created_by=_uid(),
            updated_by=_uid(),
        )
        db.add(c)
        db.commit()
        flash("客戶已建立")
        return redirect(url_for("customers.detail", customer_id=c.id))
    return render_template("customers/form.html", section="customers",
                           customer=None, form={})


@customers_bp.route("/<int:customer_id>/edit", methods=["GET", "POST"])
@role_required(*WRITE_ROLES)
def edit(customer_id):
    db = get_session()
    c = db.get(Customer, customer_id)
    if not c:
        abort(404)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("姓名為必填")
            return render_template("customers/form.html", section="customers",
                                   customer=c, form=request.form)
        c.name = name
        c.phone = (request.form.get("phone") or "").strip() or None
        c.email = (request.form.get("email") or "").strip() or None
        c.note = (request.form.get("note") or "").strip() or None
        c.updated_by = _uid()
        db.commit()
        flash("客戶已更新")
        return redirect(url_for("customers.detail", customer_id=c.id))
    return render_template("customers/form.html", section="customers",
                           customer=c, form={})


@customers_bp.route("/<int:customer_id>/delete", methods=["POST"])
@role_required(*WRITE_ROLES)
def delete(customer_id):
    db = get_session()
    c = db.get(Customer, customer_id)
    if not c:
        abort(404)
    # 有訂單關聯時不允許刪除（保護歷史資料完整性）
    has_orders = db.query(Order).filter_by(customer_id=customer_id).first()
    if has_orders:
        flash("此客戶已有訂單，無法刪除")
        return redirect(url_for("customers.detail", customer_id=customer_id))
    db.query(CustomerAddress).filter_by(customer_id=customer_id).delete()
    db.delete(c)
    db.commit()
    flash("客戶已刪除")
    return redirect(url_for("customers.index"))


# -------------------------------------------------------------------------
# 多地址：新增 / 編輯 / 刪除 / 設為預設
# -------------------------------------------------------------------------
@customers_bp.route("/<int:customer_id>/addresses/new", methods=["POST"])
@role_required(*WRITE_ROLES)
def address_new(customer_id):
    db = get_session()
    c = db.get(Customer, customer_id)
    if not c:
        abort(404)
    address = (request.form.get("address") or "").strip()
    if not address:
        flash("地址為必填")
        return redirect(url_for("customers.detail", customer_id=customer_id))
    is_default = request.form.get("is_default") == "on"
    if is_default:
        _clear_default(db, customer_id)
    # 該客戶若尚無地址，第一筆自動設為預設
    if not db.query(CustomerAddress).filter_by(customer_id=customer_id).first():
        is_default = True
    addr = CustomerAddress(
        customer_id=customer_id,
        recipient=(request.form.get("recipient") or "").strip() or None,
        phone=(request.form.get("phone") or "").strip() or None,
        address=address,
        is_default=is_default,
    )
    db.add(addr)
    db.commit()
    flash("地址已新增")
    return redirect(url_for("customers.detail", customer_id=customer_id))


@customers_bp.route("/<int:customer_id>/addresses/<int:address_id>/edit", methods=["POST"])
@role_required(*WRITE_ROLES)
def address_edit(customer_id, address_id):
    db = get_session()
    addr = db.get(CustomerAddress, address_id)
    if not addr or addr.customer_id != customer_id:
        abort(404)
    address = (request.form.get("address") or "").strip()
    if not address:
        flash("地址為必填")
        return redirect(url_for("customers.detail", customer_id=customer_id))
    addr.recipient = (request.form.get("recipient") or "").strip() or None
    addr.phone = (request.form.get("phone") or "").strip() or None
    addr.address = address
    if request.form.get("is_default") == "on":
        _clear_default(db, customer_id)
        addr.is_default = True
    db.commit()
    flash("地址已更新")
    return redirect(url_for("customers.detail", customer_id=customer_id))


@customers_bp.route("/<int:customer_id>/addresses/<int:address_id>/delete", methods=["POST"])
@role_required(*WRITE_ROLES)
def address_delete(customer_id, address_id):
    db = get_session()
    addr = db.get(CustomerAddress, address_id)
    if not addr or addr.customer_id != customer_id:
        abort(404)
    was_default = addr.is_default
    db.delete(addr)
    db.flush()
    # 若刪掉的是預設，且仍有其他地址 → 自動補一個預設
    if was_default:
        nxt = (
            db.query(CustomerAddress)
            .filter_by(customer_id=customer_id)
            .order_by(CustomerAddress.id.asc())
            .first()
        )
        if nxt:
            nxt.is_default = True
    db.commit()
    flash("地址已刪除")
    return redirect(url_for("customers.detail", customer_id=customer_id))


@customers_bp.route("/<int:customer_id>/addresses/<int:address_id>/default", methods=["POST"])
@role_required(*WRITE_ROLES)
def address_set_default(customer_id, address_id):
    db = get_session()
    addr = db.get(CustomerAddress, address_id)
    if not addr or addr.customer_id != customer_id:
        abort(404)
    _clear_default(db, customer_id)
    addr.is_default = True
    db.commit()
    flash("已設為預設地址")
    return redirect(url_for("customers.detail", customer_id=customer_id))


# -------------------------------------------------------------------------
# helpers
# -------------------------------------------------------------------------
def _uid():
    u = current_user()
    return u.id if u else None


def _clear_default(db, customer_id):
    for a in db.query(CustomerAddress).filter_by(customer_id=customer_id, is_default=True).all():
        a.is_default = False

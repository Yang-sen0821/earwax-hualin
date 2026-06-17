"""orders blueprint（/orders）— 訂單管理（D4）。

職責：
- 建立訂單（多品項，§4.2 同一原子 transaction）：建單 + 建明細 + 逐項呼叫
  inventory_service.deduct_for_sale 扣庫存 + 寫 movement；任一品項缺貨 ⇒ 整張 rollback。
- 查詢 / 明細。
- 改付款狀態 / 出貨狀態；出貨成立同一 transaction 內呼叫 deduct_for_shipment 扣紙袋（§4.4），
  紙袋不足 ⇒ rollback、出貨不成立。
- 含收件地址快照、訂單編號、金額 / 折扣。
- 歷史訂單匯入欄位頁（欄位齊，資料後補，D14）。

紅線 R1（本模組對外契約遵守）：
- 任何改庫存量一律呼叫 inventory_service 的 §4 契約函式（deduct_for_sale / deduct_for_shipment），
  session 為首參、在同一 transaction 內操作；本模組「不」自己寫 inventory_balances /
  inventory_movements，「不」直接 UPDATE 庫存表。
- 訂單層所有扣減 movement 共用 ref_type=order + ref_id=order_id（由 service 內以 order_ref 標記）。

嚴守鐵律：只用本檔 + templates/orders/*；不改 db.py / app.py / base.html /
inventory_service.py / 其他 blueprint。
"""
import csv
import io
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)

from auth import login_required, role_required, current_user
from db import (
    get_session, COMBO_CODES,
    Order, OrderItem, Customer, CustomerAddress,
    Product, SalesPlan,
    HistoricalOrderImport, HistoricalOrderImportRow,
)
import inventory_service

orders_bp = Blueprint("orders", __name__, url_prefix="/orders")

# 可寫角色（owner 由 role_required 永遠通過；§六：orders staff=RW）
WRITE_ROLES = ("staff",)

# 狀態閉集（§二 orders 預設值對照；UI 下拉用）
PAYMENT_STATUSES = ("unpaid", "paid", "refunded", "partial")
SHIPPING_STATUSES = ("pending", "shipped", "delivered", "cancelled")


# -------------------------------------------------------------------------
# 列表 / 查詢
# -------------------------------------------------------------------------
@orders_bp.route("/")
@login_required
def index():
    db = get_session()
    q = (request.args.get("q") or "").strip()
    pay = (request.args.get("payment_status") or "").strip()
    ship = (request.args.get("shipping_status") or "").strip()

    query = db.query(Order)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Order.order_no.like(like))
            | (Order.recipient_name.like(like))
            | (Order.recipient_phone.like(like))
        )
    if pay in PAYMENT_STATUSES:
        query = query.filter(Order.payment_status == pay)
    if ship in SHIPPING_STATUSES:
        query = query.filter(Order.shipping_status == ship)

    orders = query.order_by(Order.id.desc()).all()
    cust_map = {c.id: c for c in db.query(Customer).all()}
    return render_template(
        "orders/index.html", section="orders",
        orders=orders, cust_map=cust_map, q=q,
        payment_statuses=PAYMENT_STATUSES, shipping_statuses=SHIPPING_STATUSES,
        f_pay=pay, f_ship=ship,
    )


# -------------------------------------------------------------------------
# 明細
# -------------------------------------------------------------------------
@orders_bp.route("/<int:order_id>")
@login_required
def detail(order_id):
    db = get_session()
    order = db.get(Order, order_id)
    if not order:
        abort(404)
    items = db.query(OrderItem).filter_by(order_id=order_id).order_by(OrderItem.id.asc()).all()
    customer = db.get(Customer, order.customer_id) if order.customer_id else None
    prod_map = {p.id: p for p in db.query(Product).all()}
    return render_template(
        "orders/detail.html", section="orders",
        order=order, items=items, customer=customer, prod_map=prod_map,
        payment_statuses=PAYMENT_STATUSES, shipping_statuses=SHIPPING_STATUSES,
    )


# -------------------------------------------------------------------------
# 建立訂單（多品項，§4.2 整張原子 transaction）
# -------------------------------------------------------------------------
@orders_bp.route("/new", methods=["GET", "POST"])
@role_required(*WRITE_ROLES)
def new():
    db = get_session()
    if request.method == "POST":
        return _create_order(db)
    return render_template(
        "orders/form.html", section="orders",
        customers=db.query(Customer).order_by(Customer.name.asc()).all(),
        products=db.query(Product).filter_by(active=True, is_packaging=False).all(),
        sales_plans=db.query(SalesPlan).filter_by(active=True).all(),
        combo_codes=COMBO_CODES,
        form={},
    )


def _create_order(db):
    operator = _operator_name()

    # ---- 1. 解析表單 ----
    customer_id = _to_int(request.form.get("customer_id"))
    recipient_name = (request.form.get("recipient_name") or "").strip()
    recipient_phone = (request.form.get("recipient_phone") or "").strip()
    shipping_address = (request.form.get("shipping_address") or "").strip()
    note = (request.form.get("note") or "").strip() or None
    discount = _to_decimal(request.form.get("discount"), default=0)

    # 多品項：以平行陣列接收
    product_ids = request.form.getlist("item_product_id")
    combo_codes = request.form.getlist("item_combo_code")
    qtys = request.form.getlist("item_qty")
    unit_prices = request.form.getlist("item_unit_price")

    rows = []
    for i in range(len(product_ids)):
        pid = _to_int(product_ids[i])
        combo = (combo_codes[i] if i < len(combo_codes) else "").strip()
        qty = _to_int(qtys[i] if i < len(qtys) else None)
        price = _to_decimal(unit_prices[i] if i < len(unit_prices) else None, default=0)
        if not pid or not combo:
            continue
        if combo not in COMBO_CODES:
            flash(f"無效的銷售組合：{combo}")
            return _redraw_form(db)
        if not qty or qty <= 0:
            flash("品項數量必須為正整數")
            return _redraw_form(db)
        rows.append({"product_id": pid, "combo_code": combo, "qty": qty,
                     "unit_price": price, "subtotal": price * qty})

    if not rows:
        flash("訂單至少需一個品項")
        return _redraw_form(db)

    items_total = sum(r["subtotal"] for r in rows)
    total_amount = items_total - discount
    if total_amount < 0:
        total_amount = 0

    # ---- 2. 整張原子 transaction（§4.2）----
    order_no = _gen_order_no(db)
    order = Order(
        order_no=order_no,
        customer_id=customer_id,
        recipient_name=recipient_name or None,
        recipient_phone=recipient_phone or None,
        shipping_address=shipping_address or None,
        total_amount=total_amount,
        payment_status="unpaid",
        shipping_status="pending",
        note=note,
        created_by=_uid(),
        updated_by=_uid(),
    )
    db.add(order)
    db.flush()  # 取得 order.id，供 order_ref 與明細外鍵使用

    order_ref = str(order.id)
    try:
        for r in rows:
            db.add(OrderItem(
                order_id=order.id,
                product_id=r["product_id"],
                combo_code=r["combo_code"],
                qty=r["qty"],
                unit_price=r["unit_price"],
                subtotal=r["subtotal"],
            ))
        db.flush()

        # 逐項扣庫存：呼叫 §4.1 契約函式（同一 session / 同一 tx）
        for r in rows:
            result = inventory_service.deduct_for_sale(
                db,
                product_id=r["product_id"],
                combo_code=r["combo_code"],
                order_qty=r["qty"],
                operator=operator,
                order_ref=order_ref,
                note=f"order {order_no}",
            )
            if not _result_ok(result):
                # 任一品項缺貨 ⇒ 整張 rollback（§4.2）
                db.rollback()
                pname = _product_label(db, r["product_id"])
                flash(f"庫存不足，整張訂單未建立：{pname} / {r['combo_code']} × {r['qty']}")
                return _redraw_form(db)

        db.commit()
    except Exception as exc:  # noqa: BLE001 — 任何異動失敗整張 rollback（R1）
        db.rollback()
        flash(f"建立訂單失敗，已全數回復：{exc}")
        return _redraw_form(db)

    flash(f"訂單 {order_no} 已建立，庫存已扣減")
    return redirect(url_for("orders.detail", order_id=order.id))


# -------------------------------------------------------------------------
# 改付款狀態
# -------------------------------------------------------------------------
@orders_bp.route("/<int:order_id>/payment", methods=["POST"])
@role_required(*WRITE_ROLES)
def update_payment(order_id):
    db = get_session()
    order = db.get(Order, order_id)
    if not order:
        abort(404)
    new_status = (request.form.get("payment_status") or "").strip()
    if new_status not in PAYMENT_STATUSES:
        flash("無效的付款狀態")
        return redirect(url_for("orders.detail", order_id=order_id))
    order.payment_status = new_status
    order.updated_by = _uid()
    db.commit()
    flash("付款狀態已更新")
    return redirect(url_for("orders.detail", order_id=order_id))


# -------------------------------------------------------------------------
# 改出貨狀態（出貨成立同一 tx 扣紙袋，§4.4）
# -------------------------------------------------------------------------
@orders_bp.route("/<int:order_id>/shipping", methods=["POST"])
@role_required(*WRITE_ROLES)
def update_shipping(order_id):
    db = get_session()
    order = db.get(Order, order_id)
    if not order:
        abort(404)
    new_status = (request.form.get("shipping_status") or "").strip()
    if new_status not in SHIPPING_STATUSES:
        flash("無效的出貨狀態")
        return redirect(url_for("orders.detail", order_id=order_id))

    old_status = order.shipping_status
    operator = _operator_name()

    # 由「未出貨」→「shipped」才觸發紙袋扣減（§4.4），且避免重複扣
    triggers_shipment = (new_status == "shipped" and old_status != "shipped")

    try:
        order.shipping_status = new_status
        order.updated_by = _uid()
        db.flush()

        if triggers_shipment:
            result = inventory_service.deduct_for_shipment(
                db, order_id=order.id, operator=operator
            )
            if not _result_ok(result):
                # 紙袋不足 ⇒ rollback、出貨不成立（§4.4）
                db.rollback()
                flash("紙袋庫存不足，出貨未成立（已回復）")
                return redirect(url_for("orders.detail", order_id=order_id))

        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        flash(f"更新出貨狀態失敗，已回復：{exc}")
        return redirect(url_for("orders.detail", order_id=order_id))

    flash("出貨狀態已更新" + ("（已扣紙袋）" if triggers_shipment else ""))
    return redirect(url_for("orders.detail", order_id=order_id))


# -------------------------------------------------------------------------
# 歷史訂單匯入（D14 完整實作）
#
# 匯入是「歷史資料載入」（歷史已發生）：
#   - 不扣庫存、不走 inventory_service、不寫 inventory_movements / inventory_balances。
#   - 只落地到 historical_order_imports（批次）+ historical_order_import_rows（逐列）。
#   - 每列保留原始欄位（raw_json）+ error_message；統計 imported/success/failed。
# 權限限 owner / accounting（§六 finance/歷史資料；owner 永遠通過）。
# -------------------------------------------------------------------------

# D14 欄位對應（藍本）。表頭比對採「中文名稱 或 欄位代碼」皆可命中（去空白）。
IMPORT_FIELDS = [
    ("order_no", "訂單編號"),
    ("name", "姓名"),
    ("phone", "電話"),
    ("order_date", "訂購日期"),
    ("product_name", "商品名稱"),
    ("qty", "購買數量"),
    ("unit_price", "單價"),
    ("amount", "訂單金額"),
    ("payment_status", "付款狀態"),
    ("shipping_status", "出貨狀態"),
    ("note", "備註"),
]
IMPORT_WRITE_ROLES = ("accounting",)  # owner 由 role_required 永遠通過


@orders_bp.route("/import", methods=["GET", "POST"])
@role_required(*IMPORT_WRITE_ROLES)
def import_page():
    db = get_session()
    if request.method == "POST":
        return _do_import(db)
    # GET：上傳頁 + 欄位對照 + 歷史批次列表
    batches = (
        db.query(HistoricalOrderImport)
        .order_by(HistoricalOrderImport.id.desc())
        .limit(50)
        .all()
    )
    return render_template(
        "orders/import.html", section="orders",
        import_fields=IMPORT_FIELDS, batches=batches,
    )


@orders_bp.route("/import/<int:import_id>")
@role_required(*IMPORT_WRITE_ROLES)
def import_result(import_id):
    db = get_session()
    batch = db.get(HistoricalOrderImport, import_id)
    if not batch:
        abort(404)
    rows = (
        db.query(HistoricalOrderImportRow)
        .filter_by(import_id=import_id)
        .order_by(HistoricalOrderImportRow.id.asc())
        .all()
    )
    return render_template(
        "orders/import_result.html", section="orders",
        batch=batch, rows=rows, import_fields=IMPORT_FIELDS,
    )


# ---- 匯入核心 ------------------------------------------------------------

# 表頭文字 -> 內部欄位代碼（中文名 + 英文碼皆可命中）
_HEADER_ALIAS = {}
for _code, _label in IMPORT_FIELDS:
    _HEADER_ALIAS[_code] = _code
    _HEADER_ALIAS[_label] = _code


def _norm_header(h):
    return ("" if h is None else str(h)).strip()


def _parse_rows_from_file(filename, raw_bytes):
    """解析 Excel/CSV → (headers, list[dict])。dict 以原始表頭文字為 key。

    回傳的 list 每筆是 {原始表頭: 值}；後續再依 _HEADER_ALIAS 對應內部欄位。
    解析失敗（檔案層級）拋 ValueError。
    """
    name = (filename or "").lower()
    records = []
    if name.endswith(".csv") or name.endswith(".txt"):
        text_data = None
        for enc in ("utf-8-sig", "utf-8", "big5", "cp950"):
            try:
                text_data = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text_data is None:
            raise ValueError("CSV 編碼無法辨識（試過 utf-8/big5）")
        reader = csv.reader(io.StringIO(text_data))
        all_rows = [r for r in reader]
        if not all_rows:
            return [], []
        headers = [_norm_header(h) for h in all_rows[0]]
        for r in all_rows[1:]:
            rec = {}
            for i, h in enumerate(headers):
                rec[h] = r[i] if i < len(r) else None
            records.append(rec)
        return headers, records

    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            return [], []
        headers = [_norm_header(h) for h in header_row]
        for r in rows_iter:
            if r is None:
                continue
            rec = {}
            for i, h in enumerate(headers):
                rec[h] = r[i] if i < len(r) else None
            records.append(rec)
        return headers, records

    raise ValueError("僅支援 .xlsx / .csv 檔")


def _map_record(rec):
    """將原始列（以表頭為 key）映射到內部欄位代碼字典。未命中的表頭忽略。"""
    mapped = {}
    for raw_header, val in rec.items():
        code = _HEADER_ALIAS.get(_norm_header(raw_header))
        if code:
            mapped[code] = val
    return mapped


def _to_dec_or_none(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"無法解析數值：{v!r}")


def _to_int_or_none(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        raise ValueError(f"無法解析整數：{v!r}")


def _validate_and_build_row(mapped, raw_rec):
    """從 mapped 內部欄位建一筆 HistoricalOrderImportRow（不 commit）。

    回傳 (row_obj, is_success)。解析錯誤時 error_message 非空、is_success=False，
    但仍落地該列（保留 raw + 錯誤），符合 D14「逐列寫、保留 raw + error_message」。
    """
    errors = []
    qty = unit_price = amount = None
    try:
        qty = _to_int_or_none(mapped.get("qty"))
    except ValueError as e:
        errors.append(str(e))
    try:
        unit_price = _to_dec_or_none(mapped.get("unit_price"))
    except ValueError as e:
        errors.append(str(e))
    try:
        amount = _to_dec_or_none(mapped.get("amount"))
    except ValueError as e:
        errors.append(str(e))

    # 必要欄位輕量檢查：訂單編號 + 商品名稱其一缺 → 記為失敗（仍落地）
    if not (mapped.get("order_no") and str(mapped.get("order_no")).strip()):
        errors.append("缺訂單編號")
    if not (mapped.get("product_name") and str(mapped.get("product_name")).strip()):
        errors.append("缺商品名稱")

    err_msg = "；".join(errors) if errors else None

    def _s(key):
        v = mapped.get(key)
        return None if v is None else str(v).strip() or None

    row = HistoricalOrderImportRow(
        order_no=_s("order_no"),
        name=_s("name"),
        phone=_s("phone"),
        order_date=_s("order_date"),
        product_name=_s("product_name"),
        qty=qty,
        unit_price=unit_price,
        amount=amount,
        payment_status=_s("payment_status"),
        shipping_status=_s("shipping_status"),
        note=_s("note"),
        raw_json=json.dumps(
            {str(k): (None if v is None else str(v)) for k, v in raw_rec.items()},
            ensure_ascii=False,
        ),
        error_message=err_msg,
    )
    return row, (err_msg is None)


def _do_import(db):
    f = request.files.get("file")
    if not f or not f.filename:
        flash("請選擇要匯入的 Excel/CSV 檔")
        return redirect(url_for("orders.import_page"))

    raw_bytes = f.read()
    try:
        headers, records = _parse_rows_from_file(f.filename, raw_bytes)
    except ValueError as e:
        flash(f"匯入失敗：{e}")
        return redirect(url_for("orders.import_page"))
    except Exception as e:  # noqa: BLE001
        flash(f"匯入失敗（解析錯誤）：{e}")
        return redirect(url_for("orders.import_page"))

    batch_name = (request.form.get("batch_name") or "").strip() \
        or f"匯入 {datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"

    # ---- 落地：批次 + 逐列（單一 transaction；不碰庫存）----
    try:
        batch = HistoricalOrderImport(
            batch_name=batch_name,
            source_file=f.filename,
            row_count=len(records),
            imported_rows=0,
            success_rows=0,
            failed_rows=0,
            status="pending",
            imported_by=_uid(),
        )
        db.add(batch)
        db.flush()  # 取得 batch.id

        success = 0
        failed = 0
        for rec in records:
            mapped = _map_record(rec)
            row, ok = _validate_and_build_row(mapped, rec)
            row.import_id = batch.id
            db.add(row)
            if ok:
                success += 1
            else:
                failed += 1

        batch.imported_rows = len(records)
        batch.success_rows = success
        batch.failed_rows = failed
        batch.status = "completed" if failed == 0 else "completed_with_errors"
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        flash(f"匯入寫入失敗，已全數回復：{e}")
        return redirect(url_for("orders.import_page"))

    flash(f"匯入完成：{batch_name}（共 {len(records)} 列，成功 {success}，失敗 {failed}）")
    return redirect(url_for("orders.import_result", import_id=batch.id))


# -------------------------------------------------------------------------
# helpers
# -------------------------------------------------------------------------
def _uid():
    u = current_user()
    return u.id if u else None


def _operator_name():
    u = current_user()
    if not u:
        return "system"
    return u.display_name or u.username


def _redraw_form(db):
    return render_template(
        "orders/form.html", section="orders",
        customers=db.query(Customer).order_by(Customer.name.asc()).all(),
        products=db.query(Product).filter_by(active=True, is_packaging=False).all(),
        sales_plans=db.query(SalesPlan).filter_by(active=True).all(),
        combo_codes=COMBO_CODES,
        form=request.form,
    )


def _gen_order_no(db):
    """產生唯一訂單編號 FC + yyyymmdd + 當日序號（碰撞則遞增）。"""
    today = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"FC{today}"
    last = (
        db.query(Order)
        .filter(Order.order_no.like(f"{prefix}%"))
        .order_by(Order.order_no.desc())
        .first()
    )
    seq = 1
    if last and last.order_no.startswith(prefix):
        try:
            seq = int(last.order_no[len(prefix):]) + 1
        except ValueError:
            seq = 1
    candidate = f"{prefix}{seq:03d}"
    # 防併發碰撞：若已存在則往後找
    while db.query(Order).filter_by(order_no=candidate).first():
        seq += 1
        candidate = f"{prefix}{seq:03d}"
    return candidate


def _product_label(db, product_id):
    p = db.get(Product, product_id)
    return p.name if p else f"#{product_id}"


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_decimal(v, default=0):
    from decimal import Decimal, InvalidOperation
    if v is None or str(v).strip() == "":
        return Decimal(str(default))
    try:
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return Decimal(str(default))


def _result_ok(result):
    """容忍 inventory_service 回傳型別：物件(.ok) / dict(['ok']) / bool / None。

    None 視為未表態 → 為安全起見當作失敗；契約規定缺貨須回 ok=false（§4.1）。
    """
    if result is None:
        return False
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        return bool(result.get("ok"))
    ok = getattr(result, "ok", None)
    if ok is None:
        # 物件存在但無 ok 欄位 → 視為成功（service 以例外表達失敗的情況）
        return True
    return bool(ok)

"""products blueprint（/products）— 模組層實作。

涵蓋 DoD：
- D3 商品管理：新增 / 編輯 / 上下架(active) / 圖片(image_url) / 規格(product_specs)
- D7 檢視：4 銷售方案(sales_plans) + BOM(sales_plan_bom_items)
- D9 門檻：低水位門檻(inventory_thresholds) 設定（owner/warehouse 可改）
- D12 設定：settings 設定頁（財務測試值 / 紙袋每單數 / 其他全域設定；owner/accounting 可改財務參數）

紅線遵守：
- R1：本 blueprint 不直接寫 inventory_balances / inventory_movements（商品/規格/方案/門檻/設定
  皆為主檔維護，非庫存量變動）。任何庫存量變動只會呼叫 inventory_service §4 契約函式；
  本檔功能不含庫存量變動，故未依賴 inventory_service。
- R2：只 import 本 app（db / auth），絕不 import earwax。
- 權限（§六）：用 @role_required；owner 永遠通過。

鐵律：只填本檔 + templates/products/*，不碰 app.py / base.html / db.py / 其他 blueprint。
"""
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)

from auth import login_required, role_required, current_user
from db import (
    get_session,
    Product, ProductSpec, SalesPlan, SalesPlanBomItem,
    InventoryThreshold, Setting,
    INVENTORY_POOLS, COMBO_CODES,
)

products_bp = Blueprint("products", __name__, url_prefix="/products")


# 財務參數 key 前綴/清單（§七 / §2.6）：限 owner / accounting 可改；其餘設定 owner 可改。
FINANCE_SETTING_KEYS = {
    "cost_product_test",
    "cost_packaging_test",
    "cost_marketing_test",
}


def _uid():
    u = current_user()
    return u.id if u else None


# =========================================================================
# 商品列表（D3）
# =========================================================================
@products_bp.route("/")
@login_required
def index():
    db = get_session()
    products = db.query(Product).order_by(Product.id).all()
    return render_template("products/index.html", section="products", products=products)


# =========================================================================
# 新增商品（D3）— owner 寫
# =========================================================================
@products_bp.route("/new", methods=["GET", "POST"])
@role_required("owner")
def create():
    db = get_session()
    if request.method == "POST":
        sku = request.form.get("sku", "").strip()
        name = request.form.get("name", "").strip()
        if not sku or not name:
            flash("SKU 與名稱為必填", "error")
            return render_template("products/form.html", section="products",
                                   product=None, form=request.form, mode="create")
        if db.query(Product).filter_by(sku=sku).first():
            flash(f"SKU「{sku}」已存在", "error")
            return render_template("products/form.html", section="products",
                                   product=None, form=request.form, mode="create")
        p = Product(
            sku=sku,
            name=name,
            category=request.form.get("category", "").strip() or None,
            image_url=request.form.get("image_url", "").strip() or None,
            description=request.form.get("description", "").strip() or None,
            active=bool(request.form.get("active")),
            is_packaging=bool(request.form.get("is_packaging")),
            created_by=_uid(),
            updated_by=_uid(),
        )
        db.add(p)
        db.commit()
        flash(f"商品「{name}」已新增", "ok")
        return redirect(url_for("products.detail", pid=p.id))
    return render_template("products/form.html", section="products",
                           product=None, form={}, mode="create")


# =========================================================================
# 商品明細（D3 圖片/規格 + D7 方案/BOM 檢視）
# =========================================================================
@products_bp.route("/<int:pid>")
@login_required
def detail(pid):
    db = get_session()
    p = db.query(Product).get(pid)
    if not p:
        abort(404)
    specs = db.query(ProductSpec).filter_by(product_id=pid).order_by(ProductSpec.id).all()
    thresholds = db.query(InventoryThreshold).filter_by(product_id=pid)\
        .order_by(InventoryThreshold.inventory_pool).all()

    # D7：4 銷售方案 + 該商品 BOM（哪些方案扣本商品哪一池）
    plans = db.query(SalesPlan).order_by(SalesPlan.id).all()
    bom_items = db.query(SalesPlanBomItem).filter_by(product_id=pid).all()
    bom_by_plan = {b.sales_plan_id: b for b in bom_items}
    plan_rows = []
    for plan in plans:
        b = bom_by_plan.get(plan.id)
        plan_rows.append({
            "combo_code": plan.combo_code,
            "name": plan.name,
            "price": plan.price,
            "active": plan.active,
            "pool": b.inventory_pool if b else None,
            "qty_per_unit": b.qty_per_unit if b else None,
            "unit": b.unit if b else None,
        })

    return render_template("products/detail.html", section="products",
                           product=p, specs=specs, thresholds=thresholds,
                           plan_rows=plan_rows, pools=INVENTORY_POOLS)


# =========================================================================
# 編輯商品（D3）— owner 寫
# =========================================================================
@products_bp.route("/<int:pid>/edit", methods=["GET", "POST"])
@role_required("owner")
def edit(pid):
    db = get_session()
    p = db.query(Product).get(pid)
    if not p:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("名稱為必填", "error")
            return render_template("products/form.html", section="products",
                                   product=p, form=request.form, mode="edit")
        p.name = name
        p.category = request.form.get("category", "").strip() or None
        p.image_url = request.form.get("image_url", "").strip() or None
        p.description = request.form.get("description", "").strip() or None
        p.active = bool(request.form.get("active"))
        p.updated_by = _uid()
        p.updated_at = datetime.utcnow()
        db.commit()
        flash("商品已更新", "ok")
        return redirect(url_for("products.detail", pid=p.id))
    # GET：以 product 既有值回填
    form = {
        "sku": p.sku, "name": p.name, "category": p.category or "",
        "image_url": p.image_url or "", "description": p.description or "",
        "active": p.active,
    }
    return render_template("products/form.html", section="products",
                           product=p, form=form, mode="edit")


# =========================================================================
# 上下架切換（D3 active）— owner 寫
# =========================================================================
@products_bp.route("/<int:pid>/toggle-active", methods=["POST"])
@role_required("owner")
def toggle_active(pid):
    db = get_session()
    p = db.query(Product).get(pid)
    if not p:
        abort(404)
    p.active = not p.active
    p.updated_by = _uid()
    p.updated_at = datetime.utcnow()
    db.commit()
    flash(f"商品「{p.name}」已{'上架' if p.active else '下架'}", "ok")
    return redirect(request.referrer or url_for("products.detail", pid=pid))


# =========================================================================
# 規格管理（D3 product_specs）— owner 寫
# =========================================================================
@products_bp.route("/<int:pid>/specs/add", methods=["POST"])
@role_required("owner")
def spec_add(pid):
    db = get_session()
    p = db.query(Product).get(pid)
    if not p:
        abort(404)
    spec_name = request.form.get("spec_name", "").strip()
    if not spec_name:
        flash("規格名稱為必填", "error")
        return redirect(url_for("products.detail", pid=pid))
    db.add(ProductSpec(
        product_id=pid,
        spec_name=spec_name,
        spec_value=request.form.get("spec_value", "").strip() or None,
        unit=request.form.get("unit", "").strip() or None,
    ))
    db.commit()
    flash(f"規格「{spec_name}」已新增", "ok")
    return redirect(url_for("products.detail", pid=pid))


@products_bp.route("/<int:pid>/specs/<int:spec_id>/delete", methods=["POST"])
@role_required("owner")
def spec_delete(pid, spec_id):
    db = get_session()
    spec = db.query(ProductSpec).filter_by(id=spec_id, product_id=pid).first()
    if not spec:
        abort(404)
    db.delete(spec)
    db.commit()
    flash("規格已刪除", "ok")
    return redirect(url_for("products.detail", pid=pid))


# =========================================================================
# 低水位門檻（D9）— owner / warehouse 可改（§六：warehouse 可改 inventory_thresholds）
# 鍵：(product_id, inventory_pool)；唯一鍵 uq_threshold_pid_pool。
# =========================================================================
@products_bp.route("/<int:pid>/thresholds/set", methods=["POST"])
@role_required("owner", "warehouse")
def threshold_set(pid):
    db = get_session()
    p = db.query(Product).get(pid)
    if not p:
        abort(404)
    pool = request.form.get("inventory_pool", "").strip()
    if pool not in INVENTORY_POOLS:
        flash("庫存池不合法", "error")
        return redirect(url_for("products.detail", pid=pid))
    try:
        thr = int(request.form.get("threshold_qty", "").strip())
    except (ValueError, TypeError):
        flash("門檻數量需為整數", "error")
        return redirect(url_for("products.detail", pid=pid))
    if thr < 0:
        flash("門檻數量不得為負", "error")
        return redirect(url_for("products.detail", pid=pid))
    row = db.query(InventoryThreshold).filter_by(
        product_id=pid, inventory_pool=pool).first()
    if row:
        row.threshold_qty = thr
        row.updated_by = _uid()
        row.updated_at = datetime.utcnow()
    else:
        db.add(InventoryThreshold(
            product_id=pid, inventory_pool=pool, threshold_qty=thr,
            updated_by=_uid(),
        ))
    db.commit()
    flash(f"{pool} 低水位門檻已設為 {thr}", "ok")
    return redirect(url_for("products.detail", pid=pid))


# =========================================================================
# 銷售方案 + BOM 全覽（D7）— 全角色可讀
# =========================================================================
@products_bp.route("/plans")
@login_required
def plans():
    db = get_session()
    plans = db.query(SalesPlan).order_by(SalesPlan.id).all()
    bom_items = db.query(SalesPlanBomItem).all()
    products = {p.id: p for p in db.query(Product).all()}
    bom_by_plan = {}
    for b in bom_items:
        bom_by_plan.setdefault(b.sales_plan_id, []).append(b)
    rows = []
    for plan in plans:
        items = []
        for b in bom_by_plan.get(plan.id, []):
            prod = products.get(b.product_id)
            items.append({
                "product_name": prod.name if prod else f"#{b.product_id}",
                "product_sku": prod.sku if prod else "",
                "pool": b.inventory_pool,
                "qty_per_unit": b.qty_per_unit,
                "unit": b.unit,
            })
        rows.append({"plan": plan, "bom": items})
    return render_template("products/plans.html", section="products",
                           rows=rows, combo_codes=COMBO_CODES)


# =========================================================================
# 設定頁（D12）— 讀：全角色；改：owner（全部）/ accounting（限財務參數）
# settings 表：key(PK)/value/value_type/description/updated_by/updated_at
# =========================================================================
@products_bp.route("/settings", methods=["GET"])
@login_required
def settings():
    db = get_session()
    rows = db.query(Setting).order_by(Setting.key).all()
    user = current_user()
    can_edit_all = user.role == "owner"
    can_edit_finance = user.role in ("owner", "accounting")
    items = []
    for s in rows:
        is_finance = s.key in FINANCE_SETTING_KEYS
        editable = can_edit_all or (is_finance and can_edit_finance)
        items.append({"setting": s, "is_finance": is_finance, "editable": editable})
    return render_template("products/settings.html", section="products",
                           items=items, can_edit_all=can_edit_all,
                           can_edit_finance=can_edit_finance)


def _coerce(value: str, value_type: str):
    """依 value_type 驗證輸入；回 (ok, normalized_str_value, err)。settings.value 一律存字串。"""
    value = (value or "").strip()
    if value_type == "int":
        try:
            return True, str(int(value)), None
        except ValueError:
            return False, None, "需為整數"
    if value_type == "float":
        try:
            return True, str(float(value)), None
        except ValueError:
            return False, None, "需為數字"
    if value_type == "bool":
        if value.lower() in ("true", "false", "1", "0", "yes", "no"):
            norm = "true" if value.lower() in ("true", "1", "yes") else "false"
            return True, norm, None
        return False, None, "需為 true/false"
    return True, value, None  # str


@products_bp.route("/settings/update", methods=["POST"])
@login_required
def settings_update():
    """更新單一設定。權限：owner 可改全部；accounting 限財務參數；其餘 403。"""
    db = get_session()
    user = current_user()
    key = request.form.get("key", "").strip()
    s = db.query(Setting).filter_by(key=key).first()
    if not s:
        abort(404)

    is_finance = key in FINANCE_SETTING_KEYS
    if user.role == "owner":
        pass  # owner 全通過
    elif user.role == "accounting" and is_finance:
        pass  # accounting 限財務參數
    else:
        abort(403)

    ok, norm, err = _coerce(request.form.get("value", ""), s.value_type or "str")
    if not ok:
        flash(f"設定「{key}」{err}", "error")
        return redirect(url_for("products.settings"))
    s.value = norm
    s.updated_by = _uid()
    s.updated_at = datetime.utcnow()
    db.commit()
    flash(f"設定「{key}」已更新為 {norm}", "ok")
    return redirect(url_for("products.settings"))

"""Flora Court 面膜 ERP — 建表 + 冪等 seed（地基）

執行：python init_db.py
- create_all（SQLite 本機 / Postgres 正式）
- 冪等 seed：重複跑不重複建，已存在則略過/更新

紅線 R1：seed 寫庫存同時補 SEED movement 留痕（透過契約之外的 init-only 直寫，僅限初始化）。
"""
from datetime import datetime

from sqlalchemy import inspect

from config import Config
from db import (
    Base, engine, get_session, SCHEMA,
    User, Product, ProductSpec, SalesPlan, SalesPlanBomItem,
    InventoryBalance, InventoryMovement, InventoryThreshold, Setting,
)

# 密碼雜湊（與 auth.py 共用 werkzeug）
from werkzeug.security import generate_password_hash


# -------------------------------------------------------------------------
# seed 常數（藍本 §三 / §2.3 / D6 / D7）
# -------------------------------------------------------------------------
MASK_SKU = "FC-MASK-001"
PACKAGING_SKU = "PKG-SHARED-001"   # sentinel 共用包材商品「包材-共用」

SALES_PLANS = [
    # combo_code, name, price, pool, qty_per_unit, unit
    ("SINGLE", "單片體驗組", 0, "loose_piece", 1, "piece"),
    ("BOX1",   "經典盒裝",   0, "boxed",       1, "box"),
    ("BOX3",   "植萃養膚組", 0, "boxed",       3, "box"),
    ("BOX10",  "尊寵囤貨組", 0, "boxed",       10, "box"),
]

# §三 seed 口徑（面膜商品）
MASK_BALANCES = [
    # pool, category, qty, unit
    ("boxed",       "normal",   1271, "box"),
    ("boxed",       "reserved", 35,   "box"),
    ("loose_piece", "normal",   1825, "piece"),
]
# 紙袋掛 sentinel 共用包材商品（§4.4）
PACKAGING_BALANCES = [
    ("paper_bag", "normal", 932, "bag"),
]

DEFAULT_SETTINGS = [
    # key, value, value_type, description
    ("paperbag_qty_per_shipment", "1", "int", "每筆出貨預設扣紙袋數（§4.4，後台可改）"),
    ("seed_editable", "true", "bool", "seed 是否可後台編輯（§三 / 修正7）"),
    ("cost_product_test", "0", "float", "商品成本測試值（§七，後台可改）"),
    ("cost_packaging_test", "0", "float", "包材成本測試值（§七）"),
    ("cost_marketing_test", "0", "float", "行銷成本測試值（§七）"),
    ("concurrency_strategy", Config.CONCURRENCY_STRATEGY, "str", "併發防超賣策略（§4.3）"),
]

# 低水位門檻預設（inventory_thresholds，後台可改）
DEFAULT_THRESHOLDS = [
    # (sku_target, pool, threshold_qty)  sku_target: 'mask' or 'packaging'
    ("mask", "boxed", 50),
    ("mask", "loose_piece", 100),
    ("packaging", "paper_bag", 50),
]

SEED_USERS = [
    # username, password, role, display_name
    ("owner", "owner123", "owner", "老闆"),
    ("staff", "staff123", "staff", "員工"),
    ("warehouse", "warehouse123", "warehouse", "倉管"),
    ("accounting", "accounting123", "accounting", "會計"),
    ("viewer", "viewer123", "viewer", "檢視者"),
]


def _get_or_create(session, model, defaults=None, **filters):
    obj = session.query(model).filter_by(**filters).first()
    if obj:
        return obj, False
    params = dict(filters)
    if defaults:
        params.update(defaults)
    obj = model(**params)
    session.add(obj)
    session.flush()
    return obj, True


def seed(session):
    # ---- 1. 帳號 ----
    user_map = {}
    for username, pw, role, dname in SEED_USERS:
        u, _ = _get_or_create(
            session, User, username=username,
            defaults={
                "password_hash": generate_password_hash(pw),
                "role": role,
                "display_name": dname,
                "active": True,
            },
        )
        user_map[username] = u
    owner = user_map["owner"]

    # ---- 2. 商品（面膜 + sentinel 包材-共用）----
    mask, _ = _get_or_create(
        session, Product, sku=MASK_SKU,
        defaults={
            "name": "華苑 Flora Court 植萃全方位面膜",
            "category": "面膜",
            "active": True,
            "is_packaging": False,
            "created_by": owner.id,
        },
    )
    packaging, _ = _get_or_create(
        session, Product, sku=PACKAGING_SKU,
        defaults={
            "name": "包材-共用",
            "category": "包材",
            "active": True,
            "is_packaging": True,
            "created_by": owner.id,
        },
    )

    # 規格
    _get_or_create(session, ProductSpec, product_id=mask.id, spec_name="單片裝",
                   defaults={"spec_value": "1 片", "unit": "piece"})
    _get_or_create(session, ProductSpec, product_id=mask.id, spec_name="盒裝",
                   defaults={"spec_value": "5 片/盒", "unit": "box"})

    # ---- 3. 4 銷售方案 + BOM ----
    for combo, name, price, pool, qpu, unit in SALES_PLANS:
        plan, _ = _get_or_create(
            session, SalesPlan, combo_code=combo,
            defaults={"name": name, "price": price, "active": True},
        )
        _get_or_create(
            session, SalesPlanBomItem,
            sales_plan_id=plan.id, product_id=mask.id, inventory_pool=pool,
            defaults={"qty_per_unit": qpu, "unit": unit},
        )

    # ---- 4. inventory_balances seed + SEED movement 留痕（R1）----
    def seed_balance(product, pool, category, qty, unit):
        bal = session.query(InventoryBalance).filter_by(
            product_id=product.id, inventory_pool=pool, stock_category=category
        ).first()
        if bal:
            return  # 冪等：已存在不重建、不重複留痕
        bal = InventoryBalance(
            product_id=product.id, inventory_pool=pool, stock_category=category,
            qty=qty, unit=unit, updated_by=owner.id,
        )
        session.add(bal)
        session.flush()
        session.add(InventoryMovement(
            product_id=product.id, inventory_pool=pool,
            stock_category_from=None, stock_category_to=category,
            qty_delta=qty, qty_before=0, qty_after=qty,
            movement_type="SEED", ref_type="seed", ref_id="init",
            reason="initial seed", note="初始化建檔",
            operator="system", created_by=owner.id,
        ))

    for pool, cat, qty, unit in MASK_BALANCES:
        seed_balance(mask, pool, cat, qty, unit)
    for pool, cat, qty, unit in PACKAGING_BALANCES:
        seed_balance(packaging, pool, cat, qty, unit)

    # ---- 5. settings ----
    for key, value, vtype, desc in DEFAULT_SETTINGS:
        _get_or_create(
            session, Setting, key=key,
            defaults={"value": value, "value_type": vtype,
                      "description": desc, "updated_by": owner.id},
        )

    # ---- 6. inventory_thresholds 預設 ----
    for sku_target, pool, thr in DEFAULT_THRESHOLDS:
        prod = mask if sku_target == "mask" else packaging
        _get_or_create(
            session, InventoryThreshold, product_id=prod.id, inventory_pool=pool,
            defaults={"threshold_qty": thr, "updated_by": owner.id},
        )

    session.commit()


def create_all():
    if SCHEMA:
        # Postgres：確保 schema 存在（R2，不污染 public）
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    Base.metadata.create_all(engine)


def main():
    print(f"[init_db] DB = {Config.SQLALCHEMY_DATABASE_URI}")
    print(f"[init_db] SCHEMA = {SCHEMA}")
    create_all()
    insp = inspect(engine)
    tbls = insp.get_table_names(schema=SCHEMA)
    print(f"[init_db] tables created ({len(tbls)}): {sorted(tbls)}")
    session = get_session()
    try:
        seed(session)
        print("[init_db] seed done (idempotent).")
    finally:
        session.close()


if __name__ == "__main__":
    main()

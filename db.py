"""Flora Court 面膜 ERP — 資料模型層（地基，共 20 表）

紅線 R2：
- MetaData(schema=...) 依 config 決定（Postgres=flora_court / SQLite=None）。
- 所有 FK 顯式落在同 schema 內（透過 _fk() 產生帶 schema 前綴的目標）。
- 禁 cross-schema FK、禁 import earwax model。

欄位/型別/唯一鍵嚴格依藍本 §2。movement_type 閉集依 §2.4.1。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

from config import Config

# ---- schema 注入（R2）----
SCHEMA = Config.DB_SCHEMA  # Postgres="flora_court"；SQLite=None
metadata = MetaData(schema=SCHEMA)
Base = declarative_base(metadata=metadata)


def _fk(target: str) -> str:
    """產生帶 schema 前綴的 FK 目標字串，確保 FK 全落在 flora_court 內（R2）。

    target 例：'products.id' -> 'flora_court.products.id'（Postgres）/ 'products.id'（SQLite）。
    """
    if SCHEMA:
        return f"{SCHEMA}.{target}"
    return target


# =========================================================================
# §2.4.1 movement_type 合法值閉集（唯一合法集合，app + DB CHECK 皆以此為準）
# =========================================================================
MOVEMENT_TYPES = (
    # 入庫 2 類
    "PURCHASE", "RESTOCK",
    # 業務出庫 9 類
    "SALE", "GIFT", "TRIAL", "PR", "KOL_SAMPLE", "STAFF_USE", "INSTORE_USE",
    "SCRAP_LOSS", "SCRAP",
    # 系統內部 6 類
    "SPLIT_BOX", "RELEASE_RESERVE", "PAPERBAG_OUT", "ADJUSTMENT", "SEED", "IMPORT",
    # CR-4（2026-08-31）：訂單編輯/作廢的銷售回補（SALE 反向），線上 CHECK 需重建 +此值
    "SALE_REVERSAL",
)

INVENTORY_POOLS = ("boxed", "loose_piece", "paper_bag")
STOCK_CATEGORIES = ("normal", "reserved", "pr", "trial", "scrap")
COMBO_CODES = ("SINGLE", "BOX1", "BOX3", "BOX10")

_mv_in = "'" + "','".join(MOVEMENT_TYPES) + "'"
_pool_in = "'" + "','".join(INVENTORY_POOLS) + "'"
_cat_in = "'" + "','".join(STOCK_CATEGORIES) + "'"


# =========================================================================
# 1. users
# =========================================================================
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    # 5 角色：owner / staff / warehouse / accounting / viewer
    role = Column(String(20), nullable=False, default="viewer")
    display_name = Column(String(64))
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =========================================================================
# 2. products（含 image_url, active）
# =========================================================================
class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    sku = Column(String(64), unique=True, nullable=False)
    name = Column(String(128), nullable=False)
    category = Column(String(64))               # 保養品 / 面膜 等
    image_url = Column(String(512))             # 【修正14】
    active = Column(Boolean, nullable=False, default=True)  # 上下架旗標
    is_packaging = Column(Boolean, nullable=False, default=False)  # sentinel 共用包材標記
    description = Column(Text)
    created_by = Column(Integer, ForeignKey(_fk("users.id")))
    updated_by = Column(Integer, ForeignKey(_fk("users.id")))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =========================================================================
# 3. product_specs
# =========================================================================
class ProductSpec(Base):
    __tablename__ = "product_specs"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey(_fk("products.id")), nullable=False)
    spec_name = Column(String(64), nullable=False)   # 例：單片裝 / 5片/盒
    spec_value = Column(String(128))
    unit = Column(String(16))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =========================================================================
# 4. sales_plans（4 銷售組合）
# =========================================================================
class SalesPlan(Base):
    __tablename__ = "sales_plans"
    id = Column(Integer, primary_key=True)
    combo_code = Column(String(16), unique=True, nullable=False)  # SINGLE/BOX1/BOX3/BOX10
    name = Column(String(64), nullable=False)
    price = Column(Numeric(12, 2))
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        CheckConstraint(f"combo_code IN ({''.join(chr(39)+c+chr(39)+',' for c in COMBO_CODES).rstrip(',')})",
                        name="ck_sales_plans_combo_code"),
    )


# =========================================================================
# 5. sales_plan_bom_items（BOM 鎖扣法；紙袋不進 BOM）
# =========================================================================
class SalesPlanBomItem(Base):
    __tablename__ = "sales_plan_bom_items"
    id = Column(Integer, primary_key=True)
    sales_plan_id = Column(Integer, ForeignKey(_fk("sales_plans.id")), nullable=False)
    product_id = Column(Integer, ForeignKey(_fk("products.id")), nullable=False)
    inventory_pool = Column(String(20), nullable=False)  # boxed / loose_piece（紙袋不進 BOM）
    qty_per_unit = Column(Integer, nullable=False)       # 每組扣量
    unit = Column(String(16))                            # box / piece
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        CheckConstraint(f"inventory_pool IN ('boxed','loose_piece')",
                        name="ck_bom_pool_no_paperbag"),
    )


# =========================================================================
# 6. customers
# =========================================================================
class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    phone = Column(String(32))
    email = Column(String(128))
    note = Column(Text)
    created_by = Column(Integer, ForeignKey(_fk("users.id")))
    updated_by = Column(Integer, ForeignKey(_fk("users.id")))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =========================================================================
# 7. customer_addresses
# =========================================================================
class CustomerAddress(Base):
    __tablename__ = "customer_addresses"
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey(_fk("customers.id")), nullable=False)
    recipient = Column(String(64))
    phone = Column(String(32))
    address = Column(String(512), nullable=False)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =========================================================================
# 8. orders（payment_status / shipping_status / 地址）
# =========================================================================
class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    order_no = Column(String(64), unique=True, nullable=False)
    customer_id = Column(Integer, ForeignKey(_fk("customers.id")))
    # 冗餘地址欄位（下單當下快照，含姓名/電話/地址）
    recipient_name = Column(String(64))
    recipient_phone = Column(String(32))
    shipping_address = Column(String(512))
    total_amount = Column(Numeric(12, 2), default=0)   # 商品實收 − 折扣（不含運費，CR-5 口徑 B）
    # ---- CR-5（2026-08-31）：運費 / 運送方式 / 運送備註 / 折扣落地 ----
    # 全部有 default 或 nullable，舊資料列讀取不炸；線上 ALTER 見變更清單 §3。
    shipping_fee = Column(Numeric(12, 2), nullable=False, default=0, server_default="0")  # 代收運費，不進 total_amount
    shipping_method = Column(String(32))      # 閉集 711/post/pickup/other（中文見 display_labels）
    shipping_note = Column(String(256))       # 門市名等自由文字
    discount = Column(Numeric(12, 2), nullable=False, default=0, server_default="0")      # 折扣金額（原表單有欄未落地）
    payment_status = Column(String(20), nullable=False, default="unpaid")   # D4：unpaid/paid/refunded...
    shipping_status = Column(String(20), nullable=False, default="pending")  # D4：pending/shipped/delivered/cancelled
    note = Column(Text)
    # ---- CR-4（2026-08-31）：作廢＝軟刪除（不刪 order_items / payments / shipments，FK 與 movement ref 不斷）----
    voided_at = Column(DateTime)                                   # NULL＝有效單
    voided_by = Column(Integer, ForeignKey(_fk("users.id")))
    void_reason = Column(String(256))
    created_by = Column(Integer, ForeignKey(_fk("users.id")))
    updated_by = Column(Integer, ForeignKey(_fk("users.id")))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def active_orders(query):
    """所有「列出／統計訂單」的查詢統一經此排除作廢單（CR-4）。

    用法：active_orders(db.query(Order)).filter(...)；join 查詢亦可傳入含 Order 的 query。
    """
    return query.filter(Order.voided_at.is_(None))


# =========================================================================
# 9. order_items（combo_code）
# =========================================================================
class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey(_fk("orders.id")), nullable=False)
    product_id = Column(Integer, ForeignKey(_fk("products.id")), nullable=False)
    combo_code = Column(String(16), nullable=False)  # SINGLE/BOX1/BOX3/BOX10（D10 排行）
    qty = Column(Integer, nullable=False)            # 訂購組數
    unit_price = Column(Numeric(12, 2))
    subtotal = Column(Numeric(12, 2))
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


# =========================================================================
# 10. inventory_balances（核心庫存表）
#     唯一鍵 = (product_id, inventory_pool, stock_category)  【修正1】
# =========================================================================
class InventoryBalance(Base):
    __tablename__ = "inventory_balances"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey(_fk("products.id")), nullable=False)
    inventory_pool = Column(String(20), nullable=False)   # boxed/loose_piece/paper_bag
    stock_category = Column(String(20), nullable=False)   # normal/reserved/pr/trial/scrap
    qty = Column(Integer, nullable=False, default=0)
    unit = Column(String(16))                             # box/piece/bag
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(Integer, ForeignKey(_fk("users.id")))
    __table_args__ = (
        UniqueConstraint("product_id", "inventory_pool", "stock_category",
                         name="uq_inv_balance_pid_pool_cat"),
        CheckConstraint("qty >= 0", name="ck_inv_balance_qty_nonneg"),
        CheckConstraint(f"inventory_pool IN ({_pool_in})", name="ck_inv_balance_pool"),
        CheckConstraint(f"stock_category IN ({_cat_in})", name="ck_inv_balance_cat"),
    )


# =========================================================================
# 11. inventory_movements（庫存異動紀錄）  【修正10 欄位補齊】
# =========================================================================
class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    movement_id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey(_fk("products.id")), nullable=False)
    inventory_pool = Column(String(20), nullable=False)         # boxed/loose_piece/paper_bag
    stock_category_from = Column(String(20))                    # 調撥/釋放需兩端；出入庫其一可空
    stock_category_to = Column(String(20))
    qty_delta = Column(Integer, nullable=False)                 # 帶正負號
    qty_before = Column(Integer, nullable=False)                # 該列變動前存量
    qty_after = Column(Integer, nullable=False)                 # 該列變動後存量
    movement_type = Column(String(24), nullable=False)         # §2.4.1 閉集
    ref_type = Column(String(32))                              # order/shipment/adjustment/split...
    ref_id = Column(String(64))
    movement_group_id = Column(String(64))                     # 拆盒/調撥/釋放多筆共用
    reason = Column(String(256))                               # ADJUSTMENT 必填
    note = Column(Text)
    operator = Column(String(64))                             # 經手人
    created_by = Column(Integer, ForeignKey(_fk("users.id")))
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        CheckConstraint(f"movement_type IN ({_mv_in})", name="ck_movement_type_closed_set"),
        CheckConstraint(f"inventory_pool IN ({_pool_in})", name="ck_movement_pool"),
    )


# =========================================================================
# 12. payments
# =========================================================================
class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey(_fk("orders.id")), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    method = Column(String(32))            # cash/transfer/card...
    paid_at = Column(DateTime)
    status = Column(String(20), default="paid")
    note = Column(Text)
    created_by = Column(Integer, ForeignKey(_fk("users.id")))
    created_at = Column(DateTime, default=datetime.utcnow)


# =========================================================================
# 13. shipments
# =========================================================================
class Shipment(Base):
    __tablename__ = "shipments"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey(_fk("orders.id")), nullable=False)
    tracking_no = Column(String(64))
    carrier = Column(String(64))
    shipped_at = Column(DateTime)
    status = Column(String(20), default="pending")  # pending/shipped/delivered
    paper_bag_qty = Column(Integer, default=0)       # 本次出貨扣紙袋數（§4.4）
    note = Column(Text)
    created_by = Column(Integer, ForeignKey(_fk("users.id")))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =========================================================================
# 14. financial_entries（財務分錄）
# =========================================================================
class FinancialEntry(Base):
    __tablename__ = "financial_entries"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey(_fk("orders.id")))
    entry_type = Column(String(32), nullable=False)  # sale/product_cost/packaging/marketing...
    amount = Column(Numeric(12, 2), nullable=False)
    entry_date = Column(DateTime, default=datetime.utcnow)
    description = Column(Text)
    created_by = Column(Integer, ForeignKey(_fk("users.id")))
    created_at = Column(DateTime, default=datetime.utcnow)


# =========================================================================
# 15. extra_expenses（額外支出）
# =========================================================================
class ExtraExpense(Base):
    __tablename__ = "extra_expenses"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)  # PIF/貼紙小卡/紙袋加價/外盒加價/版模費/寄件包材
    amount = Column(Numeric(12, 2), nullable=False, default=0)
    category = Column(String(64))
    expense_date = Column(DateTime, default=datetime.utcnow)
    note = Column(Text)
    created_by = Column(Integer, ForeignKey(_fk("users.id")))
    created_at = Column(DateTime, default=datetime.utcnow)


# =========================================================================
# 16. historical_order_imports（匯入批次）
# =========================================================================
class HistoricalOrderImport(Base):
    __tablename__ = "historical_order_imports"
    id = Column(Integer, primary_key=True)
    batch_name = Column(String(128))
    source_file = Column(String(256))
    row_count = Column(Integer, default=0)          # 解析到的總列數（保留相容）
    imported_rows = Column(Integer, default=0)       # 落地到 rows 的列數（D14 統計）
    success_rows = Column(Integer, default=0)        # 解析成功列數
    failed_rows = Column(Integer, default=0)         # 解析失敗列數（error_message 非空）
    status = Column(String(20), default="pending")   # pending/completed/failed
    imported_by = Column(Integer, ForeignKey(_fk("users.id")))
    created_at = Column(DateTime, default=datetime.utcnow)


# =========================================================================
# 17. historical_order_import_rows（匯入明細）
# =========================================================================
class HistoricalOrderImportRow(Base):
    __tablename__ = "historical_order_import_rows"
    id = Column(Integer, primary_key=True)
    import_id = Column(Integer, ForeignKey(_fk("historical_order_imports.id")), nullable=False)
    order_no = Column(String(64))
    name = Column(String(64))
    phone = Column(String(32))
    order_date = Column(String(32))
    product_name = Column(String(128))
    qty = Column(Integer)
    unit_price = Column(Numeric(12, 2))
    amount = Column(Numeric(12, 2))
    payment_status = Column(String(20))
    shipping_status = Column(String(20))
    note = Column(Text)
    raw_json = Column(Text)              # 保留原始列（JSON），D14 raw 欄位
    error_message = Column(Text)         # 解析錯誤訊息（成功列為 NULL）
    created_at = Column(DateTime, default=datetime.utcnow)


# =========================================================================
# 18. audit_logs
# =========================================================================
class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    actor_id = Column(Integer, ForeignKey(_fk("users.id")))
    actor_name = Column(String(64))
    action = Column(String(64), nullable=False)     # create/update/delete/approve...
    target_type = Column(String(64))
    target_id = Column(String(64))
    detail = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


# =========================================================================
# 19. inventory_thresholds（低水位門檻）  鍵：(product_id, inventory_pool)
# =========================================================================
class InventoryThreshold(Base):
    __tablename__ = "inventory_thresholds"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey(_fk("products.id")), nullable=False)
    inventory_pool = Column(String(20), nullable=False)
    threshold_qty = Column(Integer, nullable=False, default=0)
    updated_by = Column(Integer, ForeignKey(_fk("users.id")))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("product_id", "inventory_pool", name="uq_threshold_pid_pool"),
        CheckConstraint(f"inventory_pool IN ({_pool_in})", name="ck_threshold_pool"),
    )


# =========================================================================
# 20. settings（通用設定 / 財務參數鍵值表）
# =========================================================================
class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(64), primary_key=True)
    value = Column(Text)
    value_type = Column(String(16), default="str")   # str/int/float/bool
    description = Column(String(256))
    updated_by = Column(Integer, ForeignKey(_fk("users.id")))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =========================================================================
# 21. earwax_sales（愛啪啪銷售/使用紀錄；甲案 2026-07-12）
#     獨立核算：不進面膜訂單/報表。item_id 指向 earwax.consumables.id，但
#     依 R2 不建 cross-schema FK；item_name 冗餘存檔以防品項改名。
# =========================================================================
class EarwaxSale(Base):
    __tablename__ = "earwax_sales"
    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, nullable=False)          # earwax.consumables.id（無 FK，R2）
    item_name = Column(String(100), nullable=False)    # 冗餘品名
    qty = Column(Integer, nullable=False)
    amount = Column(Float, nullable=False, default=0)  # 該筆實收金額（可 0＝純使用/耗用）
    operator = Column(String(64))
    note = Column(Text)
    created_by = Column(Integer, ForeignKey(_fk("users.id")))
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_earwax_sale_qty_pos"),
        CheckConstraint("amount >= 0", name="ck_earwax_sale_amount_nonneg"),
    )


# =========================================================================
# Engine / Session 工廠
# =========================================================================
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session

engine = create_engine(Config.SQLALCHEMY_DATABASE_URI, future=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, future=True))


def get_session():
    return SessionLocal()

# db.py
# 樺林美學 E•ar Wax — SQLAlchemy 資料模型
# 技術棧：Flask-SQLAlchemy + Supabase(PostgreSQL)
# 表名、欄位名完全依照 SPEC.md 第 2 節，不得自行更改。

from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import MetaData

# ──────────────────────────────────────────────────────────────────────
# Schema 隔離：本系統與泰安共用同一顆 Supabase(MUSEN-SAAS)，但所有表一律
# 關進獨立 schema「earwax」，與泰安的 public schema 完全隔開，避免 customers
# 等同名表互相污染。改動需求：所有 ForeignKey 字串都要 schema 限定（earwax.xxx）。
# ──────────────────────────────────────────────────────────────────────
SCHEMA = "earwax"

db = SQLAlchemy(metadata=MetaData(schema=SCHEMA))


# ======================================================================
# 淨利公式（權威實作，SPEC 第 1 節）
# profit = actual_price − variable_cost_per * voucher_count
#          + owner_shareholder_return * voucher_count
# ======================================================================

def compute_profit(member_type, voucher_count, actual_price, params):
    """計算單客淨利（後端權威，不信前端傳入值）。

    參數：
        member_type (str)：'member' 或 'nonmember'
        voucher_count (int)：本次消費張數
        actual_price (float)：實收金額（可為折扣後金額）
        params (Params)：當前 params 物件，提供 variable_cost_per 與
                         owner_shareholder_return

    回傳：
        float：本次淨利
            = actual_price
              − params.variable_cost_per  × voucher_count
              + params.owner_shareholder_return × voucher_count

    驗證（SPEC 1 節）：
        非會員 1 張無折扣：3980 − 2620 + 10 = 1370 ✓
        會員   1 張        ：3100 − 2620 + 10 = 490  ✓
    """
    profit = (
        float(actual_price)
        - float(params.variable_cost_per) * int(voucher_count)
        + float(params.owner_shareholder_return) * int(voucher_count)
    )
    return profit


# ======================================================================
# User — 登入帳號（含權限分級）
# ======================================================================

class User(db.Model):
    """登入帳號；role: 'staff' 只能做 4 步登錄與查庫存，'owner' 可看後台。"""
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(100), unique=True, nullable=False)  # 帳號（唯一）
    password_hash = db.Column(db.String(256), nullable=False)          # bcrypt/werkzeug hash
    name = db.Column(db.String(100), default="")                       # 顯示名稱
    role = db.Column(db.String(20), nullable=False, default="staff")   # 'staff' | 'owner'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ======================================================================
# Product — 代理產品參數
# ======================================================================

class Product(db.Model):
    """代理產品（目前僅 愛啪啪）；total_cost 為回本目標金額。"""
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)                   # 產品名稱（愛啪啪）
    total_cost = db.Column(db.Float, default=142000)                   # 回本目標（元）
    active = db.Column(db.Boolean, default=True)                       # 是否啟用
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 關聯
    params = db.relationship("Params", backref="product", uselist=False, lazy=True)
    voucher_inventory = db.relationship("VoucherInventory", backref="product", uselist=False, lazy=True)
    service_records = db.relationship("ServiceRecord", backref="product", lazy=True)


# ======================================================================
# Params — 淨利/成本參數（owner 後台可調）
# ======================================================================

class Params(db.Model):
    """淨利與成本參數；所有計算皆取此表數值，老闆可從後台修改。"""
    __tablename__ = "params"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    product_id = db.Column(db.Integer, db.ForeignKey("earwax.products.id"), nullable=False)
    member_base_price = db.Column(db.Float, default=3100)              # 會員基準售價
    member_base_profit = db.Column(db.Float, default=480)              # 會員基準淨利（參考用）
    nonmember_base_price = db.Column(db.Float, default=3980)           # 非會員基準售價
    nonmember_base_profit = db.Column(db.Float, default=1360)          # 非會員基準淨利（參考用）
    variable_cost_per = db.Column(db.Float, default=2620)              # 每張變動成本
    owner_shareholder_return = db.Column(db.Float, default=10)         # 每張店主股東回收額


# ======================================================================
# Customer — 消費者
# ======================================================================

class Customer(db.Model):
    """消費者資料；is_member=False 為非會員（預設），True 為會員。"""
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)                   # 姓名
    phone = db.Column(db.String(50), default="")                       # 電話
    is_member = db.Column(db.Boolean, default=False)                   # 是否為會員
    note = db.Column(db.Text, default="")                              # 備註
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 關聯
    service_records = db.relationship("ServiceRecord", backref="customer", lazy=True)


# ======================================================================
# VoucherInventory — 美容券庫存（張）
# ======================================================================

class VoucherInventory(db.Model):
    """美容券庫存；qty_on_hand 為現有張數，非會員消費時扣減。"""
    __tablename__ = "voucher_inventory"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    product_id = db.Column(db.Integer, db.ForeignKey("earwax.products.id"), nullable=False)
    qty_on_hand = db.Column(db.Integer, default=0)                     # 現有庫存（張）


# ======================================================================
# VoucherPurchase — 券補貨記錄（增券庫存）
# ======================================================================

class VoucherPurchase(db.Model):
    """美容券補貨記錄；每筆補貨後自動增加 voucher_inventory.qty_on_hand。"""
    __tablename__ = "voucher_purchases"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    date = db.Column(db.String(50), nullable=False)                    # 補貨日期
    qty = db.Column(db.Integer, default=0)                             # 本次補貨張數
    unit_cost = db.Column(db.Float, default=0)                        # 每張單價
    total_cost = db.Column(db.Float, default=0)                       # 本次總成本
    note = db.Column(db.Text, default="")                              # 備註
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ======================================================================
# Consumable — 耗材主檔（消耗品 + 儀器設備）
# ======================================================================

class Consumable(db.Model):
    """耗材主檔；category 區分兩類：
      'consumable' — 消耗品（管庫存、可補貨、施作扣用）：針筒/耳塞/安瓶/洗卸品/洗臉巾/防曬
      'equipment'  — 儀器設備（固定資產，只記清單，不逐次扣用）：導入儀/氣壓儀器/偵測儀/台車/破壁機
    """
    __tablename__ = "consumables"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    category = db.Column(db.String(20), nullable=False, default="consumable")
    # 'consumable'（消耗品，管庫存）| 'equipment'（儀器設備，固定資產）
    name = db.Column(db.String(100), nullable=False)                   # 耗材/儀器名稱
    qty_on_hand = db.Column(db.Integer, default=0)                     # 現有數量
    unit_cost = db.Column(db.Float, default=0)                        # 單位成本（元，可後補）
    note = db.Column(db.Text, default="")                              # 備註
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 關聯
    consumable_purchases = db.relationship("ConsumablePurchase", backref="consumable", lazy=True)
    service_consumables = db.relationship("ServiceConsumable", backref="consumable", lazy=True)


# ======================================================================
# ConsumablePurchase — 耗材補貨記錄（增消耗品庫存）
# ======================================================================

class ConsumablePurchase(db.Model):
    """消耗品補貨記錄；每筆補貨後自動增加 consumables.qty_on_hand。"""
    __tablename__ = "consumable_purchases"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    date = db.Column(db.String(50), nullable=False)                    # 補貨日期
    consumable_id = db.Column(db.Integer, db.ForeignKey("earwax.consumables.id"), nullable=False)
    qty = db.Column(db.Integer, default=0)                             # 本次補貨數量
    unit_cost = db.Column(db.Float, default=0)                        # 每件單價
    total_cost = db.Column(db.Float, default=0)                       # 本次總成本
    note = db.Column(db.Text, default="")                              # 備註
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ======================================================================
# ServiceRecord — 施作記錄（4 步輸出核心表）
# ======================================================================

class ServiceRecord(db.Model):
    """施作記錄（核心交易表）。

    建立時副作用（由 blueprint 在 db.session.commit() 前執行）：
      - 非會員 → voucher_inventory.qty_on_hand -= voucher_count（不足需擋）
      - 會員   → 不扣券庫存
      - 逐筆 ServiceConsumable → consumables.qty_on_hand -= qty
        （僅 category='consumable'；不足需擋）
      - profit  → 呼叫 compute_profit() 後端計算，不信前端傳入
    """
    __tablename__ = "service_records"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    date = db.Column(db.String(50), nullable=False)                          # 施作日期
    customer_id = db.Column(db.Integer, db.ForeignKey("earwax.customers.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("earwax.products.id"), nullable=False)
    member_type = db.Column(db.String(20), nullable=False, default="nonmember")
    # 'member'（自帶券，不扣庫存）| 'nonmember'（店家賣券，扣庫存）
    voucher_count = db.Column(db.Integer, default=1)                         # 本次張數
    actual_price = db.Column(db.Float, nullable=False)                       # 實收金額（可折扣）
    profit = db.Column(db.Float, nullable=False, default=0)                  # 後端計算淨利
    note = db.Column(db.Text, default="")                                    # 備註
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 關聯
    service_consumables = db.relationship("ServiceConsumable", backref="service_record", lazy=True)


# ======================================================================
# ServiceConsumable — 施作耗材明細（一施作 N 耗材）
# ======================================================================

class ServiceConsumable(db.Model):
    """施作耗材明細；每次施作勾選的每項耗材一列。
    is_gift=True 代表此項為贈品（仍扣庫存，不計入收費）。
    """
    __tablename__ = "service_consumables"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    service_record_id = db.Column(
        db.Integer,
        db.ForeignKey("earwax.service_records.id"),
        nullable=False
    )
    consumable_id = db.Column(
        db.Integer,
        db.ForeignKey("earwax.consumables.id"),
        nullable=False
    )
    qty = db.Column(db.Integer, nullable=False, default=1)             # 本次用量
    is_gift = db.Column(db.Boolean, nullable=False, default=False)     # 是否為贈品
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ======================================================================
# MaskSale — 面膜銷售記錄（SPEC §8.6）
# ======================================================================

class MaskSale(db.Model):
    """面膜銷售記錄；依通路（盒裝/單片/包裝袋）與會員屬性分欄位記錄。"""
    __tablename__ = "mask_sales"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    date = db.Column(db.String(50), nullable=False)                    # 銷售日期
    general_box_qty = db.Column(db.Integer, default=0)                 # 一般盒裝數量
    general_box_amount = db.Column(db.Float, default=0)                # 一般盒裝金額
    pr_box_qty = db.Column(db.Integer, default=0)                      # 公關盒裝數量
    general_piece_qty = db.Column(db.Integer, default=0)               # 一般單片數量
    general_piece_amount = db.Column(db.Float, default=0)              # 一般單片金額
    pr_piece_qty = db.Column(db.Integer, default=0)                    # 公關單片數量
    bag_qty = db.Column(db.Integer, default=0)                         # 包裝袋數量
    note = db.Column(db.Text, nullable=True)                           # 備註（可空）
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ======================================================================
# MaskCostItem — 面膜成本項目（SPEC §8.6）
# ======================================================================

class MaskCostItem(db.Model):
    """面膜成本項目主檔；記錄各類成本名稱與金額，供 ROI 儀表板使用。"""
    __tablename__ = "mask_cost_items"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)                   # 成本項目名稱
    amount = db.Column(db.Float, nullable=False)                       # 成本金額
    note = db.Column(db.Text, default="")                              # 備註
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ======================================================================
# MaskInventory — 面膜庫存（SPEC §8.6）
# ======================================================================

class MaskInventory(db.Model):
    """面膜庫存；以 item_key 對應固定品項（general_box/pr_box/general_piece/pr_piece/bag）。"""
    __tablename__ = "mask_inventory"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    item_key = db.Column(db.String(50), nullable=False, unique=True)   # 品項識別鍵
    name = db.Column(db.String(100), nullable=False)                   # 品項顯示名稱
    qty_on_hand = db.Column(db.Integer, default=0)                     # 現有庫存數量
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ======================================================================
# MaskPurchase — 面膜進貨記錄（SPEC §8.6）
# ======================================================================

class MaskPurchase(db.Model):
    """面膜進貨記錄；每筆進貨後自動增加 mask_inventory.qty_on_hand。"""
    __tablename__ = "mask_purchases"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    date = db.Column(db.String(50), nullable=False)                    # 進貨日期
    item_key = db.Column(db.String(50), nullable=False)                # 對應 mask_inventory.item_key
    qty = db.Column(db.Integer, nullable=False)                        # 本次進貨數量
    note = db.Column(db.Text, default="")                              # 備註
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

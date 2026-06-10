# init_db.py
# 樺林美學 E•ar Wax — 建表 + 種子資料（可重複執行，已存在就略過）
#
# 用法：
#   python init_db.py
#
# 必要環境變數（可寫進 .env，再用 python-dotenv 載入）：
#   DATABASE_URL     — Supabase PostgreSQL 連線字串
#   OWNER_USER       — owner 帳號名稱（預設：owner）
#   OWNER_PASS       — owner 登入密碼（預設：OwnerPass@2024）
#   STAFF_USER       — staff 帳號名稱（預設：staff）
#   STAFF_PASS       — staff 登入密碼（預設：StaffPass@2024）
#
# 注意：生產環境請務必在 .env 設定強密碼，不要使用預設值。

import os

from flask import Flask
from sqlalchemy import text
from werkzeug.security import generate_password_hash

# 嘗試載入 .env（若有安裝 python-dotenv）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # 沒有 python-dotenv 就略過，直接讀 os.environ

from db import (
    db,
    SCHEMA,
    User,
    Product,
    Params,
    VoucherInventory,
    Consumable,
    ServiceConsumable,
    MaskInventory,
)


def create_app():
    """建立最小化 Flask app，僅供 init_db 使用。"""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:changeme@localhost:5432/earwax_hualin"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def seed_users():
    """種子：owner / staff 兩帳號（已存在就略過）。"""
    accounts = [
        {
            "username": os.environ.get("OWNER_USER", "owner"),
            "password": os.environ.get("OWNER_PASS", "OwnerPass@2024"),
            "name": "老闆",
            "role": "owner",
        },
        {
            "username": os.environ.get("STAFF_USER", "staff"),
            "password": os.environ.get("STAFF_PASS", "StaffPass@2024"),
            "name": "員工",
            "role": "staff",
        },
    ]

    for acc in accounts:
        existing = User.query.filter_by(username=acc["username"]).first()
        if existing:
            print(f"  - 帳號 [{acc['username']}] 已存在，略過。")
            continue
        user = User(
            username=acc["username"],
            password_hash=generate_password_hash(acc["password"]),
            name=acc["name"],
            role=acc["role"],
        )
        db.session.add(user)
        print(f"  - 建立帳號 [{acc['username']}]（role={acc['role']}）")

    db.session.commit()


def seed_product_and_params():
    """種子：愛啪啪 Product + Params（已存在就略過）。"""
    product = Product.query.filter_by(name="愛啪啪").first()
    if product:
        print("  - Product [愛啪啪] 已存在，略過。")
    else:
        product = Product(
            name="愛啪啪",
            total_cost=142000,   # 回本目標（元）
            active=True,
        )
        db.session.add(product)
        db.session.flush()  # 取得 product.id 供 Params 使用
        print("  - 建立 Product [愛啪啪]")

    # 確保 Params 存在
    params = Params.query.filter_by(product_id=product.id).first()
    if params:
        print(f"  - Params (product_id={product.id}) 已存在，略過。")
    else:
        params = Params(
            product_id=product.id,
            member_base_price=3100,            # 會員基準售價
            member_base_profit=480,            # 會員基準淨利（參考用）
            nonmember_base_price=3980,         # 非會員基準售價
            nonmember_base_profit=1360,        # 非會員基準淨利（參考用）
            variable_cost_per=2620,            # 每張變動成本
            owner_shareholder_return=10,       # 每張店主股東回收額
        )
        db.session.add(params)
        print(f"  - 建立 Params (product_id={product.id})")

    db.session.commit()
    return product


def seed_voucher_inventory(product):
    """種子：券庫存初始為 0（已存在就略過）。"""
    inv = VoucherInventory.query.filter_by(product_id=product.id).first()
    if inv:
        print(f"  - VoucherInventory (product_id={product.id}) 已存在，略過。")
    else:
        inv = VoucherInventory(
            product_id=product.id,
            qty_on_hand=0,  # 初始庫存 0 張，後續由補貨入帳
        )
        db.session.add(inv)
        db.session.commit()
        print(f"  - 建立 VoucherInventory (product_id={product.id})，qty_on_hand=0")


def seed_consumables():
    """種子：消耗品 + 儀器設備（已存在就略過）。

    消耗品（category='consumable'）：管庫存、施作可扣用
    儀器設備（category='equipment'）：固定資產，只記清單
    """
    consumable_items = [
        # (category, name)
        ("consumable", "針筒"),
        ("consumable", "耳塞"),
        ("consumable", "安瓶"),
        ("consumable", "洗卸品"),
        ("consumable", "洗臉巾"),
        ("consumable", "防曬"),
        ("equipment",  "導入儀"),
        ("equipment",  "氣壓儀器"),
        ("equipment",  "頭皮臉部偵測儀"),
        ("equipment",  "台車"),
        ("equipment",  "破壁機"),
    ]

    for category, name in consumable_items:
        existing = Consumable.query.filter_by(name=name).first()
        if existing:
            print(f"  - Consumable [{name}] 已存在，略過。")
            continue
        item = Consumable(
            category=category,
            name=name,
            qty_on_hand=0,   # 初始庫存 0，後續由補貨入帳
            unit_cost=0,     # 單位成本待後補
            note="",
        )
        db.session.add(item)
        print(f"  - 建立 Consumable [{name}]（category={category}）")

    db.session.commit()


def seed_mask_inventory():
    """種子：面膜 5 項獨立庫存（SPEC §8.4/§8.6/§8.8），初始 qty_on_hand=0。

    冪等：依 item_key 判斷，已存在就略過。各品項互不換算（SPEC §8.4）。
    """
    mask_items = [
        # (item_key, name)
        ("general_box",   "一般盒"),
        ("pr_box",        "公關盒"),
        ("general_piece", "一般片"),
        ("pr_piece",      "公關片"),
        ("bag",           "包裝材"),
    ]

    for item_key, name in mask_items:
        existing = MaskInventory.query.filter_by(item_key=item_key).first()
        if existing:
            print(f"  - MaskInventory [{item_key}] 已存在，略過。")
            continue
        inv = MaskInventory(
            item_key=item_key,
            name=name,
            qty_on_hand=0,   # 初始庫存 0，後續由補貨入帳
        )
        db.session.add(inv)
        print(f"  - 建立 MaskInventory [{item_key}]（{name}）qty_on_hand=0")

    db.session.commit()


def main():
    app = create_app()
    with app.app_context():
        print("=" * 50)
        print(f"確保 schema [{SCHEMA}] 存在（與泰安 public 隔離）...")
        db.session.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        db.session.commit()
        print(f"建立資料表（schema={SCHEMA}）...")
        db.create_all()
        print("資料表建立完成。\n")

        print("開始寫入種子資料 ...")

        print("\n[帳號]")
        seed_users()

        print("\n[代理產品 + 參數]")
        product = seed_product_and_params()

        print("\n[美容券庫存]")
        seed_voucher_inventory(product)

        print("\n[耗材庫存]")
        seed_consumables()

        print("\n[面膜庫存]")
        seed_mask_inventory()

        print("\n種子資料完成。")
        print("=" * 50)

        # 驗收摘要
        print("\n各表筆數確認：")
        from db import Customer, VoucherPurchase, ConsumablePurchase, ServiceRecord
        for model, label in [
            (User,                "users（帳號）"),
            (Product,             "products（代理產品）"),
            (Params,              "params（淨利參數）"),
            (VoucherInventory,    "voucher_inventory（券庫存）"),
            (Consumable,          "consumables（耗材主檔）"),
            (Customer,            "customers（消費者）"),
            (VoucherPurchase,     "voucher_purchases（補券記錄）"),
            (ConsumablePurchase,  "consumable_purchases（補耗材記錄）"),
            (ServiceRecord,       "service_records（施作記錄）"),
            (ServiceConsumable,   "service_consumables（施作耗材明細）"),
            (MaskInventory,       "mask_inventory（面膜庫存）"),
        ]:
            count = model.query.count()
            print(f"  - {label}: {count} 筆")

        print("\n完成。可啟動 Flask app 開始使用。")


if __name__ == "__main__":
    main()

"""Flora Court 面膜 ERP — 全站顯示中文化共用對照（單一來源）。

職責：把使用者看得到的英文 enum / code 轉成中文「顯示文字」。
- 底層存的值維持英文 code 不變（只改顯示）；本模組不碰任何寫入、不改 enum 值。
- 對照集中於此一檔，全站共用：經由 register_display_helpers(app) 註冊成
  Jinja filter（顯示用）+ context_processor（下拉選單用，value 仍存 code）。
- 查無對應的 code 一律原樣回傳（label(code) 找不到就回 code 本身，絕不崩）。

注意：combo_code 的中文名以 sales_plans seed 的 plan_name 為準（見 init_db.py）；
此處的 COMBO 僅為「查不到 DB 時的 fallback」，DB 有值時應優先採 DB（reports 已自行處理）。
"""

# combo_code → 中文
# 新模型（建單只產生這兩個）：LOOSE=片、BOX=盒。
# 舊 4-combo 對照保留相容，避免歷史資料顯示崩。
COMBO_LABELS = {
    "LOOSE": "片",
    "BOX": "盒",
    # ---- 舊 4-combo 相容（歷史資料）----
    "SINGLE": "單片體驗組",
    "BOX1": "經典盒裝",
    "BOX3": "植萃養膚組",
    "BOX10": "尊寵囤貨組",
}

# inventory_pool → 中文
POOL_LABELS = {
    "boxed": "盒裝",
    "loose_piece": "裸片",
    "paper_bag": "紙袋",
}

# stock_category → 中文
CAT_LABELS = {
    "normal": "正常",
    "reserved": "預留",
    "pr": "公關品",
    "trial": "試用品",
    "scrap": "損耗報廢",
}

# payment_status → 中文
PAYMENT_LABELS = {
    "unpaid": "未付款",
    "partial": "部分付款",
    "paid": "已付款",
    "refunded": "已退款",
}

# shipping_status → 中文
SHIPPING_LABELS = {
    "pending": "待出貨",
    "packing": "備貨中",
    "shipped": "已出貨",
    "delivered": "已送達",
    "cancelled": "已取消",
}

# shipping_method → 中文（CR-5 閉集；值存英文 code）
SHIPPING_METHOD_LABELS = {
    "711": "711 店到店",
    "post": "郵寄",
    "pickup": "自取",
    "other": "其他",
}

# movement_type → 中文（§2.4.1 閉集）
MTYPE_LABELS = {
    "PURCHASE": "進貨",
    "RESTOCK": "補貨",
    "SALE": "銷售",
    "GIFT": "贈品",
    "TRIAL": "試用品",
    "PR": "公關品",
    "KOL_SAMPLE": "KOL寄樣",
    "STAFF_USE": "員工領用",
    "INSTORE_USE": "店內使用",
    "SCRAP_LOSS": "損耗",
    "SCRAP": "報廢",
    "SPLIT_BOX": "拆盒",
    "RELEASE_RESERVE": "釋放預留",
    "PAPERBAG_OUT": "紙袋出庫",
    "ADJUSTMENT": "調整",
    "SEED": "期初",
    "IMPORT": "匯入",
    "SALE_REVERSAL": "銷售回補",   # CR-4：訂單編輯/作廢回補
}

# 訂單作廢標籤（CR-4；列表灰字標示）
VOIDED_LABEL = "已作廢"

# users.role → 中文
ROLE_LABELS = {
    "owner": "老闆",
    "staff": "員工",
    "warehouse": "倉管",
    "accounting": "會計",
    "viewer": "檢視",
}


def _lookup(mapping, code):
    """查不到 / None 一律原樣回傳，絕不崩。"""
    if code is None:
        return ""
    return mapping.get(code, code)


def combo_label(code):
    return _lookup(COMBO_LABELS, code)


def pool_label(code):
    return _lookup(POOL_LABELS, code)


def cat_label(code):
    return _lookup(CAT_LABELS, code)


def payment_label(code):
    return _lookup(PAYMENT_LABELS, code)


def shipping_label(code):
    return _lookup(SHIPPING_LABELS, code)


def mtype_label(code):
    return _lookup(MTYPE_LABELS, code)


def shipping_method_label(code):
    return _lookup(SHIPPING_METHOD_LABELS, code)


def role_label(code):
    return _lookup(ROLE_LABELS, code)


def register_display_helpers(app):
    """全站註冊：Jinja filter（顯示用）+ context_processor（下拉用對照表）。

    Filter 用法（顯示）：{{ o.payment_status | payment_label }}
    下拉用法（value 仍存 code）：
      {% for s in payment_statuses %}
        <option value="{{ s }}">{{ s | payment_label }}</option>
      {% endfor %}
    """
    app.add_template_filter(combo_label, "combo_label")
    app.add_template_filter(pool_label, "pool_label")
    app.add_template_filter(cat_label, "cat_label")
    app.add_template_filter(payment_label, "payment_label")
    app.add_template_filter(shipping_label, "shipping_label")
    app.add_template_filter(mtype_label, "mtype_label")
    app.add_template_filter(shipping_method_label, "shipping_method_label")
    app.add_template_filter(role_label, "role_label")

    @app.context_processor
    def _inject_label_maps():
        # 模板可直接取整份對照表（例如需要 .get 或迴圈時）
        return {
            "COMBO_LABELS": COMBO_LABELS,
            "POOL_LABELS": POOL_LABELS,
            "CAT_LABELS": CAT_LABELS,
            "PAYMENT_LABELS": PAYMENT_LABELS,
            "SHIPPING_LABELS": SHIPPING_LABELS,
            "MTYPE_LABELS": MTYPE_LABELS,
            "SHIPPING_METHOD_LABELS": SHIPPING_METHOD_LABELS,
            "VOIDED_LABEL": VOIDED_LABEL,
            "ROLE_LABELS": ROLE_LABELS,
        }

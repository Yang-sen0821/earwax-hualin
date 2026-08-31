"""Flora Court 面膜 ERP — 操作留痕共用工具（CR-8，2026-08-31）。

單一寫入口：所有 blueprint 要留 audit_logs 一律呼叫 write_audit()，不再各自 db.add(AuditLog(...))。
- actor 取自 session 使用者（auth.current_user）；未登入 / 系統作業 actor_id=None、actor_name="system"。
- detail 一律存 JSON 字串（ensure_ascii=False），沿用既有慣例：
    {"before": {...}, "after": {...}}   欄位變更（只放有變的欄）
    其餘事件自帶語意鍵（reason / items / qty_before / qty_after / bulk_group_id …）
- 密碼類欄位絕不寫值（見 SECRET_FIELDS：只寫「已變更」）。
- 本模組不 commit；與呼叫端同一 transaction，呼叫端 rollback 時留痕一併消失（不會留下「做了但沒成」的假紀錄）。

另提供 summarize()：把 detail JSON 轉成人話摘要，給 /admin/audit 頁與 Excel「操作紀錄」分頁共用。
"""
import json
from datetime import datetime, date, timedelta
from decimal import Decimal

from db import AuditLog
from display_labels import (
    action_label, payment_label, shipping_label, pool_label, cat_label,
    mtype_label, combo_label, shipping_method_label,
)

# 不得寫值的欄位（只寫「已變更」）
SECRET_FIELDS = {"password", "password_hash", "pw"}

# detail 欄位 → 中文（摘要用；查不到原字）
FIELD_LABELS = {
    "name": "名稱", "sku": "SKU", "category": "類別", "image_url": "圖片",
    "description": "說明", "active": "上架", "is_packaging": "包材",
    "phone": "電話", "email": "Email", "note": "備註", "address": "地址",
    "recipient": "收件人", "is_default": "預設",
    "recipient_name": "收件人", "recipient_phone": "電話",
    "shipping_address": "地址", "shipping_fee": "運費",
    "shipping_method": "運送方式", "shipping_note": "運送備註",
    "discount": "折扣", "total_amount": "總金額", "items": "品項",
    "payment_status": "付款狀態", "shipping_status": "出貨狀態",
    "amount": "金額", "expense_date": "日期", "value": "值",
    "threshold_qty": "門檻", "inventory_pool": "庫存池",
    "spec_name": "規格名", "spec_value": "規格值", "unit": "單位",
    "qty_on_hand": "數量", "unit_cost": "單位成本",
    "customer_id": "客戶", "customer_name": "客戶",
}

# 值的中文化（依欄位名決定用哪張對照）
_VALUE_LABELERS = {
    "payment_status": payment_label,
    "shipping_status": shipping_label,
    "shipping_method": shipping_method_label,
    "inventory_pool": pool_label,
    "pool": pool_label,
    "stock_category": cat_label,
    "category_code": cat_label,
    "movement_type": mtype_label,
    "combo_code": combo_label,
}


def _json_default(v):
    """沿用 orders 既有慣例：Decimal → 字串（保留小數位，如 "800.00"）、datetime → ISO 字串。"""
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def actor_info():
    """回 (actor_id, actor_name)；未登入 = (None, 'system')。"""
    from auth import current_user  # 延遲 import：auth 也會用本模組（login 留痕），避免循環
    u = current_user()
    if u is None:
        return None, "system"
    return u.id, (u.display_name or u.username)


def write_audit(db, action, target_type, target_id, detail=None,
                actor_id=None, actor_name=None):
    """寫一筆 audit_logs（不 commit）。actor 未給就取 session 使用者。回該 AuditLog 物件。"""
    if actor_id is None and actor_name is None:
        actor_id, actor_name = actor_info()
    row = AuditLog(
        actor_id=actor_id, actor_name=actor_name,
        action=action, target_type=target_type,
        target_id=(str(target_id) if target_id is not None else None),
        detail=json.dumps(detail if detail is not None else {},
                          ensure_ascii=False, default=_json_default),
    )
    db.add(row)
    return row


def snapshot(obj, fields):
    """把 ORM 物件指定欄位抓成 dict（密碼欄只留佔位）。"""
    out = {}
    for f in fields:
        v = getattr(obj, f, None)
        out[f] = "***" if f in SECRET_FIELDS else v
    return out


def diff(before, after):
    """只保留有變動的欄位；回 (before_changed, after_changed)。兩邊皆空代表沒變。"""
    b, a = {}, {}
    for k in set(before) | set(after):
        vb, va = before.get(k), after.get(k)
        if _norm(vb) != _norm(va):
            if k in SECRET_FIELDS:
                b[k], a[k] = "***", "已變更"
            else:
                b[k], a[k] = vb, va
    return b, a


def _norm(v):
    if isinstance(v, Decimal):
        return float(v)
    if v == "":
        return None
    return v


# -------------------------------------------------------------------------
# 時間顯示：DB 存 UTC（datetime.utcnow）；操作紀錄頁與 Excel 分頁以台北時間（UTC+8，無夏令）顯示
# -------------------------------------------------------------------------
DISPLAY_TZ_OFFSET = timedelta(hours=8)


def to_local(dt, reverse=False):
    """UTC → 台北；reverse=True 時 台北 → UTC（給日期篩選用）。None 原樣回。"""
    if dt is None:
        return None
    return dt - DISPLAY_TZ_OFFSET if reverse else dt + DISPLAY_TZ_OFFSET


def fmt_ts(dt):
    return to_local(dt).strftime("%Y-%m-%d %H:%M:%S") if dt else ""


# -------------------------------------------------------------------------
# 人話摘要（/admin/audit 與 Excel 共用）
# -------------------------------------------------------------------------
def parse_detail(raw):
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {"value": d}
    except (TypeError, ValueError):
        return {"raw": raw}


def _fmt_val(field, v):
    if v is None or v == "":
        return "（空）"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, str) and _looks_decimal(v):
        f = float(v)
        v = int(f) if f.is_integer() else f
    labeler = _VALUE_LABELERS.get(field)
    if labeler and isinstance(v, str):
        return labeler(v)
    if isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False, default=_json_default)
        return s if len(s) <= 60 else s[:57] + "…"
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "…"


def _looks_decimal(s):
    """"800.00" / "-12.5" 這類 Decimal 字串（不含日期、單號）。"""
    if not s or len(s) > 20 or "." not in s:
        return False
    try:
        float(s)
        return all(ch.isdigit() or ch in ".-" for ch in s)
    except ValueError:
        return False


def _before_after(d):
    before, after = d.get("before") or {}, d.get("after") or {}
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    def order_key(k):
        # 名稱類欄位排最前，其餘依字母
        return (0 if k in ("name", "spec_name", "key", "address") else 1, k)

    # 純新增（只有 after）／純刪除（只有 before）：列非空欄位一次講完，不逐欄畫箭頭
    if after and not before:
        vals = [f"{FIELD_LABELS.get(k, k)} {_fmt_val(k, after[k])}"
                for k in sorted(after, key=order_key) if after[k] not in (None, "")]
        return ["新增：" + "、".join(vals)] if vals else []
    if before and not after:
        vals = [f"{FIELD_LABELS.get(k, k)} {_fmt_val(k, before[k])}"
                for k in sorted(before, key=order_key) if before[k] not in (None, "")]
        return ["刪除前：" + "、".join(vals)] if vals else []
    parts = []
    for k in sorted(set(before) | set(after), key=order_key):
        vb, va = before.get(k), after.get(k)
        label = FIELD_LABELS.get(k, k)
        if k not in before:
            parts.append(f"{label}：{_fmt_val(k, va)}")
        elif k not in after:
            parts.append(f"{label}：{_fmt_val(k, vb)}（刪除）")
        else:
            parts.append(f"{label}：{_fmt_val(k, vb)} → {_fmt_val(k, va)}")
    return parts


def summarize(action, detail):
    """action + detail(dict 或 JSON 字串) → 一行人話。任何情況不崩，退回 key: value。"""
    d = detail if isinstance(detail, dict) else parse_detail(detail)
    parts = []
    try:
        if action == "order_create":
            items = d.get("items") or []
            names = "、".join(
                f"{it.get('product_name') or '#' + str(it.get('product_id'))}"
                f"({combo_label(it.get('combo_code'))})×{it.get('qty')}"
                for it in items[:4])
            if len(items) > 4:
                names += f"…共 {len(items)} 項"
            parts.append(f"單號 {d.get('order_no', '')}")
            if d.get("customer_name"):
                parts.append(f"客戶 {d['customer_name']}")
            if names:
                parts.append(names)
            if d.get("total_amount") is not None:
                parts.append(f"金額 {_fmt_val('total_amount', d['total_amount'])}")
            if d.get("created_customer"):
                parts.append(f"同時新增客戶「{d['created_customer']}」")
        elif action == "order_void":
            parts.append(f"原因：{d.get('reason', '')}")
            parts.append("已出貨單、庫存不回補" if d.get("shipped") else "庫存已回補")
            if d.get("payment_status_before") != d.get("payment_status_after"):
                parts.append(f"付款狀態：{payment_label(d.get('payment_status_before'))} → "
                             f"{payment_label(d.get('payment_status_after'))}")
            if d.get("bulk"):
                parts.append(f"批次作廢 {str(d.get('bulk_group_id', ''))[:8]}")
        elif action.startswith("inventory_") or action in ("earwax_sale_create",):
            if d.get("product_name") or d.get("item"):
                parts.append(str(d.get("product_name") or d.get("item")))
            loc = "／".join(x for x in (
                pool_label(d.get("pool")) if d.get("pool") else "",
                cat_label(d.get("category")) if d.get("category") else "") if x)
            if loc:
                parts.append(loc)
            if d.get("movement_type"):
                parts.append(mtype_label(d["movement_type"]))
            if d.get("qty_before") is not None and d.get("qty_after") is not None:
                parts.append(f"{_fmt_val('q', d['qty_before'])} → {_fmt_val('q', d['qty_after'])}")
            elif d.get("qty") is not None:
                parts.append(f"數量 {_fmt_val('q', d['qty'])}")
            if d.get("box_qty") is not None:
                parts.append(f"拆 {d['box_qty']} 盒 → {d.get('pieces', '')} 片")
            if d.get("reason"):
                parts.append(f"原因：{d['reason']}")
            if d.get("amount") is not None and action == "earwax_sale_create":
                parts.append(f"金額 {_fmt_val('amount', d['amount'])}")
        elif action == "setting_update":
            parts.append(f"{d.get('key', '')}")
        elif action in ("login_ok", "login_fail"):
            parts.append(f"帳號 {d.get('username', '')}")
            if d.get("ip"):
                parts.append(f"IP {d['ip']}")
        # 通用：before/after 逐欄
        ba = _before_after(d)
        if ba:
            parts.extend(ba)
        if not parts:
            for k, v in d.items():
                if k in ("before", "after", "snapshot"):
                    continue
                parts.append(f"{FIELD_LABELS.get(k, k)}：{_fmt_val(k, v)}")
                if len(parts) >= 6:
                    break
    except Exception:  # noqa: BLE001 — 摘要絕不讓頁面崩
        parts = [str(d)[:120]]
    text = "；".join(p for p in parts if p)
    return text or action_label(action)

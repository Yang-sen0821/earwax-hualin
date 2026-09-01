# -*- coding: utf-8 -*-
"""CR-12 驗收：客戶階級欄（一般客戶／經銷商／代理商）— 2026-09-01。

throwaway sqlite 放 %TEMP%；env 在 import 任何 app 模組前先設定。
驗證項目：
  A model 欄位：Customer.tier String(32) NOT NULL default 'general' server_default 'general'；不帶 tier 建立 → general
  B 桌面新增 dealer 成功；非法值被拒（不建立）；編輯改 agent 成功、非法值被拒（不變）
  C 列表 ?tier=dealer 只列 dealer；階級欄顯示中文；篩選下拉存在
  D 明細顯示中文標籤；手機列表／明細顯示中文標籤；手機新增可帶 tier
  E audit customer_create after 含 tier；customer_update before/after 含 tier；summarize 顯示中文
  F 編輯不帶 tier 欄位 → 維持原值
"""
import os
import sys
import json
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_TMP_DB = os.path.join(tempfile.gettempdir(), "flora_court_tier_test.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")
os.environ["ENABLE_MOBILE"] = "true"
os.environ["SECRET_KEY"] = "test-secret"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import init_db  # noqa: E402
from db import get_session, Customer, AuditLog  # noqa: E402
from display_labels import CUSTOMER_TIERS, tier_label  # noqa: E402
from audit_util import summarize  # noqa: E402
from app import create_app  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


def fresh():
    s = get_session()
    s.expire_all()
    return s


def cust_by_name(name):
    db = fresh()
    c = db.query(Customer).filter_by(name=name).first()
    out = (c.id, c.tier) if c else (None, None)
    db.close()
    return out


def cust_count():
    db = fresh()
    n = db.query(Customer).count()
    db.close()
    return n


def audit_last(action, cid):
    db = fresh()
    a = (db.query(AuditLog).filter_by(action=action, target_id=str(cid))
         .order_by(AuditLog.id.desc()).first())
    d = json.loads(a.detail) if a else None
    db.close()
    return d


def main():
    init_db.create_all()
    s = get_session()
    try:
        init_db.seed(s)
    finally:
        s.close()

    app = create_app()
    app.config["TESTING"] = True
    sc = app.test_client()
    login(sc, "staff", "staff123")
    vc = app.test_client()
    login(vc, "viewer", "viewer123")

    # A. model
    col = Customer.__table__.c.tier
    check("A Customer.tier = VARCHAR(32) NOT NULL", str(col.type) == "VARCHAR(32)" and col.nullable is False, str(col.type))
    check("A default 'general' / server_default 'general'", col.default.arg == "general"
          and col.server_default is not None and str(col.server_default.arg) == "general")
    check("A CUSTOMER_TIERS 三階（general/dealer/agent）", [c for c, _ in CUSTOMER_TIERS] == ["general", "dealer", "agent"]
          and tier_label("dealer") == "經銷商" and tier_label("agent") == "代理商" and tier_label("general") == "一般客戶")
    r = sc.post("/customers/new", data={"name": "預設客"}, follow_redirects=True)
    cid0, t0 = cust_by_name("預設客")
    check("A 不帶 tier 建立 → general", cid0 is not None and t0 == "general", f"tier={t0}")
    check("A 舊資料相容：tier_label(None) → 一般客戶", tier_label(None) == "一般客戶")

    # B. 桌面新增 / 編輯
    n0 = cust_count()
    r = sc.post("/customers/new", data={"name": "經銷A", "phone": "0911", "tier": "dealer"}, follow_redirects=True)
    cid1, t1 = cust_by_name("經銷A")
    check("B 新增 dealer 成功", cid1 is not None and t1 == "dealer" and "客戶已建立" in r.get_data(as_text=True))
    r = sc.post("/customers/new", data={"name": "壞階級", "tier": "vip"}, follow_redirects=True)
    check("B 非法值 vip 被拒、未建立", "無效的客戶階級" in r.get_data(as_text=True) and cust_by_name("壞階級")[0] is None
          and cust_count() == n0 + 1)
    r = sc.post(f"/customers/{cid1}/edit", data={"name": "經銷A", "phone": "0911", "tier": "agent"}, follow_redirects=True)
    check("B 編輯改 agent 成功", cust_by_name("經銷A")[1] == "agent" and "客戶已更新" in r.get_data(as_text=True))
    r = sc.post(f"/customers/{cid1}/edit", data={"name": "經銷A", "tier": "boss"}, follow_redirects=True)
    check("B 編輯非法值被拒、維持 agent", "無效的客戶階級" in r.get_data(as_text=True) and cust_by_name("經銷A")[1] == "agent")
    r = sc.post(f"/customers/{cid1}/edit", data={"name": "經銷A", "phone": "0911", "tier": "dealer"}, follow_redirects=True)
    check("B 編輯回 dealer", cust_by_name("經銷A")[1] == "dealer")
    sc.post("/customers/new", data={"name": "代理B", "tier": "agent"}, follow_redirects=True)
    cid2, t2 = cust_by_name("代理B")
    check("B 新增 agent 成功", t2 == "agent")

    # C. 列表
    lst = sc.get("/customers/").get_data(as_text=True)
    check("C 列表有階級欄與篩選下拉", "階級" in lst and 'name="tier"' in lst and "全部階級" in lst)
    check("C 列表顯示中文標籤", "經銷商" in lst and "代理商" in lst and "一般客戶" in lst)
    lst_d = sc.get("/customers/?tier=dealer").get_data(as_text=True)
    check("C ?tier=dealer 只列 dealer", "經銷A" in lst_d and "代理B" not in lst_d and "預設客" not in lst_d)
    lst_a = sc.get("/customers/?tier=agent").get_data(as_text=True)
    check("C ?tier=agent 只列 agent", "代理B" in lst_a and "經銷A" not in lst_a)
    lst_x = sc.get("/customers/?tier=zzz").get_data(as_text=True)
    check("C 非閉集篩選值忽略（列全部）", "經銷A" in lst_x and "代理B" in lst_x and "預設客" in lst_x)
    lst_r = sc.get("/customers/?tier=dealer&sort=rank")
    check("C 篩選 + 排行排序 200", lst_r.status_code == 200 and "經銷A" in lst_r.get_data(as_text=True))

    # D. 明細 / 手機
    det = sc.get(f"/customers/{cid1}").get_data(as_text=True)
    check("D 桌面明細顯示「經銷商」", 'id="customer-tier"' in det and "經銷商" in det)
    frm = sc.get(f"/customers/{cid1}/edit").get_data(as_text=True)
    check("D 編輯表單階級下拉預選 dealer", 'value="dealer" selected' in frm)
    frm_new = sc.get("/customers/new").get_data(as_text=True)
    check("D 新增表單預設 general", 'value="general" selected' in frm_new)
    ml = sc.get("/m/customers").get_data(as_text=True)
    check("D 手機列表顯示階級標籤", "經銷商" in ml and "代理商" in ml)
    md = sc.get(f"/m/customers/{cid2}").get_data(as_text=True)
    check("D 手機明細顯示「代理商」", "階級" in md and "代理商" in md)
    mn = sc.get("/m/customers/new").get_data(as_text=True)
    check("D 手機新增表單有階級下拉", 'name="tier"' in mn and "經銷商" in mn)
    r = sc.post("/m/customers/new", data={"name": "手機經銷", "tier": "dealer"}, follow_redirects=True)
    cid3, t3 = cust_by_name("手機經銷")
    check("D 手機新增 dealer 成功", t3 == "dealer")
    r = sc.post("/m/customers/new", data={"name": "手機壞", "tier": "xx"}, follow_redirects=True)
    check("D 手機非法值被拒", cust_by_name("手機壞")[0] is None and "無效的客戶階級" in r.get_data(as_text=True))
    check("D viewer 列表可看階級欄", "經銷商" in vc.get("/customers/").get_data(as_text=True))

    # E. audit
    d = audit_last("customer_create", cid2)
    check("E customer_create after 含 tier=agent", d and d["after"].get("tier") == "agent", str(d))
    d = audit_last("customer_update", cid1)
    check("E customer_update before/after 含 tier（agent → dealer）", d and d["before"].get("tier") == "agent"
          and d["after"].get("tier") == "dealer", str(d))
    smry = summarize("customer_update", d)
    check("E summarize 階級中文：代理商 → 經銷商", "階級" in smry and "代理商 → 經銷商" in smry, smry)
    d3 = audit_last("customer_create", cid3)
    check("E 手機 customer_create after 含 tier", d3 and d3["after"].get("tier") == "dealer" and d3.get("via") == "mobile")
    ap = sc.get("/admin/audit/?target_type=customers")
    check("E /admin/audit 200", ap.status_code in (200, 403))

    # F. 編輯不帶 tier → 維持
    r = sc.post(f"/customers/{cid2}/edit", data={"name": "代理B", "note": "改備註"}, follow_redirects=True)
    check("F 編輯不帶 tier 欄 → 維持 agent", cust_by_name("代理B")[1] == "agent")
    d = audit_last("customer_update", cid2)
    check("F 該次 audit 只記備註、不含 tier", d and "tier" not in (d.get("after") or {}) and "note" in (d.get("after") or {}))

    print(f"\nPASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  -", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

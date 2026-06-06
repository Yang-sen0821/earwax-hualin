# blueprints/customers.py
# 消費者管理 blueprint（customers_bp）
# 路由：/customers（列表）、/customers/new（新增）

import datetime
from flask import (
    Blueprint, render_template, request,
    redirect, url_for, flash, jsonify
)
from db import db, Customer
from auth import login_required

customers_bp = Blueprint("customers", __name__, url_prefix="/customers")

PAGE_SIZE = 20


# ──────────────────────────────────────────────
# 列表
# ──────────────────────────────────────────────

@customers_bp.route("/")
@login_required
def list_customers():
    """消費者列表，支援姓名/電話關鍵字搜尋與分頁。"""
    q         = request.args.get("q", "").strip()
    is_member = request.args.get("is_member", "").strip()
    page      = int(request.args.get("page", 1))

    query = Customer.query
    if q:
        query = query.filter(
            db.or_(
                Customer.name.ilike(f"%{q}%"),
                Customer.phone.ilike(f"%{q}%"),
            )
        )
    # 身份篩選（模板 customers/list.html 送 is_member=1/0）
    if is_member == "1":
        query = query.filter(Customer.is_member.is_(True))
    elif is_member == "0":
        query = query.filter(Customer.is_member.is_(False))
    total = query.count()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page  = max(1, min(page, pages))

    customers = (
        query.order_by(Customer.id.desc())
             .offset((page - 1) * PAGE_SIZE)
             .limit(PAGE_SIZE)
             .all()
    )

    return render_template(
        "customers/list.html",
        customers=customers,
        q=q,
        page=page,
        pages=pages,
        total=total,
    )


# ──────────────────────────────────────────────
# 新增
# ──────────────────────────────────────────────

@customers_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_customer():
    """新增消費者。POST 時建立記錄後回列表。"""
    if request.method == "POST":
        name      = request.form.get("name", "").strip()
        phone     = request.form.get("phone", "").strip()
        is_member = request.form.get("is_member") == "1"
        note      = request.form.get("note", "").strip()

        if not name:
            flash("姓名為必填欄位。")
            return render_template("customers/new.html",
                                   form=request.form)

        customer = Customer(
            name=name,
            phone=phone,
            is_member=is_member,
            note=note,
            created_at=datetime.datetime.utcnow(),
        )
        db.session.add(customer)
        db.session.commit()
        flash(f"消費者「{customer.name}」已建立。")
        return redirect(url_for("customers.list_customers"))

    return render_template("customers/new.html", form={})


# ──────────────────────────────────────────────
# AJAX 搜尋（供 4 步表單即時查消費者）
# ──────────────────────────────────────────────

@customers_bp.route("/search")
@login_required
def search_customers():
    """回傳 JSON，供施作登錄步驟 2 即時搜尋消費者。
    參數：q（姓名或電話片段），最多回傳 10 筆。
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    results = (
        Customer.query
        .filter(
            db.or_(
                Customer.name.ilike(f"%{q}%"),
                Customer.phone.ilike(f"%{q}%"),
            )
        )
        .order_by(Customer.id.desc())
        .limit(10)
        .all()
    )

    data = [
        {
            "id":        c.id,
            "name":      c.name,
            "phone":     c.phone,
            "is_member": c.is_member,
        }
        for c in results
    ]
    return jsonify(data)

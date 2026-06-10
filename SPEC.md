# 樺林美學 E•ar Wax — 系統建置規格（凍結契約）

> 本檔是所有建置代理的**唯一契約**。表名、欄位名、路由名、模板名一律以此為準，不得自創。
> 技術棧比照泰安：Flask + SQLAlchemy + Supabase(PostgreSQL)，blueprints 分模組。
> 參照原始碼：`E:\AI-Wiki\Projects\taian_webapp\`（app.py / db.py / auth.py / blueprints / templates）。

## 0. 設計定案（來自森哥逐題確認；2026-06-06 客戶回饋大修）
- **1 個代理產品**：愛啪啪（外泌體服務）。本系統＝愛啪啪南科店單一營運主體。
- **⚠️〔2026-06-10 已修訂，見 §8〕面膜（Flora Court）**：原定「完全移出本系統」，森哥
  2026-06-10 改為**獨立代理產品**——與愛啪啪**平行**的獨立模組（獨立頁面/成本/庫存/ROI，
  獨立核算，**不與愛啪啪混帳**）。本點原「移出」決策自即日失效，**一律以 §8 面膜模組為準**。
  獨立核算原則不變：面膜數據**不計入愛啪啪損益與 §4 全店總覽**。
- **真耗材**分兩類：
  - **消耗品**（管庫存+補貨+每次施作扣用）：針筒、耳塞、安瓶、洗卸品、洗臉巾、防曬
  - **儀器設備**（固定資產，只記清單不逐次扣）：導入儀、氣壓儀器、頭皮臉部偵測儀、台車、破壁機
- **兩套獨立庫存**：①美容券庫存（券，張）②消耗品庫存（逐項，件/支/片）。
- **會員/非會員**：
  - 非會員 = 跟店家買券（扣店家券庫存）→ 利潤高（基準淨利 1360）
  - 會員 = 自帶券（不扣店家券庫存）→ 利潤低（基準淨利 480）
- **售價可折扣**：非會員可給特殊折扣，第 4 步「實收金額」可改，淨利自動重算。
- **損益**：愛啪啪獨立回本 + 全店總覽＝**愛啪啪單一主體**（不含面膜或任何其他主體）。
- **部署**：比照泰安，Render + **共用 MUSEN-SAAS Supabase**，所有表關進獨立
  `earwax` schema 與泰安 public 隔離（2026-06-06 森哥定案，不另開新專案）。

## 1. 淨利公式（權威，後端計算）
所有成本項相同（變動成本 2620/張），會員與非會員差別只在售價：

```
基準售價：非會員 3980 / 會員 3100
變動成本：2620（每張，券+面膜+手作費分潤等，已含 Excel 全部成本）
店主股東回收：+10（每張；店主為 10 位股東之一，拿回自己那份）

實收金額 actual_price 預設 = 基準售價 × 張數，員工可改（折扣）
單客淨利 profit = 實收金額 − 變動成本 × 張數 + 店主股東回收 × 張數
```
驗證：非會員 1 張無折扣 = 3980−2620+10 = **1370**（Excel 1360 + 股東回收 10）✓
會員 1 張 = 3100−2620+10 = **490**（Excel 480 + 10）✓
> 上述數字全部做成「參數設定」可改（params 表），老闆後台可調。
> 假設：張數對金額/淨利為線性（買 N 張 = 基準 ×N，再套折扣）。此假設若需調整，改 params 即可。

## 2. 資料模型（db.py，SQLAlchemy，表名/欄位名固定）

### User（登入帳號，**新增：權限分級**）
- `users`：id, username(唯一), password_hash, name, role(`staff`|`owner`), created_at
- owner 可看後台儀表板；staff 只能做 4 步登錄與查庫存。

### Product（代理產品參數）
- `products`：id, name(愛啪啪), total_cost(回本目標,142000), active, created_at

### Params（淨利/成本參數，可由 owner 後台改）
- `params`：id, product_id, member_base_price(3100), member_base_profit(480),
  nonmember_base_price(3980), nonmember_base_profit(1360),
  variable_cost_per(2620), owner_shareholder_return(10)

### Customer（消費者）
- `customers`：id, name, phone, is_member(Boolean, 預設非會員=False), note, created_at

### VoucherInventory（美容券庫存，張）
- `voucher_inventory`：id, product_id, qty_on_hand(Integer)

### VoucherPurchase（券補貨記錄 → 增券庫存）
- `voucher_purchases`：id, date, qty, unit_cost, total_cost, note, created_at

### Consumable（耗材主檔：消耗品 + 儀器設備）
- `consumables`：id, name, category(`consumable`|`equipment`), qty_on_hand(Integer),
  unit_cost(Float,可後補), note, created_at
- `category='consumable'` → 管庫存、可補貨、施作可扣用（針筒/安瓶/洗卸品/洗臉巾/耳塞/防曬）
- `category='equipment'` → 固定資產，只記清單，**不逐次扣用**（導入儀/氣壓儀器/偵測儀/台車/破壁機）

### ConsumablePurchase（消耗品補貨記錄 → 增庫存）
- `consumable_purchases`：id, date, consumable_id, qty, unit_cost, total_cost, note, created_at

### ServiceRecord（施作記錄＝4 步輸出，核心表）
- `service_records`：id, date, customer_id, product_id, member_type(`member`|`nonmember`),
  voucher_count(張數,Integer), actual_price(實收), profit(算出), note, created_at
- **不再有 mask_used/gift_mask**；耗材改用下方 ServiceConsumable 明細表（一筆施作可多項耗材）。
- **建立時副作用**（後端交易，全程同一 transaction，失敗 rollback）：
  - 非會員 → 扣 voucher_inventory.qty_on_hand −= voucher_count（不足要擋並提示）
  - 會員 → 不扣券庫存
  - 逐筆 ServiceConsumable → 扣 consumables.qty_on_hand −= qty（僅 category='consumable'；不足要擋）
  - profit 依第 1 節公式後端計算（不信前端）

### ServiceConsumable（施作耗材明細，新增；一施作 N 耗材）
- `service_consumables`：id, service_record_id(FK earwax.service_records.id),
  consumable_id(FK earwax.consumables.id), qty(Integer), is_gift(Boolean,該項是否為贈品), created_at
- 第 4 步勾選的每項耗材一列；is_gift=True 代表這項是免費贈送（仍扣庫存）。

## 3. 路由 / Blueprints（blueprints/，名稱固定）
- `auth`：/login /logout（session 存 user dict 含 role）
- `service`（service_bp）：/service/new 4 步表單、/service 列表
  - 4 步：①選代理產品(愛啪啪) ②選/建消費者 ③會員/非會員 ④張數+實收(可折扣)
    +耗材明細(可多項消耗品，各填數量、各自可標贈品)
- `customers`（customers_bp）：/customers 列表、/customers/new、有則套用無則建立（表單可即時新增）
- `inventory`（inventory_bp）：/inventory 顯示券庫存 + 消耗品庫存(逐項) + 儀器設備清單
- `purchases`（purchases_bp）：/purchases/voucher 補券、/purchases/consumable 補消耗品(選品項+數量)
- `dashboard`（dashboard_bp）：/（首頁，**僅 owner**）儀表板
- 權限：staff 進 /dashboard 導回 /service/new；裝飾器 `@owner_required` / `@login_required`

## 4. 儀表板指標（dashboard，owner 限定）
- 愛啪啪回本：累計淨利、剩餘回本金額、回本進度%（累計淨利/142000）
- 全店總覽：＝愛啪啪單一主體損益（累計淨利、累計營收、客數）。**不得含面膜或任何其他主體。**
- 會員/非會員：人數、占比、各自累計淨利（比照 Excel 總覽分頁）
- 明星耗材：依 ServiceConsumable 加總各消耗品用量（含贈品），排名（針筒/安瓶/…）
- 回本週期：依日期區間平均淨利/客 × 來客頻率，推估還需多久回本（用 service_records 日期跨度估算）
- 期間篩選：start/end（比照泰安 compute_dashboard）

## 5. 部署 / 設定檔
- `config.py`：SECRET_KEY 從環境變數；**DATABASE_URL 一律從 os.environ 讀，不得寫死密碼**（泰安壞示範不照抄）
- **Schema 隔離**：`db = SQLAlchemy(metadata=MetaData(schema="earwax"))`；所有 FK 字串
  schema 限定（`earwax.xxx`）；init_db 與 app 啟動先 `CREATE SCHEMA IF NOT EXISTS earwax`
- `requirements.txt`、`Procfile`、`render.yaml`、`.env.example`、`.gitignore`（比照泰安）
- `init_db.py`：建表 + 種子：
  - 帳號：owner（老闆，可看後台）、staff（員工）各一組（密碼 hash；明碼寫進 .env.example 供森哥改）
  - 愛啪啪 product + params（用第 1 節數值）
  - voucher_inventory 初始 0
  - consumables 種子（qty_on_hand 初始 0、unit_cost 0 待後補）：
    - category='consumable'：針筒、耳塞、安瓶、洗卸品、洗臉巾、防曬
    - category='equipment'：導入儀、氣壓儀器、頭皮臉部偵測儀、台車、破壁機
  - **不得 seed 面膜（已移出本系統）**

## 6. 模板（templates/，Bootstrap，比照泰安 base.html）
base.html, login.html, index.html(dashboard), service/new.html(4步,耗材可多項), service/list.html,
customers/list.html, customers/new.html, inventory/index.html(券+消耗品+儀器),
purchases/voucher.html, purchases/consumable.html

## 7. 驗收標準
- `python -c "import app"` 不報錯
- init_db.py 可建表 + 種子（對空 DB）
- 登入 owner 看得到儀表板；staff 看不到後台只能登錄
- 4 步登錄一筆非會員 → 券庫存 −張數、所選消耗品 −用量、profit 正確
- README/啟動說明.md 寫清楚：環境變數、建 Supabase、init_db、跑起來步驟

## 8. 面膜模組（Flora Court，2026-06-10 新增；推翻 §0「面膜移出」決策）

> 森哥 2026-06-10 定案：面膜改作**獨立代理產品**，與愛啪啪**平行**掛在同一 earwax 系統／
> 同一 Supabase `earwax` schema，新增獨立頁面與資料表，**獨立核算、不與愛啪啪混帳**。
> 本節推翻 §0 第二點「面膜完全移出」；該決策即日失效，以本節為準。

### 8.1 範圍與權限
- 面膜＝獨立主體：自己的銷售/成本/庫存/ROI，**不計入愛啪啪損益、不計入 §4 全店總覽**（§4 不變，全店仍＝愛啪啪單一主體）。
- 權限比照愛啪啪：**staff 可 KEY 銷售單**；**owner 可看 ROI 後台與成本頁**。
- 新增 blueprint `mask`（mask_bp），與既有 blueprints 平行，不動既有愛啪啪程式。

### 8.2 銷售 KEY 單（5 項固定）
每筆銷售一列，5 個品項，每項都有「數量欄」可 KEY；只有「一般」項有「金額欄＝該筆實收總額(可折扣)」：

| # | 品項 | 數量 | 金額(實收) | 性質 |
|---|------|------|-----------|------|
| 1 | 一般盒裝 general_box | ✅ | ✅ | 販售，計收入 |
| 2 | 公關盒裝 pr_box | ✅ | — | 贈送，無收入，扣庫存 |
| 3 | 一般單片 general_piece | ✅ | ✅ | 販售，計收入 |
| 4 | 公關單片 pr_piece | ✅ | — | 贈送，無收入，扣庫存 |
| 5 | 包裝袋 bag | ✅ | — | 耗材，扣庫存 |

- 該筆收入 = 一般盒裝金額 + 一般單片金額（公關、包裝袋不計收入）。
- 送出副作用（同一 transaction，不足要擋並提示）：5 項數量各自扣對應的 5 個獨立庫存。

### 8.3 成本頁（可增減品項 + 輸入金額）
- 自由新增/刪除成本品項，每項一個名稱 + 一個金額（無固定品項清單）。
- **面膜已投入成本總額 = Σ 所有成本品項金額**（供 ROI 用）。

### 8.4 庫存（5 個獨立庫存）
- 一般盒、公關盒、一般片、公關片、包裝材，**各自獨立計算，互不換算**。
- 庫存頁顯示 5 項當前數量 + 補貨輸入（增加對應庫存）；KEY 銷售單時各自扣減。
- ⏳ 初始品項細節（單位/初始數量/是否有單位成本）以森哥下午提供的庫存清單為準（僅 seed 用，不改本模型）。

### 8.5 ROI / 面膜獨立顯示頁（owner）
- 累積收入 = Σ(一般盒裝金額 + 一般單片金額)
- 已投入成本 = §8.3 成本頁總額
- 面膜淨利 = 累積收入 − 已投入成本；回本進度 = 累積收入 / 已投入成本
- 統計：各項銷量（含公關贈送數）、包裝袋用量；期間篩選 start/end（比照 §4）。

### 8.6 資料模型（earwax schema，新增表，不動既有表）
- `mask_sales`：id, date, general_box_qty, general_box_amount, pr_box_qty,
  general_piece_qty, general_piece_amount, pr_piece_qty, bag_qty, note, created_at
- `mask_cost_items`：id, name, amount(Float), note, created_at
- `mask_inventory`：id, item_key(`general_box`|`pr_box`|`general_piece`|`pr_piece`|`bag`),
  name, qty_on_hand(Integer), created_at（種子 5 列、初始 0）
- `mask_purchases`：id, date, item_key, qty(Integer), note, created_at（補貨 → 增 mask_inventory）

### 8.7 路由 / 模板
- `mask_bp`：
  - `/mask`（owner）面膜 ROI 儀表板（獨立顯示頁）
  - `/mask/sales/new`（staff）銷售 KEY 單；`/mask/sales` 列表
  - `/mask/cost`（owner）成本頁（增減品項+金額）
  - `/mask/inventory` 庫存頁 + 補貨
- 模板：mask/dashboard.html, mask/sales_new.html, mask/sales_list.html, mask/cost.html, mask/inventory.html
- base.html 導覽列新增「面膜」入口（staff 見 KEY 單；owner 多見 ROI/成本）。

### 8.8 驗收
- init_db 追加 mask_inventory 5 列種子（qty 0）；不影響既有愛啪啪表與泰安 public schema。
- staff 可開 /mask/sales/new KEY 一筆 → 5 庫存各扣、該筆收入正確；owner /mask 看到回本=收入−成本總額。
- 面膜數據完全不污染愛啪啪 §4 全店總覽。

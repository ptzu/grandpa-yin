# Alembic migrations

管理 **`grandpa_yin.*` 產品層 schema**。共用層 `public.*`（accounts / transactions /
linked_identities）由 Altide 的 `altide-landing-page/supabase/schema.sql` 管理，本目錄
一律不碰——過濾邏輯在 `env.py` 的 `include_object()`。

上線步驟、baseline 標記、全新資料庫的初始化順序，見[部署文件第六章](../docs/DEPLOYMENT.md)。

## 檔案

| | |
|---|---|
| `env.py` | 連線與範圍設定：從 `DATABASE_URL` 取連線字串、限定 `grandpa_yin` schema、version 表放在同 schema |
| `versions/` | migration 腳本 |
| `script.py.mako` | 新 migration 的樣板 |
| `../alembic.ini` | 設定檔（放在專案根目錄，`alembic` 指令預設從當前目錄找它） |

## 命名慣例

檔名格式為 `<時間戳>_<描述>.py`，例如 `20260809235858_add_standalone_identity_and_wallet.py`。
由 `alembic.ini` 的 `file_template` 自動產生，時間戳開頭讓 `versions/` 天然依時序排列。

**revision id 不在檔名裡**（`a6e5ccf71d56` 這類），要用 id 找檔案：

```bash
grep -rl a7b8c9d0e1f2 alembic/versions/     # id → 檔案
alembic history                             # 看完整的鏈與 id
```

> 現有三支的時間戳是導入此慣例時回填的：baseline 取自檔內的 `Create Date`，另外兩支取自
> git 首次 commit 時間。後兩支同屬一個 commit，秒數差 1 秒以保留 `down_revision` 的順序。
> 重新命名不影響任何行為——Alembic 認的是檔案裡的 `revision`，不是檔名。

## 常用指令

```bash
alembic current                              # 目前資料庫在哪個版本
alembic history --verbose                    # 完整鏈
alembic upgrade head                         # 套用到最新（部署時由 railway.json 自動執行）
alembic downgrade -1                         # 回退一步
alembic check                                # 檢查 model 與 migration 有無落差

# 改完 src/models/*.py 後產生新 migration（務必人工檢視產出）
alembic revision --autogenerate -m "描述"
```

所有指令都需要 `DATABASE_URL`（從 `.env` 或環境變數取得）。

## 注意事項

- **`--autogenerate` 的產出一定要人工檢視**：它偵測不到欄位改名（會出成 drop + add，資料會沒）、
  CHECK 約束、RLS policy、trigger。
- **本地與線上的差異**：`test/setup_test_db.py` 用 `create_all` 建表再 `stamp` 到 head，
  所以本地不含線上由 `schema.sql` 建立的 CHECK / RLS / trigger。
- **兩種部署模式都要能跑**：`standalone` 沒有 `public.accounts`，跨 schema 外鍵只能在
  `platform` 環境補。整合用的 migration（`a7b8c9d0e1f2`）因此寫成冪等且會偵測環境後跳過。
  新增 migration 時若碰到 `public.*`，記得比照辦理。

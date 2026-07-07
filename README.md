# LINE Bot - 圖片彩色化服務

一個基於 LINE Bot API 的智能圖片彩色化服務，使用 Replicate AI 模型將黑白照片自動轉換為彩色照片。

## 🚀 功能特色

- **📸 圖片彩色化**: 使用 AI 技術將黑白照片自動彩色化
- **🤖 智能對話**: 支援文字對話和功能選單
- **⚡ 即時處理**: 背景非同步處理，不阻塞用戶體驗
- **🔧 模組化設計**: 易於擴展新功能
- **👤 用戶狀態管理**: 支援多用戶同時使用，狀態持久化儲存

## 📋 系統需求

- Python 3.7+
- LINE Developers 帳號
- Replicate API 帳號
- PostgreSQL 資料庫 (Supabase 推薦)
- ngrok (本地測試用)

## 🛠️ 安裝與設定

### 1. 克隆專案

```bash
git clone <repository-url>
cd LineBot
```

### 2. 安裝依賴

```bash
pip install -r requirements.txt
```

### 3. 環境變數設定

複製 `env_example.txt` 為 `.env` 並填入必要的 API 金鑰：

```bash
cp env_example.txt .env
```

編輯 `.env` 檔案：

```env
# LINE Bot 設定
CHANNEL_ACCESS_TOKEN=your_line_channel_access_token_here
CHANNEL_SECRET=your_line_channel_secret_here

# Replicate API 設定
REPLICATE_API_TOKEN=your_replicate_api_token_here

# 資料庫設定 (Supabase 推薦)
DATABASE_URL=postgresql://postgres:[YOUR-PASSWORD]@db.[PROJECT-ID].supabase.co:5432/postgres

# 應用程式設定
PORT=5000
```

### 4. 取得 API 金鑰

#### LINE Bot 設定
1. 前往 [LINE Developers Console](https://developers.line.biz/)
2. 建立新的 Messaging API 頻道
3. 取得 Channel Access Token 和 Channel Secret

#### Replicate API 設定
1. 前往 [Replicate](https://replicate.com/)
2. 註冊帳號並取得 API Token
3. 確保帳號有足夠的點數進行圖片處理

#### 資料庫設定 (Supabase 推薦)
1. 前往 [Supabase](https://supabase.com/)
2. 建立新專案
3. 在專案設定中取得資料庫連線字串
4. 複製連線字串到 `.env` 檔案的 `DATABASE_URL`

## 🚀 啟動服務

### 資料庫初始化

資料庫 schema 由 Supabase migrations 管理（見 `altide-landing-page/supabase/migrations/`），無需在本專案建表。

輔助腳本：

```bash
# 手動新增會員 / 加點（互動式）
python scripts/add_member.py

# 清理超過 24 小時的舊用戶狀態
python scripts/cleanup_user_states.py 24
```

### 本地開發環境

#### 方法一：使用自動啟動腳本（推薦）

```bash
cd test
python start_local_server.py
```

此腳本會自動：
- 檢查環境變數和依賴
- 啟動 Flask 應用程式
- 啟動 ngrok 隧道
- 提供 webhook URL 設定指引

#### 方法二：手動啟動

1. **啟動 Flask 應用程式**
```bash
python app.py
```

2. **啟動 ngrok 隧道**（新終端機）
```bash
ngrok http 5000
```

3. **設定 LINE Webhook**
   - 複製 ngrok 提供的 HTTPS URL
   - 在 LINE Developers Console 中設定 Webhook URL
   - 格式：`https://your-ngrok-url.ngrok.io/webhook`

### 生產環境部署

#### Railway 部署

1. 在 [Railway](https://railway.app/) 建立專案並連結此 Git repository
2. 在 Railway 專案的 Variables 設定環境變數：
   - `CHANNEL_ACCESS_TOKEN`
   - `CHANNEL_SECRET`
   - `REPLICATE_API_TOKEN`
   - `DATABASE_URL`（Supabase 連線字串）
   - `COLORIZE_COST`、`EDIT_COST`、`WELCOME_POINTS`（可選）
3. Push 到 main 分支即自動部署（啟動指令見 `Procfile`）
4. 將 Railway 提供的網域設定為 LINE Webhook URL：`https://your-app.up.railway.app/webhook`

## 📱 使用方式

### 基本指令

- `!功能` - 開啟功能選單
- `圖片彩色化` - 啟動圖片彩色化功能
- `使用說明` - 查看詳細使用說明

### 圖片彩色化流程

1. 輸入 `!功能` 開啟選單
2. 選擇「📸 圖片彩色化」
3. 確認要進行彩色化處理
4. 上傳黑白照片
5. 等待 AI 處理完成
6. 接收彩色化結果

## 🏗️ 專案結構

```
grandpa-yin/
├── app.py                      # 主應用程式入口（webhook、初始化）
├── message_publisher.py        # 訊息發送器
├── user_state_manager.py       # 用戶狀態管理（bot_sessions）
├── features/                   # 功能模組
│   ├── base_feature.py         # 功能基礎類別
│   ├── feature_registry.py     # 功能註冊表（訊息路由）
│   ├── menu_feature.py         # 選單功能
│   ├── colorize_feature.py     # 圖片彩色化功能
│   ├── edit_feature.py         # 圖片編輯功能
│   └── member_feature.py       # 會員查詢功能
├── services/
│   └── member_service.py       # 會員服務層（點數、交易）
├── models/                     # SQLAlchemy 資料模型
│   ├── database.py             # 連線與 session 管理
│   ├── account.py              # 帳號（共用點數錢包）
│   ├── linked_identity.py      # 第三方身分綁定
│   ├── grandpa_yin_profile.py  # 長輩專屬設定
│   ├── bot_session.py          # 對話狀態
│   ├── transaction.py          # 點數交易記錄
│   └── usage_log.py            # 功能使用記錄
├── scripts/                    # 管理腳本
│   ├── add_member.py           # 手動新增會員
│   └── cleanup_user_states.py  # 清理舊狀態
├── test/                       # 本地測試
│   ├── start_local_server.py   # 本地測試啟動器（含 ngrok）
│   └── test_local.py           # 模擬 LINE 訊息測試
├── log/                        # 設計文件
├── requirements.txt            # Python 依賴
├── Procfile                    # 部署啟動指令（gunicorn）
└── env_example.txt             # 環境變數範例
```

## 🔧 開發指南

### 新增功能

1. 繼承 `BaseFeature` 類別
2. 實作必要的方法：
   - `name`: 功能名稱
   - `can_handle()`: 判斷是否能處理訊息
   - `handle_text()`: 處理文字訊息
   - `handle_image()`: 處理圖片訊息（可選）

3. 在 `app.py` 中註冊新功能

```python
# 在 init() 函數中
new_feature = NewFeature(line_bot_api, publisher, user_state_manager)
feature_registry.register(new_feature)
```

### 測試

執行本地測試：

```bash
cd test
python test_local.py
```

## 🐛 故障排除

### 常見問題

1. **環境變數未設定**
   - 確認 `.env` 檔案存在且包含所有必要的變數
   - 檢查變數名稱是否正確

2. **ngrok 連接失敗**
   - 確認 ngrok 已正確安裝
   - 檢查防火牆設定

3. **Replicate API 錯誤**
   - 確認 API Token 正確
   - 檢查帳號點數是否充足

4. **LINE Webhook 驗證失敗**
   - 確認 Webhook URL 格式正確
   - 檢查 Channel Secret 是否正確

### 日誌查看

應用程式會輸出詳細的日誌資訊，包括：
- 初始化狀態
- 訊息處理過程
- 錯誤訊息和堆疊追蹤

## 📄 授權

此專案採用 MIT 授權條款。

## 🤝 貢獻

歡迎提交 Issue 和 Pull Request！

## 📞 支援

如有問題，請建立 Issue 或聯繫開發團隊。


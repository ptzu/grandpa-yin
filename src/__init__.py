"""銀爺爺 LINE Bot 核心程式碼。

分層：
  core/      跨切面基礎設施（logging、錯誤追蹤、執行緒池），不含領域知識
  models/    資料層（SQLAlchemy model、連線管理）
  services/  對外部系統與領域狀態的封裝（LINE 發送、會員／點數、Storage、
             對話狀態、AccountBackend port）
  features/  功能模組，由 app.py 的 FeatureRegistry 路由

app.py 是 Flask entrypoint（webhook、初始化、訊息路由）。
"""

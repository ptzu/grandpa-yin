#!/usr/bin/env python3
"""
本地測試啟動腳本
自動啟動 Flask 應用程式和 ngrok 隧道
"""

import os
import sys
import subprocess
import time
import requests
import json
from dotenv import load_dotenv

# 載入環境變數
load_dotenv()

class LocalTestStarter:
    def __init__(self):
        self.flask_process = None
        self.ngrok_process = None
        self.webhook_url = None
        
    def check_dependencies(self):
        """檢查必要的依賴"""
        print("🔍 檢查依賴...")
        
        # 檢查環境變數
        required_vars = ["CHANNEL_ACCESS_TOKEN", "CHANNEL_SECRET", "REPLICATE_API_TOKEN"]
        missing_vars = []
        
        for var in required_vars:
            if not os.getenv(var):
                missing_vars.append(var)
        
        if missing_vars:
            print(f"❌ 缺少環境變數: {', '.join(missing_vars)}")
            print("請檢查 .env 檔案是否正確設定")
            return False
        
        # 檢查 ngrok
        try:
            result = subprocess.run(["ngrok", "version"], capture_output=True, text=True)
            if result.returncode == 0:
                print("✅ ngrok 已安裝")
            else:
                print("❌ ngrok 未安裝或無法執行")
                print("請前往 https://ngrok.com/ 下載並安裝 ngrok")
                return False
        except FileNotFoundError:
            print("❌ ngrok 未安裝")
            print("請前往 https://ngrok.com/ 下載並安裝 ngrok")
            return False
        
        print("✅ 所有依賴檢查完成")
        return True
    
    def start_flask_app(self):
        """啟動 Flask 應用程式"""
        print("🚀 啟動 Flask 應用程式...")
        
        # 設定開發模式環境變數
        env = os.environ.copy()
        env["FLASK_DEBUG"] = "true"
        
        try:
            self.flask_process = subprocess.Popen(
                [sys.executable, "../app.py"],
                # 移除輸出重定向，讓 Flask 輸出直接顯示
                # stdout=subprocess.PIPE,
                # stderr=subprocess.PIPE,
                text=True,
                env=env
            )
            
            print(f"✅ Flask 進程已啟動 (PID: {self.flask_process.pid})")
            
            # 等待 Flask 啟動
            time.sleep(3)
            
            # 檢查 Flask 是否正常運行
            try:
                response = requests.get("http://localhost:5000/webhook", timeout=5)
                if response.status_code == 405:  # Method Not Allowed 是正常的，因為 webhook 只接受 POST
                    print("✅ Flask 應用程式已啟動")
                    return True
            except requests.exceptions.RequestException:
                pass
            
            print("❌ Flask 應用程式啟動失敗")
            return False
            
        except Exception as e:
            print(f"❌ 啟動 Flask 時發生錯誤: {e}")
            return False
    
    def start_ngrok(self):
        """啟動 ngrok 隧道"""
        ngrok_domain = os.getenv("NGROK_DOMAIN")
        cmd = ["ngrok", "http"]
        if ngrok_domain:
            cmd.append(f"--url={ngrok_domain}")
            print(f"🌐 啟動 ngrok 隧道（固定網域: {ngrok_domain}）...")
        else:
            print("🌐 啟動 ngrok 隧道（隨機網址；設定 NGROK_DOMAIN 可固定網址）...")
        cmd.append("5000")

        try:
            self.ngrok_process = subprocess.Popen(
                cmd,
                # 移除輸出重定向，讓 ngrok 輸出直接顯示
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            print(f"✅ ngrok 進程已啟動 (PID: {self.ngrok_process.pid})")
            
            # 等待 ngrok 啟動
            time.sleep(5)
            
            # 取得 ngrok 公開 URL
            try:
                response = requests.get("http://localhost:4040/api/tunnels", timeout=5)
                if response.status_code == 200:
                    tunnels = response.json()
                    if tunnels.get("tunnels"):
                        self.webhook_url = tunnels["tunnels"][0]["public_url"]
                        print(f"✅ ngrok 隧道已啟動: {self.webhook_url}")
                        return True
            except requests.exceptions.RequestException:
                pass
            
            print("❌ ngrok 隧道啟動失敗")
            return False
            
        except Exception as e:
            print(f"❌ 啟動 ngrok 時發生錯誤: {e}")
            return False
    
    def setup_line_webhook(self):
        """透過 LINE API 自動設定並驗證 Webhook URL"""
        if not self.webhook_url:
            print("❌ 沒有可用的 ngrok URL")
            return False

        webhook_endpoint = f"{self.webhook_url}/webhook"
        print(f"🔗 Webhook URL: {webhook_endpoint}")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.getenv('CHANNEL_ACCESS_TOKEN')}",
        }

        try:
            response = requests.put(
                "https://api.line.me/v2/bot/channel/webhook/endpoint",
                headers=headers,
                json={"endpoint": webhook_endpoint},
                timeout=10,
            )
            if response.status_code != 200:
                print(f"❌ 自動設定 webhook 失敗: {response.status_code} {response.text}")
                print("   請確認 CHANNEL_ACCESS_TOKEN 是否為測試 channel 的值")
                return False
            print("✅ Webhook URL 已自動設定到 LINE")
        except requests.exceptions.RequestException as e:
            print(f"❌ 設定 webhook 時發生連線錯誤: {e}")
            return False

        # 給 Flask 一點時間確保完全就緒後再測試
        time.sleep(1)
        try:
            test_response = requests.post(
                "https://api.line.me/v2/bot/channel/webhook/test",
                headers=headers,
                json={"endpoint": webhook_endpoint},
                timeout=15,
            )
            result = test_response.json()
            if result.get("success"):
                print(f"✅ Webhook 驗證成功（回應狀態碼 {result.get('statusCode')}）")
            else:
                print(f"⚠️  Webhook 驗證未通過: {result.get('reason', result)}")
                print("   （可能 Flask 剛啟動還沒就緒，先傳訊息測試看看，不行再手動重跑）")
        except requests.exceptions.RequestException as e:
            print(f"⚠️  無法驗證 webhook（不影響已設定的結果）: {e}")

        return True
    
    def run_test(self):
        """執行測試"""
        print("\n🧪 執行測試...")
        
        try:
            result = subprocess.run([sys.executable, "test_local.py"], text=True)
            return result.returncode == 0
        except Exception as e:
            print(f"❌ 執行測試時發生錯誤: {e}")
            return False
    
    def cleanup(self):
        """清理資源"""
        print("\n🧹 清理資源...")
        
        if self.flask_process:
            self.flask_process.terminate()
            print("✅ Flask 應用程式已停止")
        
        if self.ngrok_process:
            self.ngrok_process.terminate()
            print("✅ ngrok 隧道已停止")
    
    def start(self):
        """開始本地測試"""
        print("🤖 LINE Bot 本地測試啟動器")
        print("=" * 50)
        
        try:
            # 檢查依賴
            if not self.check_dependencies():
                return
            
            # 啟動 Flask
            if not self.start_flask_app():
                return
            
            # 啟動 ngrok
            if not self.start_ngrok():
                return
            
            # 設定 webhook
            if not self.setup_line_webhook():
                return
            
            print("\n🎉 本地測試環境已準備就緒!")
            print("現在你可以:")
            print("1. 用手機 LINE App 掃描 QR Code 加入你的 Bot")
            print("2. 執行 python test/test_local.py 進行本地測試")
            print("3. 修改程式碼時 Flask 會自動重載，無需手動重啟")
            print("4. 按 Ctrl+C 停止測試環境")
            
            # 保持運行
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\n⏹️  收到停止信號")
        
        except Exception as e:
            print(f"❌ 發生錯誤: {e}")
        
        finally:
            self.cleanup()

def main():
    starter = LocalTestStarter()
    starter.start()

if __name__ == "__main__":
    main()

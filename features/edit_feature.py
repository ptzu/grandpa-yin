import os
import base64
import requests
import replicate
from task_executor import submit_image_task
from .base_feature import BaseFeature, _UNSET
from linebot.models import TextSendMessage, ImageSendMessage


class EditFeature(BaseFeature):
    """圖片編輯功能處理器"""

    def __init__(self, line_bot_api, publisher, state_manager, member_service=None):
        super().__init__(line_bot_api, publisher, state_manager, member_service)
        self.replicate_model = "google/nano-banana"
        self.required_points = int(os.getenv("EDIT_COST", "5"))
    
    @property
    def name(self) -> str:
        return "edit"
    
    def can_handle(self, message: str, user_id: str, user_state=_UNSET) -> bool:
        """判斷是否能處理此訊息"""
        # 處理圖片編輯相關的訊息
        if message == "圖片編輯":
            return True

        # 檢查是否為全局命令（這些命令不應該被 edit 攔截）
        if self._is_global_command(message):
            return False

        # 檢查用戶是否在圖片編輯狀態中
        state = self.resolve_user_state(user_id, user_state)
        if state and state.get("feature") == self.name:
            return True

        return False
    
    def _is_global_command(self, message: str) -> bool:
        """檢查是否為全局命令"""
        message = message.strip()
        global_commands = [
            "點數", "點數查詢", "查看點數", "查詢點數",
            "歷史", "交易記錄", "記錄",
            "會員", "會員資訊",
            "!功能", "功能", "！功能", "使用說明", "其他功能"
        ]
        if message in global_commands:
            return True
        if "點數" in message and ("查詢" in message or "查看" in message):
            return True
        return False
    
    def handle_text(self, event: dict) -> dict:
        """處理文字訊息"""
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message = self.get_message_text(event)
        user_name = self.get_user_name(user_id)
        
        try:
            if message == "圖片編輯":
                return self._handle_edit_request(reply_token, user_name, user_id, event)
            
            # 檢查用戶是否在等待編輯描述狀態
            if self.is_user_in_state(user_id, "waiting_description"):
                return self._handle_description_input(reply_token, user_name, user_id, message, event)
                
        except Exception as e:
            print(f"❌ EditFeature handle_text error: {str(e)}")
            import traceback
            traceback.print_exc()
        
        return None
    
    def can_handle_image(self, user_id: str) -> bool:
        """在等待圖片狀態時才處理圖片"""
        return self.is_user_in_state(user_id, "waiting_image")

    def handle_image(self, event: dict) -> dict:
        """處理圖片訊息"""
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message_id = self.get_message_id(event)
        user_name = self.get_user_name(user_id)
        
        print(f"收到圖片訊息，用戶 ID：{user_id}")
        
        # 檢查用戶是否在等待圖片狀態
        if not self.is_user_in_state(user_id, "waiting_image"):
            # 用戶沒有確認圖片編輯，靜默處理，不發送任何回覆
            print(f"用戶 {user_id} 上傳圖片但未確認圖片編輯功能，靜默處理")
            return None

        # 會員系統不可用時拒絕服務，避免免費放送處理額度
        if not self.member_service:
            self.clear_user_state(user_id)
            return self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text="⚠️ 系統維護中，功能暫時無法使用，請稍後再試 🙏"),
                user_id,
                event
            )

        try:
            # 1. 從 LINE 下載圖片並暫存
            message_content = self.line_bot_api.get_message_content(message_id)
            image_bytes = b''.join(chunk for chunk in message_content.iter_content())
            
            # 2. 設定狀態為等待編輯描述，同時保存圖片數據
            self.set_user_state(user_id, "waiting_description", {
                "image_data": base64.b64encode(image_bytes).decode('utf-8')
            })
            
            # 3. 回覆用戶已收到圖片，請輸入編輯描述
            result = self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text=f"{user_name}，我已經收到您的圖片了！📷✨\n\n請告訴我您希望如何編輯這張圖片？例如：\n• 將背景改成海灘\n• 把天空變成夕陽\n• 添加彩虹效果\n• 讓人物穿上紅色衣服\n\n請輸入您的編輯描述："),
                user_id,
                event  # 傳遞 event 以支援群組聊天
            )
            return result

        except Exception as e:
            # 發生錯誤時清除狀態
            self.clear_user_state(user_id)
            
            result = self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text=f"處理圖片時發生錯誤: {str(e)}"),
                user_id,
                event  # 傳遞 event 以支援群組聊天
            )
            return result
        
        return None
    
    def _handle_edit_request(self, reply_token: str, user_name: str, user_id: str, event: dict) -> dict:
        """處理圖片編輯請求"""
        # 會員系統不可用時拒絕服務
        if not self.member_service:
            return self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text="⚠️ 系統維護中，功能暫時無法使用，請稍後再試 🙏"),
                user_id,
                event
            )

        # 檢查點數
        member = self.member_service.get_or_create_member(user_id, user_name)
        if member['points'] < self.required_points:
            result = self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(
                    text=f"❌ 點數不足！\n\n💎 目前點數：{member['points']} 點\n💰 需要點數：{self.required_points} 點\n\n請輸入「點數」查看詳細資訊"
                ),
                user_id,
                event
            )
            return result
        
        # 設定用戶狀態為等待圖片
        self.set_user_state(user_id, "waiting_image")
        
        result = self.publisher.process_reply_message(
            reply_token,
            TextSendMessage(
                text=f"{user_name} 你好！✨\n🎨 圖片編輯功能\n\n💎 此功能會消耗 {self.required_points} 點點數，讓您的圖片煥然一新！\n\n請先上傳一張您想要編輯的圖片，然後我會請您描述想要的編輯效果 🖼️"
            ),
            user_id,
            event  # 傳遞 event 以支援群組聊天
        )
        return result
    
    def _handle_description_input(self, reply_token: str, user_name: str, user_id: str, description: str, event: dict) -> dict:
        """處理編輯描述輸入"""
        try:
            # 獲取暫存的圖片數據
            user_state = self.get_user_state(user_id)
            image_data = user_state.get("data", {}).get("image_data") if user_state else None
            
            if not image_data:
                self.clear_user_state(user_id)
                return self.publisher.process_reply_message(
                    reply_token,
                    TextSendMessage(text="找不到您上傳的圖片，請重新開始圖片編輯流程。"),
                    user_id,
                    event  # 傳遞 event 以支援群組聊天
                )
            
            # 設定狀態為正在處理，保留圖片數據和描述
            self.set_user_state(user_id, "processing", {
                "image_data": image_data,
                "description": description
            })
            
            # 1. 先回覆用戶已收到描述
            result = self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text=f"{user_name}，我已經收到您的編輯需求！🎨\n\n編輯描述：「{description}」\n\n正在為您精心處理中，請稍候片刻 ✨"),
                user_id,
                event  # 傳遞 event 以支援群組聊天
            )
            if result:  # 如果回傳錯誤 JSON
                return result
            
            # 2. 發送載入動畫
            try:
                self._start_loading_animation(user_id)
            except Exception as e:
                print(f"發送載入動畫失敗: {str(e)}")

            # 3. 在背景執行圖片編輯處理
            def process_image_async():
                try:
                    # 重新獲取狀態以確保數據完整
                    current_state = self.get_user_state(user_id)
                    if not current_state:
                        print(f"用戶 {user_id} 狀態已清除，停止處理")
                        return

                    image_data = current_state.get("data", {}).get("image_data")
                    description = current_state.get("data", {}).get("description")

                    if not image_data or not description:
                        self.publisher.process_push_message(
                            user_id,
                            TextSendMessage(text="處理過程中遺失了圖片或描述資料，請重新開始。"),
                            event  # 傳遞 event 以支援群組聊天
                        )
                        return

                    # 先扣點，扣不到就不處理（避免先服務後扣點被免費使用）
                    if not self.member_service.deduct_points(
                        user_id,
                        self.required_points,
                        f"圖片編輯：{description[:20]}",
                        feature_type='edit',
                    ):
                        self.publisher.process_push_message(
                            user_id,
                            TextSendMessage(text="❌ 點數不足或扣點失敗，本次未進行處理。\n請輸入「點數」查看剩餘點數。"),
                            event
                        )
                        return

                    try:
                        # 將 base64 轉回 bytes 並呼叫 Replicate API 處理
                        image_bytes = base64.b64decode(image_data)
                        output_url = self._edit_image(image_bytes, description)
                    except Exception as e:
                        # 處理失敗 → 退點並留下 failed 稽核記錄
                        print(f"❌ 圖片編輯處理失敗，退還點數: {str(e)}")
                        self.member_service.refund_points(
                            user_id, self.required_points,
                            feature_type='edit', reason=str(e)
                        )
                        self.publisher.process_push_message(
                            user_id,
                            TextSendMessage(text="處理圖片時發生錯誤，點數已退還，請稍後再試 🙏"),
                            event
                        )
                        return

                    # 回傳編輯後的圖片（載入動畫會自動停止）
                    self.publisher.process_push_message(
                        user_id,
                        ImageSendMessage(
                            original_content_url=output_url,
                            preview_image_url=output_url
                        ),
                        event  # 傳遞 event 以支援群組聊天
                    )
                finally:
                    # 處理完成後清除用戶狀態
                    self.clear_user_state(user_id)
                    print(f"用戶 {user_id} 圖片編輯處理完成，狀態已重置")

            # 提交到有界執行緒池；容量滿時優雅降級
            if not submit_image_task(process_image_async):
                self.clear_user_state(user_id)
                self.publisher.process_push_message(
                    user_id,
                    TextSendMessage(text="目前使用人數較多，請稍後再試 🙏"),
                    event
                )

        except Exception as e:
            # 發生錯誤時也要清除狀態
            self.clear_user_state(user_id)
            
            result = self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text=f"發生錯誤: {str(e)}"),
                user_id,
                event  # 傳遞 event 以支援群組聊天
            )
            return result
        
        return None
    
    def _start_loading_animation(self, user_id: str):
        """開始載入動畫"""
        try:
            # 使用 LINE Bot API 的載入動畫功能
            url = "https://api.line.me/v2/bot/chat/loading/start"
            
            # 設定 headers
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}'
            }
            
            # 設定載入動畫參數（5-60秒）
            data = {
                "chatId": user_id,
                "loadingSeconds": 45  # 圖片編輯可能需要更長時間
            }
            
            # 發送請求（連線 3 秒、讀取 10 秒逾時，避免執行緒卡死）
            response = requests.post(url, headers=headers, json=data, timeout=(3, 10))
            
            if response.status_code == 200:
                print(f"載入動畫已啟動，用戶: {user_id}")
            else:
                print(f"載入動畫啟動失敗: {response.status_code} - {response.text}")
                
        except Exception as e:
            print(f"啟動載入動畫時發生錯誤: {str(e)}")
    
    def _edit_image(self, image_bytes: bytes, description: str) -> str:
        """呼叫 Replicate 圖片編輯 API"""
        try:
            print(f"🔍 開始處理圖片編輯...")
            print(f"📊 圖片大小: {len(image_bytes)} bytes")
            print(f"📝 編輯描述: {description}")
            
            # 將 bytes 轉換為 base64 格式
            image_b64 = base64.b64encode(image_bytes).decode('utf-8')
            image_data_url = f"data:image/jpeg;base64,{image_b64}"
            
            print(f"🤖 呼叫模型: {self.replicate_model}")
            print("📡 正在發送請求到 Replicate API...")
            
            # 使用 Replicate Python SDK 呼叫 google/nano-banana 模型
            # 根據官方範例使用正確的參數格式
            output = replicate.run(
                self.replicate_model,
                input={
                    "prompt": description,
                    "image_input": [image_data_url],  # 使用 image_input 而不是 image
                    "output_format": "jpg"
                }
            )
            
            print(f"✅ API 回應類型: {type(output)}")
            print(f"📄 API 回應內容: {output}")
            
            if output:
                # 處理 FileOutput 物件，獲取 URL 字串
                try:
                    # 嘗試不同的方式獲取 URL
                    if hasattr(output, 'url'):
                        if callable(getattr(output, 'url')):
                            result_url = output.url()
                            print(f"🎯 回傳 URL (使用 .url()): {result_url}")
                            return result_url
                        else:
                            result_url = output.url
                            print(f"🎯 回傳 URL (使用 .url 屬性): {result_url}")
                            return result_url
                    elif isinstance(output, str):
                        print(f"🎯 回傳字串 URL: {output}")
                        return output
                    elif isinstance(output, list) and len(output) > 0:
                        first_item = output[0]
                        if hasattr(first_item, 'url'):
                            if callable(getattr(first_item, 'url')):
                                result_url = first_item.url()
                            else:
                                result_url = first_item.url
                            print(f"🎯 回傳列表第一個元素的 URL: {result_url}")
                            return result_url
                        else:
                            print(f"🎯 回傳列表第一個元素 (轉字串): {str(first_item)}")
                            return str(first_item)
                    else:
                        # 嘗試轉換為字串
                        result_str = str(output)
                        print(f"🎯 回傳轉換後字串: {result_str}")
                        return result_str
                except Exception as url_error:
                    print(f"❌ 獲取 URL 失敗: {url_error}")
                    # 備用方案：轉換為字串
                    result_str = str(output)
                    print(f"🔄 備用方案，回傳字串: {result_str}")
                    return result_str
            else:
                print("❌ API 沒有回傳任何結果")
                raise Exception("API 沒有回傳結果")
                
        except Exception as e:
            print(f"❌ Replicate API 錯誤詳細信息: {str(e)}")
            print(f"❌ 錯誤類型: {type(e)}")
            
            if "Insufficient credit" in str(e):
                raise Exception("Replicate 點數不足，請前往 https://replicate.com/account/billing#billing 購買點數")
            elif "Model not found" in str(e) or "does not exist" in str(e):
                raise Exception("找不到 google/nano-banana 模型，請檢查模型名稱是否正確")
            elif "Invalid input" in str(e):
                raise Exception("輸入參數格式錯誤，請檢查圖片和描述格式")
            else:
                raise Exception(f"圖片編輯處理失敗: {str(e)}")

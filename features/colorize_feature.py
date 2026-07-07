import os
import base64
import requests
import replicate
from app_logger import get_logger
from task_executor import submit_image_task
from .base_feature import BaseFeature, _UNSET
from linebot.models import TextSendMessage, ImageSendMessage

logger = get_logger("colorize")


class ColorizeFeature(BaseFeature):
    """圖片彩色化功能處理器"""

    def __init__(self, line_bot_api, publisher, state_manager, member_service=None):
        super().__init__(line_bot_api, publisher, state_manager, member_service)
        self.replicate_model = "flux-kontext-apps/restore-image"
        self.required_points = int(os.getenv("COLORIZE_COST", "10"))
    
    @property
    def name(self) -> str:
        return "colorize"
    
    def can_handle(self, message: str, user_id: str, user_state=_UNSET) -> bool:
        """判斷是否能處理此訊息"""
        # 處理彩色化相關的訊息
        if message == "圖片彩色化":
            return True

        # 檢查是否為全局命令（這些命令不應該被 colorize 攔截）
        if self._is_global_command(message):
            return False

        # 檢查用戶是否在彩色化狀態中
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
            if message == "圖片彩色化":
                return self._handle_colorize_request(reply_token, user_name, user_id, event)
                
        except Exception:
            logger.exception("ColorizeFeature handle_text error")
        
        return None
    
    def can_handle_image(self, user_id: str) -> bool:
        """在等待圖片狀態時才處理圖片"""
        return self.is_user_in_state(user_id, "waiting")

    def handle_image(self, event: dict) -> dict:
        """處理圖片訊息"""
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message_id = self.get_message_id(event)
        user_name = self.get_user_name(user_id)
        
        logger.info(f"收到圖片訊息，用戶 ID：{user_id}")
        
        # 檢查用戶是否在等待彩色化狀態
        if not self.is_user_in_state(user_id, "waiting"):
            # 用戶沒有確認彩色化，靜默處理，不發送任何回覆
            logger.debug(f"用戶 {user_id} 上傳圖片但未確認彩色化功能，靜默處理")
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
            # 設定狀態為正在彩色化
            self.set_user_state(user_id, "processing")
            
            # 1. 從 LINE 下載圖片
            message_content = self.line_bot_api.get_message_content(message_id)
            image_bytes = b''.join(chunk for chunk in message_content.iter_content())

            # 2. 先回覆用戶已收到圖片
            result = self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text=f"{user_name}，我已經收到您的珍貴照片了！✨ 正在為您精心處理中，請稍候片刻 🌟"),
                user_id,
                event  # 傳遞 event 以支援群組聊天
            )
            if result:  # 如果回傳錯誤 JSON
                return result
            
            # 3. 發送載入動畫
            try:
                self._start_loading_animation(user_id)
            except Exception as e:
                logger.warning(f"發送載入動畫失敗: {str(e)}")

            # 4. 在背景執行彩色化處理
            def process_image_async():
                try:
                    # 先扣點，扣不到就不處理（避免先服務後扣點被免費使用）
                    if not self.member_service.deduct_points(
                        user_id,
                        self.required_points,
                        "彩色化圖片",
                        feature_type='colorize',
                    ):
                        self.publisher.process_push_message(
                            user_id,
                            TextSendMessage(text="❌ 點數不足或扣點失敗，本次未進行處理。\n請輸入「點數」查看剩餘點數。"),
                            event
                        )
                        return

                    try:
                        output_url = self._colorize_image(image_bytes)
                    except Exception as e:
                        # 處理失敗 → 退點並留下 failed 稽核記錄
                        logger.exception(f"彩色化處理失敗，退還點數: {user_id}")
                        self.member_service.refund_points(
                            user_id, self.required_points,
                            feature_type='colorize', reason=str(e)
                        )
                        self.publisher.process_push_message(
                            user_id,
                            TextSendMessage(text="處理圖片時發生錯誤，點數已退還，請稍後再試 🙏"),
                            event
                        )
                        return

                    # 回傳彩色圖片（載入動畫會自動停止）
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
                    logger.info(f"用戶 {user_id} 彩色化處理完成，狀態已重置")

            # 提交到有界執行緒池；容量滿時優雅降級
            if not submit_image_task(process_image_async):
                self.clear_user_state(user_id)
                self.publisher.process_push_message(
                    user_id,
                    TextSendMessage(text="目前使用人數較多，請稍後再試 🙏"),
                    event
                )

        except Exception:
            # 發生錯誤時也要清除狀態
            logger.exception(f"彩色化 handle_image 失敗: {user_id}")
            self.clear_user_state(user_id)

            result = self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text="處理圖片時發生錯誤，請稍後再試 🙏"),
                user_id,
                event  # 傳遞 event 以支援群組聊天
            )
            return result
        
        return None
    
    def _handle_colorize_request(self, reply_token: str, user_name: str, user_id: str, event: dict) -> dict:
        """處理彩色化請求"""
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
        self.set_user_state(user_id, "waiting")
        
        result = self.publisher.process_reply_message(
            reply_token,
            TextSendMessage(
                text=f"{user_name} 你好！✨\n🎨 圖片彩色化功能\n\n💎 此功能會消耗 {self.required_points} 點點數，讓您的珍貴回憶重現色彩！\n\n請上傳一張黑白照片，我將為您進行彩色化處理，讓回憶重新綻放光彩 🌈"
            ),
            user_id,
            event  # 傳遞 event 以支援群組聊天
        )
        return result
    
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
                "loadingSeconds": 30  # 設定為30秒，通常足夠處理圖片
            }
            
            # 發送請求（連線 3 秒、讀取 10 秒逾時，避免執行緒卡死）
            response = requests.post(url, headers=headers, json=data, timeout=(3, 10))
            
            if response.status_code == 200:
                logger.debug(f"載入動畫已啟動，用戶: {user_id}")
            else:
                logger.warning(f"載入動畫啟動失敗: {response.status_code} - {response.text}")
                
        except Exception as e:
            logger.warning(f"啟動載入動畫時發生錯誤: {str(e)}")
    
    def _colorize_image(self, image_bytes: bytes) -> str:
        """呼叫 Replicate 彩色化 API"""
        try:
            # 將 bytes 轉換為 base64 格式
            image_b64 = base64.b64encode(image_bytes).decode('utf-8')
            image_data_url = f"data:image/jpeg;base64,{image_b64}"
            
            # 使用 Replicate Python SDK
            output = replicate.run(
                self.replicate_model,
                input={
                    "input_image": image_data_url,
                }
            )
            
            if output:
                # 如果 output 是字串（URL），直接回傳
                if isinstance(output, str):
                    return output
                # 如果 output 是列表，回傳第一個元素
                elif isinstance(output, list) and len(output) > 0:
                    return output[0]
                # 如果 output 是 FileOutput 物件，轉換為字串
                else:
                    return str(output)
            else:
                raise Exception("API 沒有回傳結果")
                
        except Exception as e:
            logger.error(f"Replicate API 錯誤: {str(e)}")
            if "Insufficient credit" in str(e):
                raise Exception("Replicate 點數不足，請前往 https://replicate.com/account/billing#billing 購買點數")
            else:
                raise Exception(f"彩色化處理失敗: {str(e)}")

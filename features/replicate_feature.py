import os
import base64
import requests
import replicate
from linebot.models import TextSendMessage, ImageSendMessage

from app_logger import get_logger
from task_executor import submit_image_task
from .base_feature import BaseFeature, _UNSET
from .feature_registry import is_global_command

logger = get_logger("replicate_feature")

MAINTENANCE_MESSAGE = "⚠️ 系統維護中，功能暫時無法使用，請稍後再試 🙏"


class ReplicateImageFeature(BaseFeature):
    """Replicate 圖片功能的共用基底。

    收攏所有 Replicate 功能共用的建材：觸發判斷、會員/點數 guard、
    圖片下載、載入動畫、「扣點 → 處理 → 失敗退點 → 推送結果」流程、
    Replicate 呼叫與回傳解析。

    子類別需定義：
      - name（property）：功能名稱，同時作為扣點的 feature_type
      - trigger_command：觸發功能的訊息文字（例：「圖片彩色化」）
      - replicate_model：Replicate 模型 ID
      - required_points：在 __init__ 從環境變數讀取
      - image_waiting_state：等待用戶上傳圖片的狀態名稱
      - handle_text / handle_image：功能各自的流程
    可選覆寫：
      - loading_seconds：載入動畫秒數（預設 30）
    """

    trigger_command = None
    replicate_model = None
    image_waiting_state = None
    loading_seconds = 30

    def can_handle(self, message: str, user_id: str, user_state=_UNSET) -> bool:
        """觸發指令或用戶已在本功能狀態中"""
        if message == self.trigger_command:
            return True
        if is_global_command(message):
            return False
        state = self.resolve_user_state(user_id, user_state)
        return bool(state and state.get("feature") == self.name)

    def can_handle_image(self, user_id: str) -> bool:
        """在等待圖片狀態時才處理圖片"""
        if not self.image_waiting_state:
            return False
        return self.is_user_in_state(user_id, self.image_waiting_state)

    # ---- Guards ----

    def reply_maintenance_if_unavailable(self, reply_token, user_id, event, clear_state=False) -> bool:
        """會員系統不可用時回覆維護訊息（拒絕服務，避免免費放送處理額度）"""
        if self.member_service:
            return False
        if clear_state:
            try:
                self.clear_user_state(user_id)
            except Exception:
                logger.warning(f"清除用戶狀態失敗（維護模式）: {user_id}")
        self.publisher.process_reply_message(
            reply_token,
            TextSendMessage(text=MAINTENANCE_MESSAGE),
            user_id,
            event
        )
        return True

    def reply_if_insufficient_points(self, reply_token, user_name, user_id, event) -> bool:
        """點數不足時回覆提示；足夠回傳 False"""
        member = self.member_service.get_or_create_member(user_id, user_name)
        if member['points'] >= self.required_points:
            return False
        self.publisher.process_reply_message(
            reply_token,
            TextSendMessage(
                text=f"❌ 點數不足！\n\n💎 目前點數：{member['points']} 點\n💰 需要點數：{self.required_points} 點\n\n請輸入「點數」查看詳細資訊"
            ),
            user_id,
            event
        )
        return True

    # ---- 共用建材 ----

    def download_image(self, message_id: str) -> bytes:
        """從 LINE 下載用戶上傳的圖片"""
        message_content = self.line_bot_api.get_message_content(message_id)
        return b''.join(chunk for chunk in message_content.iter_content())

    def start_loading_animation(self, user_id: str):
        """發送 LINE 載入動畫；失敗不影響主流程"""
        try:
            response = requests.post(
                "https://api.line.me/v2/bot/chat/loading/start",
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {os.getenv("CHANNEL_ACCESS_TOKEN")}',
                },
                json={"chatId": user_id, "loadingSeconds": self.loading_seconds},
                timeout=(3, 10),
            )
            if response.status_code != 200:
                logger.warning(f"載入動畫啟動失敗: {response.status_code} - {response.text}")
        except Exception as e:
            logger.warning(f"啟動載入動畫時發生錯誤: {str(e)}")

    def submit_billed_processing(self, user_id, event, deduct_description, run) -> bool:
        """「先扣點 → run() 產出結果圖 URL → 推送；失敗退點」的完整計費流程。

        提交到共用的有界執行緒池；容量滿時回覆繁忙訊息並清除狀態。

        Args:
            run: 無參數 callable，回傳結果圖片 URL（在背景執行緒中執行）
        """
        def task():
            try:
                # 先扣點，扣不到就不處理（避免先服務後扣點被免費使用）
                if not self.member_service.deduct_points(
                    user_id,
                    self.required_points,
                    deduct_description,
                    feature_type=self.name,
                ):
                    self.publisher.process_push_message(
                        user_id,
                        TextSendMessage(text="❌ 點數不足或扣點失敗，本次未進行處理。\n請輸入「點數」查看剩餘點數。"),
                        event
                    )
                    return

                try:
                    output_url = run()
                except Exception as e:
                    # 處理失敗 → 退點並留下 failed 稽核記錄
                    logger.exception(f"{self.name} 處理失敗，退還點數: {user_id}")
                    self.member_service.refund_points(
                        user_id, self.required_points,
                        feature_type=self.name, reason=str(e)
                    )
                    self.publisher.process_push_message(
                        user_id,
                        TextSendMessage(text="處理圖片時發生錯誤，點數已退還，請稍後再試 🙏"),
                        event
                    )
                    return

                # 推送結果圖片（載入動畫會自動停止）
                self.publisher.process_push_message(
                    user_id,
                    ImageSendMessage(
                        original_content_url=output_url,
                        preview_image_url=output_url
                    ),
                    event
                )
            finally:
                self.clear_user_state(user_id)
                logger.info(f"用戶 {user_id} {self.name} 處理完成，狀態已重置")

        if not submit_image_task(task):
            # 執行緒池容量滿：優雅降級
            self.clear_user_state(user_id)
            self.publisher.process_push_message(
                user_id,
                TextSendMessage(text="目前使用人數較多，請稍後再試 🙏"),
                event
            )
            return False
        return True

    def run_replicate(self, input_dict: dict) -> str:
        """呼叫 Replicate 模型並解析出結果圖片 URL"""
        logger.debug(f"呼叫模型: {self.replicate_model}, input keys: {list(input_dict.keys())}")
        try:
            output = replicate.run(self.replicate_model, input=input_dict)
        except Exception as e:
            logger.error(f"Replicate API 錯誤: {str(e)}")
            if "Insufficient credit" in str(e):
                raise Exception("Replicate 點數不足，請前往 https://replicate.com/account/billing 儲值") from e
            raise

        logger.debug(f"API 回應類型: {type(output)}, 內容: {output}")
        url = self._extract_output_url(output)
        if not url:
            raise Exception("Replicate API 沒有回傳結果")
        return url

    @staticmethod
    def _extract_output_url(output):
        """從 Replicate 回傳值解析 URL（支援字串 / 列表 / FileOutput 物件）"""
        if not output:
            return None
        if isinstance(output, str):
            return output
        if isinstance(output, list):
            return ReplicateImageFeature._extract_output_url(output[0]) if output else None
        url_attr = getattr(output, 'url', None)
        if url_attr is not None:
            return url_attr() if callable(url_attr) else url_attr
        return str(output)

    @staticmethod
    def image_to_data_url(image_bytes: bytes) -> str:
        """將圖片 bytes 轉為 Replicate 接受的 base64 data URL"""
        image_b64 = base64.b64encode(image_bytes).decode('utf-8')
        return f"data:image/jpeg;base64,{image_b64}"

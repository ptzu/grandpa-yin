from linebot.models import TextSendMessage, ImageSendMessage

from src.core.app_logger import get_logger
from src.core.model_config import get_model_config
from .base_feature import BaseFeature, _UNSET
from .feature_registry import is_global_command

logger = get_logger("replicate_feature")

MAINTENANCE_MESSAGE = "⚠️ 系統維護中，功能暫時無法使用，請稍後再試 🙏"
PROCESSING_FAILURE_MESSAGE = "處理圖片時發生錯誤，點數已退還，請稍後再試 🙏"


class ReplicateImageFeature(BaseFeature):
    """Replicate 圖片功能的共用基底。

    只負責這類功能共通的「對話面」：觸發判斷、會員/點數 guard、交棒入口，
    以及把計費（services.billing）與模型呼叫（services.replicate_client）
    接起來。金流編排與 API 呼叫本身都不在這裡。

    子類別需定義：
      - name（property）：功能名稱，同時作為扣點的 feature_type，也是
        config/models.yml 裡對應的區段名稱
      - trigger_command：觸發功能的訊息文字（例：「圖片彩色化」）
      - image_waiting_state：等待用戶上傳圖片的狀態名稱
      - handle_text / handle_image：功能各自的流程

    模型 ID、點數、載入秒數與模型的輸入欄位對應都來自 config/models.yml，
    不寫在程式碼裡（換模型／調價不必改碼）。
    """

    trigger_command = None
    image_waiting_state = None

    def __init__(self, ctx):
        super().__init__(ctx)
        self.config = get_model_config(self.name)
        self.replicate_model = self.config.model
        self.required_points = self.config.cost
        self.loading_seconds = self.config.loading_seconds

    def can_handle(self, message: str, user_id: str, user_state=_UNSET) -> bool:
        """觸發指令或用戶已在本功能狀態中"""
        if message == self.trigger_command:
            return True
        if is_global_command(message):
            return False
        # 別的功能的觸發指令不該被本功能的狀態吃掉（例如卡在「圖片編輯」
        # 流程中途時輸入「圖片彩色化」，應該切過去而不是被當成編輯描述）
        if self._is_other_trigger_command(message):
            return False
        state = self.resolve_user_state(user_id, user_state)
        return bool(state and state.get("feature") == self.name)

    def _is_other_trigger_command(self, message: str) -> bool:
        """訊息是否為其他已註冊功能的觸發指令"""
        if not self.registry:
            return False
        for feature in self.registry.get_all_features():
            if feature is self:
                continue
            if getattr(feature, "trigger_command", None) == message:
                return True
        return False

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

    # ---- 交棒入口（用戶先傳圖、後選功能） ----

    def accept_handoff(self, reply_token, user_id, event, stash: dict) -> bool:
        """接手一張在選功能之前就上傳的照片（由 PhotoIntentFeature 交棒）。

        跑完與正常入口相同的 guard 後委派給 begin_from_stash()。回傳 False
        代表 guard 擋下、未接手，呼叫端需自行清理 stash 與狀態。
        """
        if self.reply_maintenance_if_unavailable(reply_token, user_id, event):
            return False

        user_name = self.get_user_name(user_id)
        if self.reply_if_insufficient_points(reply_token, user_name, user_id, event):
            return False

        self.begin_from_stash(reply_token, user_id, event, stash)
        return True

    def begin_from_stash(self, reply_token, user_id, event, stash: dict):
        """以暫存的圖片開始本功能的流程（guard 已由 accept_handoff 跑過）"""
        raise NotImplementedError

    # ---- 共用建材 ----

    def start_loading_animation(self, user_id: str):
        """發送 LINE 載入動畫；失敗不影響主流程"""
        self.line.start_loading_animation(user_id, self.loading_seconds)

    def submit_billed_processing(self, user_id, event, deduct_description, run) -> bool:
        """扣點跑 run()，成功就把結果圖推給用戶。

        金流（扣點／失敗退點／滿載降級）由 BillingService 負責；這裡只補上
        「圖片功能」專屬的部分：結果如何呈現、結束後清狀態。

        Args:
            run: 無參數 callable，回傳結果圖片 URL（在背景執行緒中執行）
        """
        def deliver(output_url):
            # 推送結果圖片（載入動畫會自動停止）
            self.publisher.process_push_message(
                user_id,
                ImageSendMessage(
                    original_content_url=output_url,
                    preview_image_url=output_url
                ),
                event
            )

        return self.billing.submit(
            user_id=user_id,
            event=event,
            points=self.required_points,
            feature_type=self.name,
            description=deduct_description,
            run=run,
            on_success=deliver,
            on_finish=lambda: self.clear_user_state(user_id),
            failure_message=PROCESSING_FAILURE_MESSAGE,
        )

    def run_replicate(self, input_dict: dict) -> str:
        """呼叫本功能的 Replicate 模型並取得結果圖片 URL"""
        return self.replicate.run(self.replicate_model, input_dict)

    def build_model_input(self, image_bytes: bytes, prompt: str = None) -> dict:
        """依設定檔的欄位對應組出模型輸入（不同模型欄位名稱不同）"""
        return self.config.build_input(
            self.replicate.image_to_data_url(image_bytes), prompt=prompt
        )

    def run_model(self, image_bytes: bytes, prompt: str = None) -> str:
        """組輸入 → 呼叫模型 → 取得結果圖片 URL"""
        return self.run_replicate(self.build_model_input(image_bytes, prompt=prompt))

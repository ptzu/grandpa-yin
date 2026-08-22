from linebot.models import TextSendMessage, ImageSendMessage

from src.core.app_logger import get_logger
from src.core.settings import get_model_config
from .base_feature import BaseFeature, _UNSET
from .feature_registry import is_global_command

logger = get_logger("replicate_feature")

MAINTENANCE_MESSAGE = "系統正在維護，這個功能暫時不能用，麻煩晚一點再試。"
PROCESSING_FAILURE_MESSAGE = "處理的時候出了點問題，點數已經退還給您，麻煩晚一點再試。"


class ReplicateImageFeature(BaseFeature):
    """Replicate 圖片功能的共用基底。

    只負責這類功能共通的「對話面」：觸發判斷、會員/點數 guard、交棒入口，
    以及把計費（services.billing）與模型呼叫（services.replicate_client）
    接起來。金流編排與 API 呼叫本身都不在這裡。

    子類別需定義：
      - name（property）：功能名稱，同時作為扣點的 feature_type，也是
        config/settings.yml 裡對應的區段名稱
      - trigger_command：觸發功能的訊息文字（例：「修復老照片」）
      - image_waiting_state：等待用戶上傳圖片的狀態名稱
      - handle_text / handle_image：功能各自的流程

    模型 ID、點數、載入秒數與模型的輸入欄位對應都來自 config/settings.yml，
    不寫在程式碼裡（換模型／調價不必改碼）。
    """

    trigger_command = None
    image_waiting_state = None

    # 成品能不能當成下一個功能的輸入。輸出影片的功能要設 False：影片餵不回
    # 圖片模型，給了「再修一下」的按鈕只會讓用戶按了撲空。
    result_can_be_reused = True

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
        # 別的功能的觸發指令不該被本功能的狀態吃掉（例如卡在「P圖大神」
        # 流程中途時輸入「修復老照片」，應該切過去而不是被當成編輯描述）
        if self.is_other_trigger_command(message):
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
        # 光說「不夠」是死路；同一則訊息就要給出拿到點數的方法
        hint = self.points_top_up_hint()
        text = (f"點數不夠喔。\n\n您現在有 {member['points']} 點，"
                f"這個功能需要 {self.required_points} 點。")
        if hint:
            text += f"\n\n{hint}"
        self.publisher.process_reply_message(
            reply_token,
            TextSendMessage(text=text),
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

    def build_result_message(self, output_url: str):
        """結果要以什麼訊息型別推給用戶（預設圖片；產影片的功能覆寫它）"""
        return ImageSendMessage(
            original_content_url=output_url,
            preview_image_url=output_url,
        )

    def submit_billed_processing(self, user_id, event, deduct_description, run,
                                 build_message=None, on_finish=None) -> bool:
        """扣點跑 run()，成功就把結果推給用戶。

        金流（扣點／失敗退點／滿載降級）由 BillingService 負責；這裡只補上
        這類功能專屬的部分：結果如何呈現、結束後清狀態。

        Args:
            run: 無參數 callable，回傳結果的 URL（在背景執行緒中執行）
            build_message: 覆寫結果訊息的組法（預設用 build_result_message）
            on_finish: 覆寫收尾動作（預設清除用戶狀態）
        """
        build_message = build_message or self.build_result_message
        finish_default = on_finish or (lambda: self.clear_user_state(user_id))
        followup = self._followup_feature(event)
        # deliver 與收尾是 billing 分開呼叫的兩個 callback，用它把成品網址帶過去
        delivered = {}

        def deliver(output_url):
            # 推送結果（載入動畫會自動停止）；回傳送達與否供 billing 決定退不退點
            result_url = self.archive_result(output_url)
            messages = [build_message(result_url)]

            offer = self._build_followup_offer(followup, user_id)
            if offer:
                messages.append(offer)

            if self.publisher.process_push_message(user_id, messages, event) is False:
                return False

            if offer:
                delivered["result_url"] = result_url
            return True

        def finish():
            """成功交付且有後續選項時進入 follow-up 狀態，否則照原本的方式收尾"""
            result_url = delivered.get("result_url")
            if not result_url:
                finish_default()
                return
            try:
                followup.remember(user_id, result_url)
            except Exception:
                # 記不起來只是少了「下一步」，不能讓用戶卡在 processing 狀態裡
                logger.exception(f"寫入 follow-up 狀態失敗: {user_id}")
                finish_default()

        return self.billing.submit(
            user_id=user_id,
            event=event,
            points=self.required_points,
            feature_type=self.name,
            description=deduct_description,
            run=run,
            on_success=deliver,
            on_finish=finish,
            failure_message=PROCESSING_FAILURE_MESSAGE,
        )

    def _followup_feature(self, event: dict):
        """負責「還要再做點什麼嗎」的功能；不適用時回 None。

        三種不適用：本功能的成品不能再當輸入（影片）、沒有成品保存所以待會
        取不回成品、以及群組聊天——follow-up 狀態掛在個人身上，群組裡誰接著
        講話都會踩到它。
        """
        if not self.result_can_be_reused or not self.result_archive or not self.registry:
            return None
        if event and self.is_group_chat(event):
            return None
        return self.registry.get_feature_by_name("followup")

    @staticmethod
    def _build_followup_offer(followup, user_id: str):
        """組後續選項訊息；失敗就當作沒有——成品送達遠比多一排按鈕重要"""
        if not followup:
            return None
        try:
            return followup.build_offer(user_id)
        except Exception:
            logger.exception(f"組後續選項失敗，只推成品: {user_id}")
            return None

    def archive_result(self, output_url: str) -> str:
        """把成品轉存到自家 Storage，回傳能撐 30 天的網址。

        模型給的網址約一小時後就失效，用戶往上滑重看會破圖。保存失敗時原樣
        回傳模型的網址：留久一點是加分項，送達才是這次付費買到的東西。
        """
        if not self.result_archive:
            return output_url
        return self.result_archive.archive(output_url) or output_url

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

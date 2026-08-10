import base64
from abc import ABC, abstractmethod
from src.core.app_logger import get_logger
from .context import FeatureContext

logger = get_logger("feature")

# can_handle 的 user_state 未傳入時的 sentinel（None 是合法的「無狀態」值）
_UNSET = object()


class BaseFeature(ABC):
    """所有功能的基礎類別"""

    def __init__(self, ctx: FeatureContext):
        self.ctx = ctx
        # Aliases for the collaborators features actually use, so call sites stay
        # short. New dependencies go on FeatureContext, not into this signature.
        self.line = ctx.line
        self.publisher = ctx.publisher
        self.state_manager = ctx.state_manager
        self.member_service = ctx.member_service
        self.storage_service = ctx.storage_service
        # Set by FeatureRegistry.register(); lets a feature hand off to a sibling
        # (photo_intent -> colorize/edit) without app.py wiring them to each other.
        self.registry = None

    @property
    @abstractmethod
    def name(self) -> str:
        """功能名稱，用於識別和狀態管理"""
        pass
    
    @abstractmethod
    def can_handle(self, message: str, user_id: str, user_state=_UNSET) -> bool:
        """
        判斷是否能處理此訊息

        Args:
            message: 用戶訊息
            user_id: 用戶 ID
            user_state: 已查詢好的用戶狀態（路由層傳入，避免每個功能重複查 DB）

        Returns:
            bool: 是否能處理
        """
        pass

    def resolve_user_state(self, user_id: str, user_state=_UNSET):
        """取得用戶狀態：優先使用路由層傳入的結果，未傳入才查 DB"""
        if user_state is _UNSET:
            return self.get_user_state(user_id)
        return user_state
    
    @abstractmethod
    def handle_text(self, event: dict) -> dict:
        """
        處理文字訊息
        
        Args:
            event: LINE webhook event
            
        Returns:
            dict: Flask 回應或 None
        """
        pass
    
    def can_handle_image(self, user_id: str) -> bool:
        """
        判斷是否能處理圖片訊息（預設不處理）

        Args:
            user_id: 用戶 ID

        Returns:
            bool: 是否能處理
        """
        return False

    def handle_image(self, event: dict) -> dict:
        """
        處理圖片訊息（預設不處理）

        Args:
            event: LINE webhook event

        Returns:
            dict: Flask 回應或 None
        """
        return None
    
    def get_user_name(self, user_id: str) -> str:
        """獲取用戶名稱：優先讀 DB 會員資料，避免每則訊息都呼叫 LINE API"""
        if self.member_service:
            try:
                member = self.member_service.get_member_info(user_id)
                if member and member.get('display_name') and member['display_name'] != '使用者':
                    return member['display_name']
            except Exception as e:
                logger.warning(f"讀取會員名稱失敗：{str(e)}")

        return self.line.get_display_name(user_id) or "使用者"
    
    def get_user_id(self, event: dict) -> str:
        """從 event 中獲取用戶 ID"""
        return event.get('source', {}).get('userId', '')
    
    def get_group_id(self, event: dict) -> str:
        """從 event 中獲取群組 ID"""
        return event.get('source', {}).get('groupId', '')
    
    def get_room_id(self, event: dict) -> str:
        """從 event 中獲取房間 ID"""
        return event.get('source', {}).get('roomId', '')
    
    def get_source_type(self, event: dict) -> str:
        """從 event 中獲取來源類型"""
        return event.get('source', {}).get('type', 'user')
    
    def get_target_id(self, event: dict) -> str:
        """
        獲取正確的目標ID（用於推送訊息）
        群組聊天時返回群組ID，個人聊天時返回用戶ID
        """
        source_type = self.get_source_type(event)
        if source_type == 'group':
            return self.get_group_id(event)
        elif source_type == 'room':
            return self.get_room_id(event)
        else:  # source_type == 'user'
            return self.get_user_id(event)
    
    def is_group_chat(self, event: dict) -> bool:
        """判斷是否為群組聊天"""
        source_type = self.get_source_type(event)
        return source_type in ['group', 'room']
    
    def get_reply_token(self, event: dict) -> str:
        """從 event 中獲取回覆 token"""
        return event.get('replyToken', '')
    
    def get_message_text(self, event: dict) -> str:
        """從 event 中獲取訊息文字"""
        return event.get('message', {}).get('text', '')
    
    def get_message_id(self, event: dict) -> str:
        """從 event 中獲取訊息 ID"""
        return event.get('message', {}).get('id', '')

    # ---- 圖片下載與暫存（跨功能共用） ----

    def download_image(self, message_id: str) -> bytes:
        """從 LINE 下載用戶上傳的圖片"""
        return self.line.download_message_content(message_id)

    def stash_image(self, image_bytes: bytes) -> dict:
        """暫存圖片，回傳可直接放進 user state `data` 的 payload。

        優先放 Supabase Storage（state 只留 object key），未設定時退回
        base64 內嵌——會讓 JSONB row 膨脹，僅作為不中斷服務的降級路徑。
        """
        if self.storage_service and self.storage_service.is_configured():
            key = self.storage_service.upload_image(image_bytes, prefix=self.name)
            return {"image_key": key}

        logger.warning("Supabase Storage 未設定，退回以 base64 暫存於 state（建議設定 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY）")
        return {"image_data": base64.b64encode(image_bytes).decode('utf-8')}

    @staticmethod
    def has_stashed_image(state_data: dict) -> bool:
        """state data 中是否帶有暫存圖片"""
        state_data = state_data or {}
        return bool(state_data.get("image_key") or state_data.get("image_data"))

    def load_stashed_image(self, state_data: dict) -> bytes:
        """取回 stash_image() 暫存的圖片；找不到時回傳 None"""
        state_data = state_data or {}
        image_key = state_data.get("image_key")
        if image_key:
            return self.storage_service.download_image(image_key)

        image_data = state_data.get("image_data")
        if image_data:
            return base64.b64decode(image_data)

        return None

    def discard_stashed_image(self, state_data: dict):
        """丟棄暫存圖片（用過或取消）；失敗只記 log，不影響主流程"""
        state_data = state_data or {}
        image_key = state_data.get("image_key")
        if image_key and self.storage_service:
            self.storage_service.delete_image(image_key)

    def set_user_state(self, user_id: str, state: str, data: dict = None):
        """設定用戶狀態；順手清掉被這次轉換取代掉的暫存圖"""
        replaced = self.state_manager.set_state(user_id, {
            "feature": self.name,
            "state": state,
            "data": data
        })
        self._discard_superseded_image(replaced, data)

    def get_user_state(self, user_id: str) -> dict:
        """獲取用戶狀態"""
        return self.state_manager.get_state(user_id)

    def clear_user_state(self, user_id: str):
        """清除用戶狀態；順手清掉該狀態引用的暫存圖"""
        removed = self.state_manager.clear_state(user_id)
        self._discard_superseded_image(removed, None)

    def _discard_superseded_image(self, previous_data: dict, next_data: dict):
        """狀態轉換後刪掉不再被任何狀態引用的暫存圖。

        收攏在狀態轉換這一層，而不是散在各功能裡：切換到別的功能、背景任務
        結束、流程被中斷……所有路徑都會經過 set_user_state / clear_user_state，
        放這裡才不會有漏網的孤兒物件。同一張圖延用到下一個狀態時不刪。
        """
        old_key = (previous_data or {}).get("image_key")
        if not old_key:
            return
        if (next_data or {}).get("image_key") == old_key:
            return

        try:
            self.discard_stashed_image({"image_key": old_key})
        except Exception:
            # 清理是盡力而為，失敗不能影響主流程；殘留物件由 cleanup_storage.py 兜底
            logger.warning(f"清理被取代的暫存圖失敗: {old_key}")
    
    def is_user_in_state(self, user_id: str, state: str) -> bool:
        """檢查用戶是否在特定狀態"""
        user_state = self.get_user_state(user_id)
        if not user_state:
            return False
        return (user_state.get("feature") == self.name and 
                user_state.get("state") == state)

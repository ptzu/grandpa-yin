from abc import ABC, abstractmethod
from linebot import LineBotApi
from message_publisher import MessagePublisher
from user_state_manager import UserStateManager

# can_handle 的 user_state 未傳入時的 sentinel（None 是合法的「無狀態」值）
_UNSET = object()


class BaseFeature(ABC):
    """所有功能的基礎類別"""
    
    def __init__(self, line_bot_api: LineBotApi, publisher: MessagePublisher, state_manager: UserStateManager, member_service=None):
        self.line_bot_api = line_bot_api
        self.publisher = publisher
        self.state_manager = state_manager
        self.member_service = member_service
    
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
                print(f"讀取會員名稱失敗：{str(e)}")

        try:
            profile = self.line_bot_api.get_profile(user_id)
            return profile.display_name
        except Exception as e:
            print(f"無法獲取用戶名稱：{str(e)}")
            return "使用者"
    
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
    
    def set_user_state(self, user_id: str, state: str, data: dict = None):
        """設定用戶狀態"""
        self.state_manager.set_state(user_id, {
            "feature": self.name, 
            "state": state,
            "data": data
        })
    
    def get_user_state(self, user_id: str) -> dict:
        """獲取用戶狀態"""
        return self.state_manager.get_state(user_id)
    
    def clear_user_state(self, user_id: str):
        """清除用戶狀態"""
        self.state_manager.clear_state(user_id)
    
    def is_user_in_state(self, user_id: str, state: str) -> bool:
        """檢查用戶是否在特定狀態"""
        user_state = self.get_user_state(user_id)
        if not user_state:
            return False
        return (user_state.get("feature") == self.name and 
                user_state.get("state") == state)

from typing import List, Optional
from app_logger import get_logger
from .base_feature import BaseFeature

logger = get_logger("registry")


class FeatureRegistry:
    """功能註冊表，負責路由訊息到對應的功能處理器"""
    
    # 全局命令：這些命令可以在任何功能狀態下被執行
    GLOBAL_COMMANDS = [
        "點數", "點數查詢", "查看點數", "查詢點數",
        "歷史", "交易記錄", "記錄",
        "會員", "會員資訊",
        "!功能", "功能", "！功能", "使用說明", "其他功能"
    ]
    
    def __init__(self):
        self.features: List[BaseFeature] = []
    
    def register(self, feature: BaseFeature):
        """註冊功能"""
        self.features.append(feature)
        logger.info(f"已註冊功能: {feature.name}")
    
    def get_feature_by_name(self, name: str) -> Optional[BaseFeature]:
        """根據名稱獲取功能"""
        for feature in self.features:
            if feature.name == name:
                return feature
        return None
    
    def route_text_message(self, event: dict) -> dict:
        """
        路由文字訊息到對應的功能處理器
        
        Args:
            event: LINE webhook event
            
        Returns:
            dict: Flask 回應或 None
        """
        user_id = event.get('source', {}).get('userId', '')
        message = event.get('message', {}).get('text', '').strip()
        
        # 檢查是否為全局命令
        is_global_command = self._is_global_command(message)
        
        # 1. 如果是全局命令，直接尋找能處理此訊息的功能（跳過用戶狀態檢查）
        if is_global_command:
            for feature in self.features:
                if feature.can_handle(message, user_id):
                    logger.debug(f"全局命令路由到功能: {feature.name}")
                    return feature.handle_text(event)
        
        # 2. 如果不是全局命令，首先檢查用戶是否有特定功能的狀態
        #    （狀態只查一次，傳給 can_handle 避免每個功能重複查 DB）
        user_state = self._get_user_state(user_id)
        if user_state and user_state.get("feature"):
            feature_name = user_state.get("feature")
            feature = self.get_feature_by_name(feature_name)
            if feature and feature.can_handle(message, user_id, user_state=user_state):
                logger.debug(f"根據用戶狀態路由到功能: {feature_name}")
                return feature.handle_text(event)

        # 3. 如果沒有狀態或狀態中的功能無法處理，則尋找能處理此訊息的功能
        for feature in self.features:
            if feature.can_handle(message, user_id, user_state=user_state):
                logger.debug(f"路由到功能: {feature.name}")
                return feature.handle_text(event)
        
        # 4. 沒有功能能處理此訊息
        logger.info(f"沒有功能能處理訊息: {message}")
        return None
    
    def route_image_message(self, event: dict) -> dict:
        """
        路由圖片訊息到對應的功能處理器
        
        Args:
            event: LINE webhook event
            
        Returns:
            dict: Flask 回應或 None
        """
        user_id = event.get('source', {}).get('userId', '')
        
        # 1. 首先檢查用戶是否有特定功能的狀態
        user_state = self._get_user_state(user_id)
        if user_state and user_state.get("feature"):
            feature_name = user_state.get("feature")
            feature = self.get_feature_by_name(feature_name)
            if feature:
                logger.debug(f"根據用戶狀態路由圖片到功能: {feature_name}")
                return feature.handle_image(event)
        
        # 2. 如果沒有狀態，則尋找能處理圖片的功能（先判斷、再執行，避免重複執行）
        for feature in self.features:
            if feature.can_handle_image(user_id):
                logger.debug(f"路由圖片到功能: {feature.name}")
                return feature.handle_image(event)

        # 3. 沒有功能能處理此圖片
        logger.info("沒有功能能處理圖片訊息")
        return None
    
    def _is_global_command(self, message: str) -> bool:
        """檢查訊息是否為全局命令"""
        message = message.strip()
        # 完全匹配
        if message in self.GLOBAL_COMMANDS:
            return True
        # 包含關鍵字的命令
        if "點數" in message and ("查詢" in message or "查看" in message):
            return True
        return False
    
    def _get_user_state(self, user_id: str) -> dict:
        """獲取用戶狀態（需要從第一個功能中獲取 state_manager）"""
        if self.features:
            return self.features[0].get_user_state(user_id)
        return None
    
    def get_all_features(self) -> List[BaseFeature]:
        """獲取所有註冊的功能"""
        return self.features.copy()

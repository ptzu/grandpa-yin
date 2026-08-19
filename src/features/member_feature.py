from datetime import datetime

from src.core.app_logger import get_logger
from src.features.base_feature import BaseFeature

logger = get_logger("member")


class MemberFeature(BaseFeature):
    """會員功能 - 提供點數查詢、交易記錄等功能"""
    
    @property
    def name(self) -> str:
        return "member"
    
    # 本功能負責的指令（完全比對；同義詞已精簡掉，見 feature_registry）
    COMMANDS = ("點數", "歷史", "會員", "儲值")

    def can_handle(self, message: str, user_id: str, user_state=None) -> bool:
        """判斷是否能處理此訊息"""
        return message.strip() in self.COMMANDS
    
    def handle_text(self, event: dict) -> dict:
        """處理文字訊息"""
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message = self.get_message_text(event).strip()
        user_name = self.get_user_name(user_id)
        
        if message == "點數":
            return self._handle_points_query(user_id, user_name, reply_token, event)
        elif message == "歷史":
            return self._handle_history_query(user_id, user_name, reply_token, event)
        elif message == "會員":
            return self._handle_member_info(user_id, user_name, reply_token, event)
        elif message == "儲值":
            return self._handle_topup(user_id, user_name, reply_token, event)

        return None

    def _handle_topup(self, user_id: str, user_name: str, reply_token: str, event: dict):
        """處理儲值：給出付款頁連結與方案"""
        link = self.payment_service.topup_link() if self.payment_service else None
        if not link:
            self.publisher.reply_text(
                reply_token,
                "目前還沒開放線上加值。需要點數的話，再跟我們說一聲。",
                user_id, event,
            )
            return "OK"

        try:
            member = self.member_service.get_or_create_member(user_id, user_name)
            balance_line = f"您現在有 {member['points']} 點。\n\n" if member else ""

            plans = "\n".join(
                f"{pkg.label}　NT${pkg.price_twd}"
                for pkg in self.payment_service.packages()
            )
            response = f"""{balance_line}要加點數，點下面的連結：
{link}

{plans}

付款完成後點數會自動加進來。"""

            self.publisher.reply_text(reply_token, response, user_id, event)
            return "OK"

        except Exception:
            logger.exception(f"處理儲值失敗: {user_id}")
            self.publisher.reply_text(reply_token, "現在沒辦法加值，麻煩晚一點再試。", user_id, event)
            return "OK"
    
    def _handle_points_query(self, user_id: str, user_name: str, reply_token: str, event: dict):
        """處理點數查詢"""
        try:
            # 使用統一的會員服務獲取或建立會員
            member = self.member_service.get_or_create_member(user_id, user_name)
            
            if not member:
                self.publisher.reply_text(reply_token, "現在查不到您的資料，麻煩晚一點再試。", user_id, event)
                return "OK"
            
            # 從字典中提取所需的屬性值
            display_name = member['display_name']
            points = member['points']
            status = member['status']
            
            # 狀態顯示
            status_map = {
                'normal': '正常',
                'vip': 'VIP',
                'suspended': '停用',
                'banned': '黑名單'
            }
            status_text = status_map.get(status, status)
            
            # 狀態表情符號
            status_emoji = {
                'normal': '✅',
                'vip': '⭐',
                'suspended': '⚠️',
                'banned': '🚫'
            }
            emoji = status_emoji.get(status, '❓')
            
            status_line = "" if status == "normal" else f"\n狀態：{status_text}"
            can_top_up = bool(self.payment_service and self.payment_service.topup_link())
            topup_line = "\n想加點數，輸入「儲值」" if can_top_up else ""
            response = f"""{display_name}，您還有 {points} 點。{status_line}

想看用過哪些，輸入「歷史」
想看完整資料，輸入「會員」{topup_line}"""
            
            self.publisher.reply_text(reply_token, response, user_id, event)
            return "OK"
            
        except Exception:
            logger.exception(f"查詢點數失敗: {user_id}")
            self.publisher.reply_text(reply_token, "現在查不到，麻煩晚一點再試。", user_id, event)
            return "OK"
    
    def _handle_history_query(self, user_id: str, user_name: str, reply_token: str, event: dict):
        """處理交易記錄查詢"""
        try:
            # 使用統一的會員服務獲取或建立會員
            member = self.member_service.get_or_create_member(user_id, user_name)
            
            if not member:
                self.publisher.reply_text(reply_token, "現在查不到您的資料，麻煩晚一點再試。", user_id, event)
                return "OK"
            
            # 從字典中提取點數
            current_points = member['points']
            
            # 查詢交易記錄
            transactions = self.member_service.get_point_history(user_id, limit=10)
            
            if not transactions:
                response = f"""還沒有任何紀錄。

您現在有 {current_points} 點。"""
                self.publisher.reply_text(reply_token, response, user_id, event)
                return "OK"
            
            # 組合回應訊息
            response_lines = ["最近的紀錄：\n"]
            
            for trans in transactions:
                # 格式化時間
                created_at = datetime.fromisoformat(trans['created_at'])
                time_str = created_at.strftime("%m/%d %H:%M")
                
                # 交易類型顯示
                type_map = {
                    'earn': '獲得',
                    'spend': '使用',
                    'admin_add': '增加',
                    'admin_deduct': '扣除',
                    'expire': '過期'
                }
                type_str = type_map.get(trans['transaction_type'], trans['transaction_type'])
                
                # 點數顯示（正數顯示 +，負數自動有 -）
                points = trans['points']
                points_str = f"+{points}" if points > 0 else str(points)
                
                # 描述
                desc = trans['description'] or "無說明"
                
                line = f"{time_str}　{type_str} {points_str} 點（剩 {trans['balance_after']} 點）\n{desc}\n"
                response_lines.append(line)
            
            response_lines.append(f"\n您現在有 {current_points} 點。")
            response = "\n".join(response_lines)
            
            self.publisher.reply_text(reply_token, response, user_id, event)
            return "OK"
            
        except Exception:
            logger.exception(f"查詢交易記錄失敗: {user_id}")
            self.publisher.reply_text(reply_token, "現在查不到，麻煩晚一點再試。", user_id, event)
            return "OK"
    
    def _handle_member_info(self, user_id: str, user_name: str, reply_token: str, event: dict):
        """處理會員資訊查詢"""
        try:
            # 使用統一的會員服務獲取或建立會員
            member = self.member_service.get_or_create_member(user_id, user_name)
            
            if not member:
                self.publisher.reply_text(reply_token, "現在查不到您的資料，麻煩晚一點再試。", user_id, event)
                return "OK"
            
            # 從字典中提取所需的屬性值
            display_name = member['display_name']
            points = member['points']
            status = member['status']
            created_at_str = member['created_at']
            
            # 狀態顯示
            status_map = {
                'normal': '正常',
                'vip': 'VIP',
                'suspended': '停用',
                'banned': '黑名單'
            }
            status_text = status_map.get(status, status)
            
            # 格式化日期（從 ISO 字符串轉換）
            if created_at_str:
                try:
                    created_at = datetime.fromisoformat(created_at_str)
                    created_at_str = created_at.strftime("%Y/%m/%d %H:%M")
                except Exception as e:
                    logger.warning(f"日期格式化失敗: {str(e)}")
                    created_at_str = "未知"
            else:
                created_at_str = "未知"
            
            response = f"""{display_name}

點數：{points} 點
狀態：{status_text}
加入時間：{created_at_str}"""
            
            self.publisher.reply_text(reply_token, response, user_id, event)
            return "OK"
            
        except Exception:
            logger.exception(f"查詢會員資訊失敗: {user_id}")
            self.publisher.reply_text(reply_token, "現在查不到，麻煩晚一點再試。", user_id, event)
            return "OK"


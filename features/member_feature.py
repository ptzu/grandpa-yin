from features.base_feature import BaseFeature
from datetime import datetime


class MemberFeature(BaseFeature):
    """會員功能 - 提供點數查詢、交易記錄等功能"""
    
    @property
    def name(self) -> str:
        return "member"
    
    def can_handle(self, message: str, user_id: str, user_state=None) -> bool:
        """判斷是否能處理此訊息"""
        message = message.strip()
        
        # 完全匹配的指令
        exact_commands = ["點數", "歷史", "會員資訊", "會員"]
        if message in exact_commands:
            return True
        
        # 包含關鍵字的指令（支援更靈活的輸入）
        if "點數" in message and ("查詢" in message or "查看" in message or message == "點數"):
            return True
        if "歷史" in message or "交易記錄" in message or "記錄" in message:
            return True
        if "會員" in message:
            return True
        
        return False
    
    def handle_text(self, event: dict) -> dict:
        """處理文字訊息"""
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message = self.get_message_text(event).strip()
        user_name = self.get_user_name(user_id)
        
        # 點數相關查詢
        if message == "點數" or ("點數" in message and ("查詢" in message or "查看" in message)):
            return self._handle_points_query(user_id, user_name, reply_token, event)
        # 歷史/交易記錄查詢
        elif message == "歷史" or "交易記錄" in message or (message == "記錄"):
            return self._handle_history_query(user_id, user_name, reply_token, event)
        # 會員資訊查詢
        elif message in ["會員資訊", "會員"]:
            return self._handle_member_info(user_id, user_name, reply_token, event)
        
        return None
    
    def _handle_points_query(self, user_id: str, user_name: str, reply_token: str, event: dict):
        """處理點數查詢"""
        try:
            # 使用統一的會員服務獲取或建立會員
            member = self.member_service.get_or_create_member(user_id, user_name)
            
            if not member:
                self.publisher.reply_text(reply_token, "❌ 無法取得會員資料，請稍後再試", user_id, event)
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
            
            response = f"""💰 點數查詢

👤 {display_name}
💎 剩餘點數：{points} 點
{emoji} 會員狀態：{status_text}

輸入「歷史」查看交易記錄
輸入「會員資訊」查看完整資料"""
            
            self.publisher.reply_text(reply_token, response, user_id, event)
            return "OK"
            
        except Exception as e:
            print(f"❌ 查詢點數失敗: {str(e)}")
            import traceback
            traceback.print_exc()
            self.publisher.reply_text(reply_token, "❌ 查詢失敗，請稍後再試", user_id, event)
            return "OK"
    
    def _handle_history_query(self, user_id: str, user_name: str, reply_token: str, event: dict):
        """處理交易記錄查詢"""
        try:
            # 使用統一的會員服務獲取或建立會員
            member = self.member_service.get_or_create_member(user_id, user_name)
            
            if not member:
                self.publisher.reply_text(reply_token, "❌ 無法取得會員資料，請稍後再試", user_id, event)
                return "OK"
            
            # 從字典中提取點數
            current_points = member['points']
            
            # 查詢交易記錄
            transactions = self.member_service.get_point_history(user_id, limit=10)
            
            if not transactions:
                response = f"""📊 交易記錄

目前沒有任何交易記錄

💎 目前點數：{current_points} 點"""
                self.publisher.reply_text(reply_token, response, user_id, event)
                return "OK"
            
            # 組合回應訊息
            response_lines = ["📊 交易記錄（最近 10 筆）\n"]
            
            for trans in transactions:
                # 格式化時間
                created_at = datetime.fromisoformat(trans['created_at'])
                time_str = created_at.strftime("%m/%d %H:%M")
                
                # 交易類型顯示
                type_map = {
                    'earn': '🎁 獲得',
                    'spend': '💳 消費',
                    'admin_add': '➕ 管理員增加',
                    'admin_deduct': '➖ 管理員扣除',
                    'expire': '⏰ 過期'
                }
                type_str = type_map.get(trans['transaction_type'], trans['transaction_type'])
                
                # 點數顯示（正數顯示 +，負數自動有 -）
                points = trans['points']
                points_str = f"+{points}" if points > 0 else str(points)
                
                # 描述
                desc = trans['description'] or "無說明"
                
                line = f"{time_str} {type_str}\n{points_str} 點 → 餘額 {trans['balance_after']} 點\n說明：{desc}\n"
                response_lines.append(line)
            
            response_lines.append(f"\n💎 目前點數：{current_points} 點")
            response = "\n".join(response_lines)
            
            self.publisher.reply_text(reply_token, response, user_id, event)
            return "OK"
            
        except Exception as e:
            print(f"❌ 查詢交易記錄失敗: {str(e)}")
            import traceback
            traceback.print_exc()
            self.publisher.reply_text(reply_token, "❌ 查詢失敗，請稍後再試", user_id, event)
            return "OK"
    
    def _handle_member_info(self, user_id: str, user_name: str, reply_token: str, event: dict):
        """處理會員資訊查詢"""
        try:
            # 使用統一的會員服務獲取或建立會員
            member = self.member_service.get_or_create_member(user_id, user_name)
            
            if not member:
                self.publisher.reply_text(reply_token, "❌ 無法取得會員資料，請稍後再試", user_id, event)
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
                    print(f"⚠️ 日期格式化失敗: {str(e)}")
                    created_at_str = "未知"
            else:
                created_at_str = "未知"
            
            response = f"""👤 會員資訊

📝 姓名：{display_name}
🆔 ID：{user_id[:8]}...
💎 剩餘點數：{points} 點
📊 會員狀態：{status_text}
📅 註冊日期：{created_at_str}

輸入「點數」查看點數
輸入「歷史」查看交易記錄"""
            
            self.publisher.reply_text(reply_token, response, user_id, event)
            return "OK"
            
        except Exception as e:
            print(f"❌ 查詢會員資訊失敗: {str(e)}")
            import traceback
            traceback.print_exc()
            self.publisher.reply_text(reply_token, "❌ 查詢失敗，請稍後再試", user_id, event)
            return "OK"


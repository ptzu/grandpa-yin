"""解析用戶顯示名稱的共用邏輯。

抽出來讓「功能層」(base_feature.get_user_name) 與「發送層」(MessagePublisher
的問候前綴) 共用同一份規則，不必各自複製一遍。
"""
from src.core.app_logger import get_logger

logger = get_logger("display_name")

# member_service / LINE profile 取不到名字時回的預設值
FALLBACK_NAME = "使用者"


def resolve_display_name(user_id, member_service, line):
    """會員資料優先，退回 LINE profile。

    回傳真正的名字；都取不到（或只拿到預設值）時回 None，讓呼叫端自行決定
    要用什麼 fallback（例如問候語就退成沒有名字的「Hi 😊」）。
    """
    if not user_id:
        return None

    if member_service:
        try:
            member = member_service.get_member_info(user_id)
            if member and member.get("display_name") and member["display_name"] != FALLBACK_NAME:
                return member["display_name"]
        except Exception as e:
            logger.warning(f"讀取會員名稱失敗：{str(e)}")

    try:
        name = line.get_display_name(user_id)
    except Exception as e:
        logger.warning(f"讀取 LINE 名稱失敗：{str(e)}")
        return None

    if name and name != FALLBACK_NAME:
        return name
    return None

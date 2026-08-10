"""Replicate API client.

Kept out of the feature layer so a feature only says *which* model and *what*
input; how Replicate is called, and the several shapes its output can take, live
here. Swapping in another provider means another client with the same `run()`,
not another feature base class.
"""
import base64

import replicate

from src.core.app_logger import get_logger

logger = get_logger("replicate_client")

INSUFFICIENT_CREDIT_MESSAGE = (
    "Replicate 點數不足，請前往 https://replicate.com/account/billing 儲值"
)


class ReplicateClient:
    """Calls a Replicate model and resolves its output to an image URL."""

    def run(self, model: str, input_dict: dict) -> str:
        logger.debug(f"呼叫模型: {model}, input keys: {list(input_dict.keys())}")
        try:
            output = replicate.run(model, input=input_dict)
        except Exception as e:
            logger.error(f"Replicate API 錯誤: {str(e)}")
            if "Insufficient credit" in str(e):
                raise Exception(INSUFFICIENT_CREDIT_MESSAGE) from e
            raise

        logger.debug(f"API 回應類型: {type(output)}, 內容: {output}")
        url = self.extract_output_url(output)
        if not url:
            raise Exception("Replicate API 沒有回傳結果")
        return url

    @staticmethod
    def extract_output_url(output):
        """從 Replicate 回傳值解析 URL（支援字串 / 列表 / FileOutput 物件）"""
        if not output:
            return None
        if isinstance(output, str):
            return output
        if isinstance(output, list):
            return ReplicateClient.extract_output_url(output[0]) if output else None
        url_attr = getattr(output, 'url', None)
        if url_attr is not None:
            return url_attr() if callable(url_attr) else url_attr
        return str(output)

    @staticmethod
    def image_to_data_url(image_bytes: bytes) -> str:
        """將圖片 bytes 轉為 Replicate 接受的 base64 data URL"""
        image_b64 = base64.b64encode(image_bytes).decode('utf-8')
        return f"data:image/jpeg;base64,{image_b64}"

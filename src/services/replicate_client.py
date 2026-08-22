"""Replicate API client.

Kept out of the feature layer so a feature only says *which* model and *what*
input; how Replicate is called, and the several shapes its output can take, live
here. Swapping in another provider means another client with the same `run()`,
not another feature base class.

Waiting is deliberately hand-rolled rather than left to `replicate.run()`: that
helper ends in `prediction.wait()`, which polls without any upper bound. A model
that never finishes would hold an image worker forever, and with only a handful
of workers (see core/task_executor.py) a few stuck jobs are enough to take the
whole bot down while every log line still looks healthy.
"""
import base64
import time

import httpx
import replicate

from src.core.app_logger import get_logger
from src.core.settings import DEFAULT_TIMEOUT_SECONDS

logger = get_logger("replicate_client")

INSUFFICIENT_CREDIT_MESSAGE = (
    "Replicate 點數不足，請前往 https://replicate.com/account/billing 儲值"
)

# 每個功能的等待上限來自設定檔（config/settings.yml 的 timeout_seconds）；
# DEFAULT_TIMEOUT_SECONDS 只是呼叫端沒指定時的保險，免得漏傳就變回無限等。

# 單一 HTTP 請求的上限。整體時限擋不住另一種吊死：連線建立後對方不再說話，
# httpx 預設會無限期等下去，連輪詢迴圈都回不來。
_HTTP_TIMEOUT_SECONDS = 30.0

# 輪詢間隔。模型動輒數十秒，問太密只是浪費 API 額度。
_POLL_INTERVAL_SECONDS = 2.0

_TERMINAL_STATUSES = ("succeeded", "failed", "canceled")


class ReplicateTimeout(Exception):
    """模型在時限內沒有回應。

    對呼叫端而言這就是一次失敗，走 billing 既有的例外路徑：自動退點、告知
    用戶、在 usage_logs 留下 failed。不需要任何特別處理。
    """


class ReplicateClient:
    """Calls a Replicate model and resolves its output to an image URL."""

    def __init__(self):
        # 自建 client 只為了掛上 HTTP timeout；模組層的 replicate.run() 用的是
        # 沒有時限的預設 client。api_token 仍由 REPLICATE_API_TOKEN 提供。
        self._client = replicate.Client(
            timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS)
        )

    def run(self, model: str, input_dict: dict,
            timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
        logger.debug(f"呼叫模型: {model}, input keys: {list(input_dict.keys())}")
        try:
            prediction = self._create_prediction(model, input_dict)
            output = self._wait_for_output(prediction, model, timeout)
        except ReplicateTimeout:
            # 已經是說得清楚的失敗，不要被下面的通用轉譯蓋掉
            raise
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

    def _create_prediction(self, model: str, input_dict: dict):
        """建立 prediction 但不等它跑完，等待交由 _wait_for_output 控時。

        模型 ID 兩種寫法都收：`作者/模型名` 與 `作者/模型名:版本`。設定檔目前
        用的是前者（見 config/settings.yml），釘版本時會變成後者。
        """
        ref, _, version_id = model.partition(":")
        owner, _, name = ref.partition("/")

        if version_id:
            return self._client.predictions.create(
                version=version_id, input=input_dict
            )
        return self._client.models.predictions.create(
            model=(owner, name), input=input_dict
        )

    def _wait_for_output(self, prediction, model: str, timeout: int):
        """輪詢到終態；超過時限就取消並拋 ReplicateTimeout。"""
        deadline = time.monotonic() + timeout

        while prediction.status not in _TERMINAL_STATUSES:
            if time.monotonic() >= deadline:
                self._cancel(prediction)
                raise ReplicateTimeout(
                    f"模型 {model} 超過 {timeout} 秒仍未完成"
                    f"（prediction {prediction.id}，狀態 {prediction.status}）"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
            prediction.reload()

        if prediction.status != "succeeded":
            raise Exception(
                f"Replicate 未能完成（狀態 {prediction.status}）: {prediction.error}"
            )
        return prediction.output

    @staticmethod
    def _cancel(prediction):
        """取消逾時的工作。

        取消是為了不讓 Replicate 繼續跑一件已經沒人要的工作——那是照樣計費的。
        取消失敗不影響退點，記一筆就好。
        """
        try:
            prediction.cancel()
            logger.warning(f"已取消逾時的 prediction: {prediction.id}")
        except Exception:
            logger.warning(
                f"取消逾時的 prediction 失敗: {prediction.id}", exc_info=True
            )

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

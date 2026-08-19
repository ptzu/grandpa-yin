"""ECPay (綠界) AIO checkout: build the order form, verify the callback.

No network calls happen here. ECPay's checkout is a browser form POST, and its
result comes back to us as an HTTP callback — so this module only has to get
two things right: the parameters we send, and the signature check on what comes
back.

**CheckMacValue is the whole security model.** ECPay's callback is an ordinary
unauthenticated POST to a public URL; without verifying the signature, anyone
who finds the endpoint can grant themselves points. Every callback path must go
through `verify_callback` before anything is credited.

⚠️ The encoding rules below mirror .NET's UrlEncode, which is what ECPay's own
SDK uses, and they are easy to get subtly wrong. Verify against ECPay's test
environment before taking real money — a mismatch shows up as every payment
failing, which is at least loud rather than silent.

Credentials come from the environment, never from config/settings.yml: that
file is in git, and HashKey/HashIV are secrets.
"""
import hashlib
import os
from urllib.parse import quote_plus

from src.core.app_logger import get_logger

logger = get_logger("ecpay")

# ECPay's spec: .NET UrlEncode leaves these unescaped, Python's quote_plus does not.
_UNESCAPE = (
    ('%2d', '-'), ('%5f', '_'), ('%2e', '.'), ('%21', '!'),
    ('%2a', '*'), ('%28', '('), ('%29', ')'),
)

MAC_FIELD = 'CheckMacValue'


def check_mac_value(params, hash_key, hash_iv):
    """The signature ECPay expects for `params` (which must not include the
    signature field itself).

    Steps, per ECPay's spec: sort keys case-insensitively, wrap the query string
    with HashKey/HashIV, URL-encode, lowercase, SHA256, uppercase.
    """
    payload = {k: v for k, v in params.items() if k != MAC_FIELD}
    ordered = "&".join(
        f"{k}={payload[k]}" for k in sorted(payload, key=lambda k: k.lower())
    )
    raw = f"HashKey={hash_key}&{ordered}&HashIV={hash_iv}"

    encoded = quote_plus(raw).lower()
    for escaped, plain in _UNESCAPE:
        encoded = encoded.replace(escaped, plain)

    return hashlib.sha256(encoded.encode('utf-8')).hexdigest().upper()


def verify_callback(params, hash_key, hash_iv):
    """True when the payload carries a signature matching its own contents.

    Returns False rather than raising: a bad signature is an expected event
    (probes, misconfiguration), not an exception, and the caller must simply
    refuse to credit.
    """
    received = params.get(MAC_FIELD)
    if not received:
        logger.warning("回調缺少 CheckMacValue，拒絕處理")
        return False

    expected = check_mac_value(params, hash_key, hash_iv)
    # Compare case-insensitively: the spec says uppercase, but don't fail a
    # legitimate callback over letter case.
    if received.strip().upper() != expected:
        logger.warning("回調 CheckMacValue 不符，拒絕處理")
        return False
    return True


class ECPayClient:
    """Credentials + the two operations that need them."""

    def __init__(self, merchant_id, hash_key, hash_iv, api_url):
        self.merchant_id = merchant_id
        self._hash_key = hash_key
        self._hash_iv = hash_iv
        self.api_url = api_url

    @classmethod
    def from_env(cls):
        """Build from ECPAY_* environment variables, or None when unset.

        None means "top-up not configured" — the app must still boot, since
        every existing deployment runs without these.
        """
        merchant_id = os.getenv("ECPAY_MERCHANT_ID")
        hash_key = os.getenv("ECPAY_HASH_KEY")
        hash_iv = os.getenv("ECPAY_HASH_IV")
        api_url = os.getenv("ECPAY_API_URL")

        if not all([merchant_id, hash_key, hash_iv, api_url]):
            logger.info("ECPay 環境變數未設定完整，儲值功能停用")
            return None

        return cls(merchant_id, hash_key, hash_iv, api_url)

    def checkout_params(self, *, merchant_trade_no, trade_date, amount, item_name,
                        trade_desc, return_url, order_result_url, client_back_url=None,
                        choose_payment="ALL"):
        """The full set of form fields to POST to ECPay, signature included.

        Args:
            trade_date: "yyyy/MM/dd HH:mm:ss", ECPay's required format
            return_url: server-to-server callback — the only place points are
                granted
            order_result_url: where the user's browser lands afterwards; display
                only, never trusted for crediting
        """
        params = {
            'MerchantID': self.merchant_id,
            'MerchantTradeNo': merchant_trade_no,
            'MerchantTradeDate': trade_date,
            'PaymentType': 'aio',
            'TotalAmount': str(amount),
            'TradeDesc': trade_desc,
            'ItemName': item_name,
            'ReturnURL': return_url,
            'OrderResultURL': order_result_url,
            'ChoosePayment': choose_payment,
            'EncryptType': '1',   # SHA256
        }
        if client_back_url:
            params['ClientBackURL'] = client_back_url

        params[MAC_FIELD] = check_mac_value(params, self._hash_key, self._hash_iv)
        return params

    def verify(self, params):
        """Signature check for an incoming callback."""
        return verify_callback(params, self._hash_key, self._hash_iv)

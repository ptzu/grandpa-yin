#!/usr/bin/env bash
#
# 本地開發環境一鍵啟動：Flask + ngrok + 自動設定並驗證 LINE webhook
#
#   ./start_local_server.sh
#
# 按 Ctrl+C 會一併收掉 Flask 與 ngrok（含 Flask debug reloader 的子行程）。
# 相容 macOS 內建的 bash 3.2，不使用關聯陣列等新語法。

set -euo pipefail

# 專案根目錄；一律切過去，從任何位置呼叫都能 import 到 src 套件
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

NGROK_API="http://localhost:4040/api/tunnels"

FLASK_PID=""
NGROK_PID=""
BODY_FILE=""
CLEANED=0

# ---------------------------------------------------------------- 工具

die() { printf '❌ %s\n' "$*" >&2; exit 1; }

# 收掉一個行程與它的子行程。Flask 開 debug 時會 fork 出 reloader 子行程，
# 只殺父行程會留下佔著 port 的孤兒。
kill_tree() {
  local pid="$1"
  [ -n "$pid" ] || return 0
  pkill -P "$pid" 2>/dev/null || true
  kill "$pid" 2>/dev/null || true
}

cleanup() {
  [ "$CLEANED" = "1" ] && return 0
  CLEANED=1
  [ -n "$BODY_FILE" ] && rm -f "$BODY_FILE"
  if [ -n "$NGROK_PID" ] || [ -n "$FLASK_PID" ]; then
    printf '\n🧹 清理資源...\n'
  fi
  if [ -n "$NGROK_PID" ]; then kill_tree "$NGROK_PID"; printf '✅ ngrok 隧道已停止\n'; fi
  if [ -n "$FLASK_PID" ]; then kill_tree "$FLASK_PID"; printf '✅ Flask 應用程式已停止\n'; fi
}
# 只掛 EXIT，INT/TERM 轉成 exit，確保 cleanup 只跑一次
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 讀 .env。比照 python-dotenv 的預設語意：**已存在的環境變數優先**，不被 .env 覆蓋。
# 逐行解析而非 `source`，避免 .env 的內容被當成 shell 指令執行。
load_dotenv() {
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"                                  # 容忍 CRLF
    line="${line#"${line%%[![:space:]]*}"}"               # 去前導空白
    case "$line" in ''|'#'*) continue ;; esac
    line="${line#export }"
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    key="${key//[[:space:]]/}"
    value="${value#"${value%%[![:space:]]*}"}"            # 去 value 前導空白
    case "$key" in
      ''|*[!A-Za-z0-9_]*) continue ;;                     # 不是合法變數名就略過
    esac
    # 帶引號 → 取引號內內容（內含的 # 保留，引號外的註解一併丟掉）
    # 未帶引號 → 在「空白 + #」處砍掉行內註解（abc#def 這種不算註解，保留）
    case "$value" in
      \"*) value="${value#\"}"; value="${value%%\"*}" ;;
      \'*) value="${value#\'}"; value="${value%%\'*}" ;;
      *)
        case "$value" in
          *[[:space:]]\#*) value="${value%%[[:space:]]\#*}" ;;
        esac
        value="${value%"${value##*[![:space:]]}"}"        # 去 value 尾端空白
        ;;
    esac
    [ -n "${!key:-}" ] && continue                        # 環境變數已設定則不覆蓋
    export "$key=$value"
  done < .env
}

# ---------------------------------------------------------------- 1. 依賴檢查

printf '🤖 LINE Bot 本地測試啟動器\n'
printf '==================================================\n'
printf '🔍 檢查依賴...\n'

[ -f .env ] || die "找不到 .env，請先 cp .env.example .env 並填入測試環境的值"
load_dotenv

PORT="${PORT:-5000}"

missing=""
for var in CHANNEL_ACCESS_TOKEN CHANNEL_SECRET REPLICATE_API_TOKEN; do
  [ -n "${!var:-}" ] || missing="$missing $var"
done
# 變數一律加大括號：bash 3.2 在非 UTF-8 locale 下，會把緊接其後的全形字元
# 前導位元組吃進變數名（$missing（… 會被解讀成變數 missing\xef…）
[ -z "$missing" ] || die "缺少環境變數:${missing}（請檢查 .env）"

command -v ngrok >/dev/null 2>&1 || die "ngrok 未安裝，請執行 brew install --cask ngrok"

# macOS 的「隔空播放接收器」預設佔用 5000，是最常見的啟動失敗原因
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  printf '⚠️  port %s 已被佔用：\n' "$PORT"
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | tail -n +2 | sed 's/^/     /'
  printf '   （macOS 請確認「系統設定 → 一般 → 隔空播放接收器」是否關閉，或在 .env 改 PORT）\n'
  die "請先釋放 port $PORT 再重試"
fi

printf '✅ 所有依賴檢查完成\n'

# ---------------------------------------------------------------- 2. 啟動 Flask

printf '🚀 啟動 Flask 應用程式...\n'
FLASK_DEBUG=true python3 -m src.app &
FLASK_PID=$!

# 輪詢到真的起來為止（取代固定 sleep）。webhook 只收 POST，GET 回 405 即代表就緒。
flask_ready=0
for ((i = 0; i < 30; i++)); do
  if ! kill -0 "$FLASK_PID" 2>/dev/null; then
    die "Flask 啟動失敗（行程已結束，請看上方錯誤訊息）"
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/webhook" 2>/dev/null || true)"
  if [ "$code" = "405" ]; then
    flask_ready=1
    break
  fi
  sleep 1
done
[ "$flask_ready" = "1" ] || die "Flask 30 秒內沒有回應，請看上方錯誤訊息"
printf '✅ Flask 應用程式已啟動 (PID: %s, port %s)\n' "$FLASK_PID" "$PORT"

# ---------------------------------------------------------------- 3. 啟動 ngrok

if [ -n "${NGROK_DOMAIN:-}" ]; then
  printf '🌐 啟動 ngrok 隧道（固定網域: %s）...\n' "$NGROK_DOMAIN"
  ngrok http "--url=${NGROK_DOMAIN}" "$PORT" >/dev/null 2>&1 &
else
  printf '🌐 啟動 ngrok 隧道（隨機網址；設定 NGROK_DOMAIN 可固定網址）...\n'
  ngrok http "$PORT" >/dev/null 2>&1 &
fi
NGROK_PID=$!

WEBHOOK_BASE=""
for ((i = 0; i < 20; i++)); do
  if ! kill -0 "$NGROK_PID" 2>/dev/null; then
    die "ngrok 啟動失敗（未設 authtoken 時請先執行 ngrok config add-authtoken <token>）"
  fi
  WEBHOOK_BASE="$(
    curl -fsS "$NGROK_API" 2>/dev/null | python3 -c 'import json,sys
try:
    tunnels = json.load(sys.stdin).get("tunnels") or []
except Exception:
    sys.exit(0)
print(tunnels[0]["public_url"] if tunnels else "")' 2>/dev/null || true
  )"
  [ -n "$WEBHOOK_BASE" ] && break
  sleep 1
done
[ -n "$WEBHOOK_BASE" ] || die "20 秒內取不到 ngrok 公開網址"
printf '✅ ngrok 隧道已啟動: %s\n' "$WEBHOOK_BASE"

# ---------------------------------------------------------------- 4. 設定並驗證 webhook

WEBHOOK_ENDPOINT="${WEBHOOK_BASE}/webhook"
PAYLOAD="$(printf '{"endpoint":"%s"}' "$WEBHOOK_ENDPOINT")"
AUTH_HEADER="Authorization: Bearer ${CHANNEL_ACCESS_TOKEN}"
BODY_FILE="$(mktemp)"

printf '🔗 Webhook URL: %s\n' "$WEBHOOK_ENDPOINT"

status="$(curl -s -o "$BODY_FILE" -w '%{http_code}' \
  -X PUT https://api.line.me/v2/bot/channel/webhook/endpoint \
  -H 'Content-Type: application/json' -H "$AUTH_HEADER" \
  --data "$PAYLOAD" --max-time 10 || true)"

if [ "$status" != "200" ]; then
  printf '❌ 自動設定 webhook 失敗: %s %s\n' "$status" "$(cat "$BODY_FILE")" >&2
  die "請確認 CHANNEL_ACCESS_TOKEN 是否為測試 channel 的值"
fi
printf '✅ Webhook URL 已自動設定到 LINE\n'

# 給 Flask 一點時間完全就緒，再讓 LINE 打過來測試
sleep 1
test_body="$(curl -fsS -X POST https://api.line.me/v2/bot/channel/webhook/test \
  -H 'Content-Type: application/json' -H "$AUTH_HEADER" \
  --data "$PAYLOAD" --max-time 15 2>/dev/null || true)"

printf '%s' "$test_body" | python3 -c 'import json,sys
raw = sys.stdin.read()
if not raw:
    print("⚠️  無法驗證 webhook（不影響已設定的結果）")
    sys.exit(0)
try:
    result = json.loads(raw)
except Exception:
    print("⚠️  無法解析驗證回應（不影響已設定的結果）")
    sys.exit(0)
# 用 % 格式化而非 f-string：f-string 的表達式在 Python 3.9 不能含跳脫引號
if result.get("success"):
    print("✅ Webhook 驗證成功（回應狀態碼 %s）" % result.get("statusCode"))
else:
    print("⚠️  Webhook 驗證未通過: %s" % (result.get("reason", result),))
    print("   （可能 Flask 剛啟動還沒就緒，先傳訊息測試看看，不行再重跑）")'

# ---------------------------------------------------------------- 5. 待命

cat <<'EOF'

🎉 本地測試環境已準備就緒!
現在你可以:
1. 用手機 LINE App 掃描 Console 的 QR Code 加入你的 Bot
2. 執行 python3 test/test_local.py 進行本地測試
3. 修改程式碼時 Flask 會自動重載，無需手動重啟
4. 按 Ctrl+C 停止測試環境

EOF

# 前景等待 Flask；Ctrl+C 觸發 trap 收掉兩個行程
wait "$FLASK_PID" || true

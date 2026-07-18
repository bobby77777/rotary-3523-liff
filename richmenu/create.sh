#!/bin/bash
# Deploy a single 3523 rich menu: the whole image is one button that opens the LIFF.
set -euo pipefail
cd "$(dirname "$0")"
source .env

LINE_API="https://api.line.me/v2/bot"
DATA_API="https://api-data.line.me/v2/bot"
AUTH="Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN"

IMAGE="richmenu.png"          # 2500x843 (compact)
LIFF="https://liff.line.me/2010535285-kh8rJmpS"

# ── Helpers ───────────────────────────────────────────────────────────────────

menu_create() {
  curl -sf -X POST "$LINE_API/richmenu" \
    -H "$AUTH" -H "Content-Type: application/json" \
    -d "$1" | python3 -c "import sys,json; print(json.load(sys.stdin)['richMenuId'])"
}

menu_upload() {
  curl -sf -X POST "$DATA_API/richmenu/$1/content" \
    -H "$AUTH" -H "Content-Type: image/png" \
    --data-binary "@$2"
}

set_default() {
  curl -sf -X POST "$LINE_API/user/all/richmenu/$1" -H "$AUTH" -H "Content-Length: 0"
  echo "  Default rich menu set to $1"
}

# ── Step 1: Create the menu ───────────────────────────────────────────────────
echo "=== Creating rich menu ==="
MENU_ID=$(menu_create "$(cat <<EOF
{
  "size": {"width": 2500, "height": 843},
  "selected": true,
  "name": "3523-liff",
  "chatBarText": "點擊進入地區、社務整合",
  "areas": [
    {
      "bounds": {"x": 0, "y": 0, "width": 2500, "height": 843},
      "action": {"type": "uri", "label": "進入", "uri": "$LIFF"}
    }
  ]
}
EOF
)")
echo "  Menu id: $MENU_ID"

# ── Step 2: Upload the image ──────────────────────────────────────────────────
echo ""
echo "=== Uploading image ==="
menu_upload "$MENU_ID" "$IMAGE" && echo "  Uploaded $IMAGE"

# ── Step 3: Set as default ────────────────────────────────────────────────────
echo ""
echo "=== Setting default rich menu ==="
set_default "$MENU_ID"

# ── Step 4: Clean up every other menu (and old aliases) ───────────────────────
echo ""
echo "=== Cleaning up old menus ==="
for alias in \
  richmenu-3523-home richmenu-3523-profile richmenu-3523-ebooks richmenu-3523-admin \
  richmenu-3523c-home richmenu-3523c-profile richmenu-3523c-ebooks richmenu-3523c-admin; do
  curl -s -X DELETE "$LINE_API/richmenu/alias/$alias" -H "$AUTH" > /dev/null 2>&1 || true
done

ALL_IDS=$(curl -sf "$LINE_API/richmenu/list" -H "$AUTH" | \
  python3 -c "import sys,json; [print(m['richMenuId']) for m in json.load(sys.stdin)['richmenus']]")

while IFS= read -r mid; do
  if [[ -n "$mid" && "$mid" != "$MENU_ID" ]]; then
    curl -sf -X DELETE "$LINE_API/richmenu/$mid" -H "$AUTH" && \
      echo "  Deleted $mid" || echo "  Failed to delete $mid"
  fi
done <<< "$ALL_IDS"

echo ""
echo "Done. Single rich menu: $MENU_ID → $LIFF"

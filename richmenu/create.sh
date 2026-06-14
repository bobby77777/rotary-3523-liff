#!/bin/bash
# Create 3523 District LINE rich menus (3 tabs: home / profile / admin)
set -euo pipefail
cd "$(dirname "$0")"
source .env

LINE_API="https://api.line.me/v2/bot"
DATA_API="https://api-data.line.me/v2/bot"
AUTH="Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN"

ALIAS_HOME="richmenu-3523-home"
ALIAS_PROFILE="richmenu-3523-profile"
ALIAS_ADMIN="richmenu-3523-admin"

# ── Helpers ───────────────────────────────────────────────────────────────────

menu_create() {
  curl -sf -X POST "$LINE_API/richmenu" \
    -H "$AUTH" -H "Content-Type: application/json" \
    -d "$1" | python3 -c "import sys,json; print(json.load(sys.stdin)['richMenuId'])"
}

menu_upload() {
  curl -sf -X POST "$DATA_API/richmenu/$1/content" \
    -H "$AUTH" -H "Content-Type: image/jpeg" \
    --data-binary "@$2"
}

alias_upsert() {
  local alias_id="$1" menu_id="$2"
  # Delete old alias first (silently ignore 404 if it doesn't exist yet)
  curl -s -X DELETE "$LINE_API/richmenu/alias/$alias_id" -H "$AUTH" > /dev/null 2>&1 || true
  # Create fresh alias
  curl -sf -X POST "$LINE_API/richmenu/alias" \
    -H "$AUTH" -H "Content-Type: application/json" \
    -d "{\"richMenuAliasId\":\"$alias_id\",\"richMenuId\":\"$menu_id\"}" > /dev/null
  echo "  Set alias $alias_id → $menu_id"
}

set_default() {
  curl -sf -X POST "$LINE_API/user/all/richmenu/$1" -H "$AUTH"
  echo "  Default rich menu set to $1"
}

# ── Tab-bar areas (shared across all 3 menus) ─────────────────────────────────
# x: 0–832 = Home, 833–1665 = Profile, 1666–2499 = Admin
# y: 0–174 = tab bar, 175–1685 = content

tab_areas() {
  cat <<EOF
    {
      "bounds": {"x": 0,    "y": 0, "width": 833, "height": 175},
      "action": {"type": "richmenuswitch", "richMenuAliasId": "$ALIAS_HOME",    "data": "tab=home"}
    },
    {
      "bounds": {"x": 833,  "y": 0, "width": 833, "height": 175},
      "action": {"type": "richmenuswitch", "richMenuAliasId": "$ALIAS_PROFILE", "data": "tab=profile"}
    },
    {
      "bounds": {"x": 1666, "y": 0, "width": 834, "height": 175},
      "action": {"type": "richmenuswitch", "richMenuAliasId": "$ALIAS_ADMIN",   "data": "tab=admin"}
    }
EOF
}

# ── Step 1: Generate images ───────────────────────────────────────────────────
echo "=== Generating rich menu images ==="
python3 generate_image.py

# ── Step 2: Create menus ──────────────────────────────────────────────────────
echo ""
echo "=== Creating rich menus ==="

HOME_MENU=$(menu_create "$(cat <<EOF
{
  "size": {"width": 2500, "height": 1686},
  "selected": true,
  "name": "3523-home",
  "chatBarText": "選單開啟 / 關閉",
  "areas": [
    $(tab_areas),
    {
      "bounds": {"x": 0,    "y": 175, "width": 1666, "height": 1511},
      "action": {"type": "postback", "label": "首頁大螢幕", "data": "action=banner"}
    },
    {
      "bounds": {"x": 1666, "y": 175, "width": 834, "height": 755},
      "action": {"type": "postback", "label": "年度行事曆", "data": "action=calendar"}
    },
    {
      "bounds": {"x": 1666, "y": 930, "width": 834, "height": 756},
      "action": {"type": "uri", "label": "地區官網", "uri": "https://www.rotary3523.org.tw"}
    }
  ]
}
EOF
)")
echo "  HOME menu id: $HOME_MENU"

PROFILE_MENU=$(menu_create "$(cat <<EOF
{
  "size": {"width": 2500, "height": 1686},
  "selected": true,
  "name": "3523-profile",
  "chatBarText": "選單開啟 / 關閉",
  "areas": [
    $(tab_areas),
    {
      "bounds": {"x": 0,    "y": 175, "width": 1666, "height": 1511},
      "action": {"type": "postback", "label": "我的報到QR", "data": "action=qrcode"}
    },
    {
      "bounds": {"x": 1666, "y": 175, "width": 834, "height": 755},
      "action": {"type": "postback", "label": "報名繳費紀錄", "data": "action=history"}
    },
    {
      "bounds": {"x": 1666, "y": 930, "width": 834, "height": 756},
      "action": {"type": "postback", "label": "上傳繳費憑證", "data": "action=upload"}
    }
  ]
}
EOF
)")
echo "  PROFILE menu id: $PROFILE_MENU"

ADMIN_MENU=$(menu_create "$(cat <<EOF
{
  "size": {"width": 2500, "height": 1686},
  "selected": true,
  "name": "3523-admin",
  "chatBarText": "選單開啟 / 關閉",
  "areas": [
    $(tab_areas),
    {
      "bounds": {"x": 0,    "y": 175, "width": 1666, "height": 1511},
      "action": {"type": "postback", "label": "統計報表", "data": "action=statistics"}
    },
    {
      "bounds": {"x": 1666, "y": 175, "width": 834, "height": 755},
      "action": {"type": "postback", "label": "現場報到相機", "data": "action=scanner"}
    },
    {
      "bounds": {"x": 1666, "y": 930, "width": 834, "height": 756},
      "action": {"type": "postback", "label": "秘書處支援",   "data": "action=support"}
    }
  ]
}
EOF
)")
echo "  ADMIN menu id: $ADMIN_MENU"

# ── Step 3: Upload images ─────────────────────────────────────────────────────
echo ""
echo "=== Uploading images ==="
menu_upload "$HOME_MENU"    richmenu_home.jpg    && echo "  Uploaded home image"
menu_upload "$PROFILE_MENU" richmenu_profile.jpg && echo "  Uploaded profile image"
menu_upload "$ADMIN_MENU"   richmenu_admin.jpg   && echo "  Uploaded admin image"

# ── Step 4: Create / update aliases ──────────────────────────────────────────
echo ""
echo "=== Setting up aliases ==="
alias_upsert "$ALIAS_HOME"    "$HOME_MENU"
alias_upsert "$ALIAS_PROFILE" "$PROFILE_MENU"
alias_upsert "$ALIAS_ADMIN"   "$ADMIN_MENU"

# ── Step 5: Set default (home tab) ───────────────────────────────────────────
echo ""
echo "=== Setting default rich menu ==="
set_default "$HOME_MENU"

# ── Step 6: Clean up old menus ───────────────────────────────────────────────
echo ""
echo "=== Cleaning up old menus ==="
KEEP="$HOME_MENU $PROFILE_MENU $ADMIN_MENU"
ALL_IDS=$(curl -sf "$LINE_API/richmenu/list" -H "$AUTH" | \
  python3 -c "import sys,json; [print(m['richMenuId']) for m in json.load(sys.stdin)['richmenus']]")

while IFS= read -r mid; do
  if [[ -n "$mid" && ! " $KEEP " =~ " $mid " ]]; then
    curl -sf -X DELETE "$LINE_API/richmenu/$mid" -H "$AUTH" && \
      echo "  Deleted $mid" || echo "  Failed to delete $mid"
  fi
done <<< "$ALL_IDS"

echo ""
echo "Done. Menu IDs:"
echo "  HOME:    $HOME_MENU  (alias: $ALIAS_HOME)"
echo "  PROFILE: $PROFILE_MENU  (alias: $ALIAS_PROFILE)"
echo "  ADMIN:   $ADMIN_MENU  (alias: $ALIAS_ADMIN)"

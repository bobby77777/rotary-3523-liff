#!/bin/bash
# Create 3523 District LINE rich menus (4 tabs: home / profile / ebooks / admin)
set -euo pipefail
cd "$(dirname "$0")"
source .env

LINE_API="https://api.line.me/v2/bot"
DATA_API="https://api-data.line.me/v2/bot"
AUTH="Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN"

ALIAS_HOME="richmenu-3523-home"
ALIAS_PROFILE="richmenu-3523-profile"
ALIAS_EBOOKS="richmenu-3523-ebooks"
ALIAS_ADMIN="richmenu-3523-admin"

# Club-scope (社內專區) aliases
ALIAS_C_HOME="richmenu-3523c-home"
ALIAS_C_PROFILE="richmenu-3523c-profile"
ALIAS_C_EBOOKS="richmenu-3523c-ebooks"
ALIAS_C_ADMIN="richmenu-3523c-admin"

LIFF="https://liff.line.me/2010535285-kh8rJmpS"

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
  curl -s -X DELETE "$LINE_API/richmenu/alias/$alias_id" -H "$AUTH" > /dev/null 2>&1 || true
  curl -sf -X POST "$LINE_API/richmenu/alias" \
    -H "$AUTH" -H "Content-Type: application/json" \
    -d "{\"richMenuAliasId\":\"$alias_id\",\"richMenuId\":\"$menu_id\"}" > /dev/null
  echo "  Set alias $alias_id → $menu_id"
}

set_default() {
  curl -sf -X POST "$LINE_API/user/all/richmenu/$1" -H "$AUTH" -H "Content-Length: 0"
  echo "  Default rich menu set to $1"
}

# ── Tab-bar areas (shared across all 4 menus) ─────────────────────────────────
# 4 equal tabs: 0–624, 625–1249, 1250–1874, 1875–2499  (each 625px)
# y: 0–174 = tab bar, 175–1685 = content

tab_areas() {
  cat <<EOF
    {
      "bounds": {"x": 0,    "y": 0, "width": 625, "height": 175},
      "action": {"type": "richmenuswitch", "richMenuAliasId": "$ALIAS_HOME",    "data": "tab=home"}
    },
    {
      "bounds": {"x": 625,  "y": 0, "width": 625, "height": 175},
      "action": {"type": "richmenuswitch", "richMenuAliasId": "$ALIAS_PROFILE", "data": "tab=profile"}
    },
    {
      "bounds": {"x": 1250, "y": 0, "width": 625, "height": 175},
      "action": {"type": "richmenuswitch", "richMenuAliasId": "$ALIAS_EBOOKS",  "data": "tab=ebooks"}
    },
    {
      "bounds": {"x": 1875, "y": 0, "width": 625, "height": 175},
      "action": {"type": "richmenuswitch", "richMenuAliasId": "$ALIAS_ADMIN",   "data": "tab=admin"}
    }
EOF
}

# Club-scope tab bar: tapping tabs stays within the club menus.
tab_areas_club() {
  cat <<EOF
    {
      "bounds": {"x": 0,    "y": 0, "width": 625, "height": 175},
      "action": {"type": "richmenuswitch", "richMenuAliasId": "$ALIAS_C_HOME",    "data": "tab=home"}
    },
    {
      "bounds": {"x": 625,  "y": 0, "width": 625, "height": 175},
      "action": {"type": "richmenuswitch", "richMenuAliasId": "$ALIAS_C_PROFILE", "data": "tab=profile"}
    },
    {
      "bounds": {"x": 1250, "y": 0, "width": 625, "height": 175},
      "action": {"type": "richmenuswitch", "richMenuAliasId": "$ALIAS_C_EBOOKS",  "data": "tab=ebooks"}
    },
    {
      "bounds": {"x": 1875, "y": 0, "width": 625, "height": 175},
      "action": {"type": "richmenuswitch", "richMenuAliasId": "$ALIAS_C_ADMIN",   "data": "tab=admin"}
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
      "action": {"type": "postback", "label": "活動報名", "data": "action=event_list"}
    },
    {
      "bounds": {"x": 1666, "y": 175, "width": 834, "height": 755},
      "action": {"type": "uri", "label": "年度行事曆", "uri": "$LIFF?tab=home&action=calendar"}
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
      "action": {"type": "postback", "label": "個人中心", "data": "action=my_profile"}
    },
    {
      "bounds": {"x": 1666, "y": 175, "width": 834, "height": 755},
      "action": {"type": "postback", "label": "報名紀錄", "data": "action=registrations"}
    },
    {
      "bounds": {"x": 1666, "y": 930, "width": 834, "height": 756},
      "action": {"type": "uri", "label": "回報匯款", "uri": "$LIFF?tab=profile&action=payment"}
    }
  ]
}
EOF
)")
echo "  PROFILE menu id: $PROFILE_MENU"

EBOOKS_MENU=$(menu_create "$(cat <<EOF
{
  "size": {"width": 2500, "height": 1686},
  "selected": true,
  "name": "3523-ebooks",
  "chatBarText": "選單開啟 / 關閉",
  "areas": [
    $(tab_areas),
    {
      "bounds": {"x": 0,    "y": 175, "width": 833, "height": 1511},
      "action": {"type": "uri", "label": "活動手冊", "uri": "$LIFF?tab=ebooks&doc=handbook"}
    },
    {
      "bounds": {"x": 833,  "y": 175, "width": 833, "height": 1511},
      "action": {"type": "uri", "label": "年度成果冊", "uri": "$LIFF?tab=ebooks&doc=yearbook"}
    },
    {
      "bounds": {"x": 1666, "y": 175, "width": 834, "height": 1511},
      "action": {"type": "uri", "label": "捐贈報告", "uri": "$LIFF?tab=ebooks&doc=donation"}
    }
  ]
}
EOF
)")
echo "  EBOOKS menu id: $EBOOKS_MENU"

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
      "action": {"type": "postback", "label": "今日報到", "data": "action=today_checkin"}
    },
    {
      "bounds": {"x": 1666, "y": 175, "width": 834, "height": 755},
      "action": {"type": "uri", "label": "現場掃碼", "uri": "$LIFF?tab=admin&action=scanner"}
    },
    {
      "bounds": {"x": 1666, "y": 930, "width": 834, "height": 756},
      "action": {"type": "postback", "label": "管理選單", "data": "action=admin_menu"}
    }
  ]
}
EOF
)")
echo "  ADMIN menu id: $ADMIN_MENU"

# ── Club-scope (社內專區) menus ───────────────────────────────────────────────
echo ""
echo "=== Creating club-scope rich menus ==="

C_HOME_MENU=$(menu_create "$(cat <<EOF
{
  "size": {"width": 2500, "height": 1686},
  "selected": true,
  "name": "3523c-home",
  "chatBarText": "選單開啟 / 關閉",
  "areas": [
    $(tab_areas_club),
    {
      "bounds": {"x": 0,    "y": 175, "width": 1666, "height": 1511},
      "action": {"type": "postback", "label": "社內活動", "data": "action=event_list"}
    },
    {
      "bounds": {"x": 1666, "y": 175, "width": 834, "height": 755},
      "action": {"type": "uri", "label": "出席累計", "uri": "$LIFF?tab=home&scope=club&action=attendance"}
    },
    {
      "bounds": {"x": 1666, "y": 930, "width": 834, "height": 756},
      "action": {"type": "uri", "label": "本社官網", "uri": "$LIFF?tab=home&scope=club&action=website"}
    }
  ]
}
EOF
)")
echo "  CLUB HOME menu id: $C_HOME_MENU"

C_PROFILE_MENU=$(menu_create "$(cat <<EOF
{
  "size": {"width": 2500, "height": 1686},
  "selected": true,
  "name": "3523c-profile",
  "chatBarText": "選單開啟 / 關閉",
  "areas": [
    $(tab_areas_club),
    {
      "bounds": {"x": 0,    "y": 175, "width": 1666, "height": 1511},
      "action": {"type": "postback", "label": "個人中心", "data": "action=my_profile"}
    },
    {
      "bounds": {"x": 1666, "y": 175, "width": 834, "height": 755},
      "action": {"type": "postback", "label": "報名紀錄", "data": "action=registrations"}
    },
    {
      "bounds": {"x": 1666, "y": 930, "width": 834, "height": 756},
      "action": {"type": "uri", "label": "回報匯款", "uri": "$LIFF?tab=profile&scope=club&action=payment"}
    }
  ]
}
EOF
)")
echo "  CLUB PROFILE menu id: $C_PROFILE_MENU"

C_EBOOKS_MENU=$(menu_create "$(cat <<EOF
{
  "size": {"width": 2500, "height": 1686},
  "selected": true,
  "name": "3523c-ebooks",
  "chatBarText": "選單開啟 / 關閉",
  "areas": [
    $(tab_areas_club),
    {
      "bounds": {"x": 0,    "y": 175, "width": 833, "height": 1511},
      "action": {"type": "uri", "label": "社刊週報", "uri": "$LIFF?tab=ebooks&scope=club&doc=bulletin"}
    },
    {
      "bounds": {"x": 833,  "y": 175, "width": 833, "height": 1511},
      "action": {"type": "uri", "label": "活動花絮", "uri": "$LIFF?tab=ebooks&scope=club&doc=album"}
    },
    {
      "bounds": {"x": 1666, "y": 175, "width": 834, "height": 1511},
      "action": {"type": "uri", "label": "會議紀錄", "uri": "$LIFF?tab=ebooks&scope=club&doc=minutes"}
    }
  ]
}
EOF
)")
echo "  CLUB EBOOKS menu id: $C_EBOOKS_MENU"

C_ADMIN_MENU=$(menu_create "$(cat <<EOF
{
  "size": {"width": 2500, "height": 1686},
  "selected": true,
  "name": "3523c-admin",
  "chatBarText": "選單開啟 / 關閉",
  "areas": [
    $(tab_areas_club),
    {
      "bounds": {"x": 0,    "y": 175, "width": 1250, "height": 755},
      "action": {"type": "uri", "label": "社友出席率", "uri": "$LIFF?tab=admin&scope=club&action=attendance"}
    },
    {
      "bounds": {"x": 1250, "y": 175, "width": 1250, "height": 755},
      "action": {"type": "postback", "label": "社務對帳", "data": "action=admin_stub&f=club_finance"}
    },
    {
      "bounds": {"x": 0,    "y": 930, "width": 1250, "height": 756},
      "action": {"type": "postback", "label": "理監事專區", "data": "action=admin_stub&f=board"}
    },
    {
      "bounds": {"x": 1250, "y": 930, "width": 1250, "height": 756},
      "action": {"type": "uri", "label": "社友社費", "uri": "$LIFF?tab=admin&scope=club&action=dues"}
    }
  ]
}
EOF
)")
echo "  CLUB ADMIN menu id: $C_ADMIN_MENU"

# ── Step 3: Upload images ─────────────────────────────────────────────────────
echo ""
echo "=== Uploading images ==="
menu_upload "$HOME_MENU"    richmenu_home.jpg    && echo "  Uploaded home image"
menu_upload "$PROFILE_MENU" richmenu_profile.jpg && echo "  Uploaded profile image"
menu_upload "$EBOOKS_MENU"  richmenu_ebooks.jpg  && echo "  Uploaded ebooks image"
menu_upload "$ADMIN_MENU"   richmenu_admin.jpg   && echo "  Uploaded admin image"
menu_upload "$C_HOME_MENU"    richmenu_c_home.jpg    && echo "  Uploaded club home image"
menu_upload "$C_PROFILE_MENU" richmenu_c_profile.jpg && echo "  Uploaded club profile image"
menu_upload "$C_EBOOKS_MENU"  richmenu_c_ebooks.jpg  && echo "  Uploaded club ebooks image"
menu_upload "$C_ADMIN_MENU"   richmenu_c_admin.jpg   && echo "  Uploaded club admin image"

# ── Step 4: Create / update aliases ──────────────────────────────────────────
echo ""
echo "=== Setting up aliases ==="
alias_upsert "$ALIAS_HOME"      "$HOME_MENU"
alias_upsert "$ALIAS_PROFILE"   "$PROFILE_MENU"
alias_upsert "$ALIAS_EBOOKS"    "$EBOOKS_MENU"
alias_upsert "$ALIAS_ADMIN"     "$ADMIN_MENU"
alias_upsert "$ALIAS_C_HOME"    "$C_HOME_MENU"
alias_upsert "$ALIAS_C_PROFILE" "$C_PROFILE_MENU"
alias_upsert "$ALIAS_C_EBOOKS"  "$C_EBOOKS_MENU"
alias_upsert "$ALIAS_C_ADMIN"   "$C_ADMIN_MENU"

# ── Step 5: Set default (home tab) ───────────────────────────────────────────
echo ""
echo "=== Setting default rich menu ==="
set_default "$HOME_MENU"

# ── Step 6: Clean up old menus ───────────────────────────────────────────────
echo ""
echo "=== Cleaning up old menus ==="
KEEP="$HOME_MENU $PROFILE_MENU $EBOOKS_MENU $ADMIN_MENU $C_HOME_MENU $C_PROFILE_MENU $C_EBOOKS_MENU $C_ADMIN_MENU"
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
echo "  [District]"
echo "  HOME:    $HOME_MENU  (alias: $ALIAS_HOME)"
echo "  PROFILE: $PROFILE_MENU  (alias: $ALIAS_PROFILE)"
echo "  EBOOKS:  $EBOOKS_MENU  (alias: $ALIAS_EBOOKS)"
echo "  ADMIN:   $ADMIN_MENU  (alias: $ALIAS_ADMIN)"
echo "  [Club]"
echo "  HOME:    $C_HOME_MENU  (alias: $ALIAS_C_HOME)"
echo "  PROFILE: $C_PROFILE_MENU  (alias: $ALIAS_C_PROFILE)"
echo "  EBOOKS:  $C_EBOOKS_MENU  (alias: $ALIAS_C_EBOOKS)"
echo "  ADMIN:   $C_ADMIN_MENU  (alias: $ALIAS_C_ADMIN)"

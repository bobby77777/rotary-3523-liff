#!/bin/bash
source .env

ID=$(curl -s https://api.line.me/v2/bot/richmenu/list \
  -H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN" \
  | jq -r '.richmenus[0].richMenuId')

echo "Uploading image to rich menu: $ID"

curl -v -X POST "https://api-data.line.me/v2/bot/richmenu/$ID/content" \
-H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN" \
-H "Content-Type: image/jpeg" \
-T "$(dirname "$0")/rotary_richmenu.jpg"
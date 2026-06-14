#!/bin/bash
source .env

ID=$(curl -s https://api.line.me/v2/bot/richmenu/list \
  -H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN" \
  | jq -r '.richmenus[0].richMenuId')

echo "Setting default rich menu: $ID"

curl -v -X POST "https://api.line.me/v2/bot/user/all/richmenu/$ID" \
  -H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN" \
  -H "Content-Length: 0"

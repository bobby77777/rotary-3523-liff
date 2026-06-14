#!/bin/bash
source .env

IDS=$(curl -s https://api.line.me/v2/bot/richmenu/list \
  -H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN" \
  | jq -r '.richmenus[].richMenuId')

for id in $IDS; do
  curl -s -X DELETE https://api.line.me/v2/bot/richmenu/$id \
    -H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN"
  echo "Deleted $id"
done

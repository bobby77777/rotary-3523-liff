#!/bin/bash
source .env

curl -v -X GET https://api.line.me/v2/bot/richmenu/list \
-H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN"
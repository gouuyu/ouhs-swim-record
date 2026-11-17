# -*- coding: utf-8 -*-
"""LINE Messaging API へのブロードキャスト通知。

LINE_CHANNEL_ACCESS_TOKEN が未設定の場合は何もせずスキップする（GitHub Issue 方式
のみ使う場合や、LINE設定がまだ済んでいない場合でもエラーにならないようにするため）。
"""
import json
import os
import urllib.request

LINE_BROADCAST_API = "https://api.line.me/v2/bot/message/broadcast"
MAX_LINE_TEXT_LEN = 4800  # LINEのテキストメッセージは5000文字まで。安全マージンを取る


def send_line_broadcast(text):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        print("[line_notify] LINE_CHANNEL_ACCESS_TOKEN が未設定のため、LINE通知をスキップします。")
        return False

    if len(text) > MAX_LINE_TEXT_LEN:
        text = text[:MAX_LINE_TEXT_LEN] + "\n…(以下省略。詳細はGitHubを確認してください)"

    body = json.dumps({"messages": [{"type": "text", "text": text}]}).encode("utf-8")
    req = urllib.request.Request(LINE_BROADCAST_API, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[line_notify] LINE通知を送信しました (status={resp.status})")
            return True
    except Exception as e:
        print(f"[line_notify] LINE通知の送信に失敗しました: {e}")
        return False

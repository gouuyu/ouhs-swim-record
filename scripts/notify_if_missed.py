#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OUHS SWIM TEAM ロスター: 週次チェックの「見張り役」。

週次チェック（run_check.ps1 / check_and_update.py）は、実行予定時刻に
このパソコンの電源が入っていなければそもそも実行されない（意図的な仕様。
「スキップして翌週に繰り越す」という運用のため）。そのため、スクリプト
自身の中から「今週は実行されませんでした」と報告することはできない。

この check_missed_run.ps1 / notify_if_missed.py は、それとは別に
「パソコンを起動・ログインしたとき」に動くよう登録しておく、別のタスク。
最後に週次チェックが実際に動いた時刻（last_run.log の更新日時）を見て、
既定の週次スケジュールから見て明らかに間隔が空きすぎていたら
（既定: 8日以上。週1回・7日間隔に1日の猶予を足した値）、LINEで知らせる。

同じ「見逃し期間」について何度も重複通知しないよう、最後に通知した
last_run.log の更新時刻を状態ファイル（.watchdog_state.json）に記録し、
前回通知時から last_run.log が更新されていない場合は再通知しない
（＝パソコンを何度再起動しても、同じ見逃しについて毎回通知が来る、
ということは起きない）。

環境変数:
  LINE_CHANNEL_ACCESS_TOKEN  LINE Messaging APIのチャネルアクセストークン
                             （未設定ならログ出力のみでLINE通知はスキップ）
  WATCHDOG_THRESHOLD_DAYS    見逃し判定のしきい値（日数）。省略時 8
  LOG_PATH                   チェックするログファイルのパス。省略時 "last_run.log"
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from line_notify import send_line_broadcast  # noqa: E402

LOG_PATH = os.environ.get("LOG_PATH", "last_run.log")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".watchdog_state.json")
THRESHOLD_DAYS = float(os.environ.get("WATCHDOG_THRESHOLD_DAYS", "8"))


def log(msg):
    print(msg, flush=True)


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        log(f"状態ファイルの保存に失敗しました（通知自体には影響しません）: {e}")


def main():
    if not os.path.exists(LOG_PATH):
        log(f"{LOG_PATH} がまだ存在しません（週次チェックが一度も実行されていない可能性）。"
            "初回セットアップ直後であれば問題ありません。")
        return

    last_run_mtime = os.path.getmtime(LOG_PATH)
    last_run_dt = datetime.datetime.fromtimestamp(last_run_mtime)
    now = datetime.datetime.now()
    elapsed_days = (now - last_run_dt).total_seconds() / 86400.0

    log(f"最後に週次チェックが実行されたのは {last_run_dt:%Y-%m-%d %H:%M:%S}"
        f"（{elapsed_days:.1f}日前）です。しきい値: {THRESHOLD_DAYS}日。")

    if elapsed_days <= THRESHOLD_DAYS:
        log("しきい値以内なので、通知は不要です。")
        return

    state = load_state()
    if state.get("last_notified_for_mtime") == last_run_mtime:
        log("この見逃し期間については既に通知済みのため、再通知はしません"
            "（次回、週次チェックが実際に実行されればリセットされます）。")
        return

    text = (
        "⚠️ OUHS SWIM TEAM ロスター自動チェック\n"
        f"前回の実行から{elapsed_days:.1f}日経過しています"
        f"（前回実行: {last_run_dt:%Y-%m-%d %H:%M}）。\n"
        "この間、パソコンの電源が入っていなかったため、"
        "週次チェックが実行できなかった可能性があります。\n"
        "次回パソコン起動時に、通常どおりの週次スケジュールで"
        "自動的にチェックが再開されます。"
    )
    sent = send_line_broadcast(text)
    if sent:
        state["last_notified_for_mtime"] = last_run_mtime
        save_state(state)


if __name__ == "__main__":
    main()

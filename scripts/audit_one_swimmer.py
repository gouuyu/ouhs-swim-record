#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一人の選手について、スイムレコードモバイル側の「全年度（2006年〜今年）」の記録を
1年度ずつ取得し、手元のロスター(index.html)にある履歴と比較して、オンライン側には
あるが手元には無い（＝取りこぼしの可能性がある）記録をリストアップする、
一回限りの棚卸し調査用スクリプト。

通常の週次チェック(check_and_update.py)との違い:
  - index.htmlへの書き込み・commit/push・LINE通知は一切行わない（読み取り専用の調査）
  - 「今年」だけでなく、選手ページの年度セレクタ(select name="year", 2006〜今年)に
    ある年度を1つずつ全部見る
  - 詳細ページ(リアクション/ラップ)の取得は行わない（存在確認が目的なので軽量化）

年度セレクタの仕組みはブラウザで実際に確認済み:
  - <select name="year"> で年度を選び、"絞込検索" というリンク
    (<a onclick="document.swimmer.submit();return false;">) をクリックすると
    その年度の記録一覧に絞り込まれる。検索フォームやログインフォームと同じ、
    JavaScript経由でフォームをsubmit()する疑似ボタンの作り。

使い方（PowerShellから、run_check.ps1と同じ環境変数を使う場合）:
  $env:SWIMRECORD_ID = "..."
  $env:SWIMRECORD_PASSWORD = "..."
  $env:AUDIT_SWIMMER_NAME = "横山 あい"
  python scripts\audit_one_swimmer.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from check_and_update import (
    LOGIN_ID,
    LOGIN_PW,
    ROSTER_PATH,
    do_login,
    entry_dedup_key,
    existing_keys_for,
    log,
    search_and_open_swimmer,
)
from merge_lib import load_roster
from swimrecord_parse import parse_swimrecord_text

TARGET_NAME = os.environ.get("AUDIT_SWIMMER_NAME")
MAX_PAGES_PER_YEAR = 30  # 安全上限。1年度でこれを超えるページ数は想定していない。


def get_next_page_href(page):
    """実際にブラウザで確認したところ、このサイトの年度別記録一覧のページ送りは
    「次へ」という文字列ではなく、"< 1 2 3 4 5 6 >" のような番号付きリンクで、
    次ページへのリンクは表示テキストが完全に ">" だけのリンクだった
    （以前のバージョンは「次へ」を探していたため、一度もページ送りが発生せず、
    常に1ページ目（最新10件）しか見ていなかった）。
    hrefを直接goto()するので、クリック可否には依存しない。"""
    candidates = page.locator("a").filter(has_text=re.compile(r"^>$"))
    if candidates.count() > 0:
        return candidates.first.get_attribute("href")
    return None


def collect_entries_for_current_view(page):
    """現在表示中の年度の記録一覧を、ページネーション（">" リンク）を辿りながら
    全部集める。詳細ページ(record_00.php)は開かない（このスクリプトは存在確認
    だけが目的）。"""
    all_entries = []
    for _ in range(MAX_PAGES_PER_YEAR):
        try:
            body_text = page.locator("body").inner_text(timeout=10000)
        except Exception:
            break
        all_entries.extend(parse_swimrecord_text(body_text))
        next_href = get_next_page_href(page)
        if next_href:
            try:
                page.goto(next_href, wait_until="domcontentloaded")
                continue
            except Exception:
                break
        break
    return all_entries


def list_year_options(page):
    """選手ページの年度セレクタ(select[name="year"])の選択肢（テキスト）一覧を返す。"""
    try:
        select = page.locator('select[name="year"]')
        if select.count() == 0:
            return []
        options = select.first.locator("option")
        return [options.nth(i).inner_text().strip() for i in range(options.count())]
    except Exception:
        return []


def select_year_and_submit(page, year_text):
    """年度セレクタを year_text に変更し、「絞込検索」と同じ方法
    (document.forms['swimmer'].submit()) でフォームを送信する。"""
    select = page.locator('select[name="year"]').first
    select.select_option(label=year_text)
    with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
        page.evaluate("document.forms['swimmer'] && document.forms['swimmer'].submit()")


def main():
    if not TARGET_NAME:
        log("エラー: AUDIT_SWIMMER_NAME が設定されていません。")
        sys.exit(1)
    if not LOGIN_ID or not LOGIN_PW:
        log("エラー: SWIMRECORD_ID / SWIMRECORD_PASSWORD が設定されていません。")
        sys.exit(1)

    _, _, roster_data = load_roster(ROSTER_PATH)
    roster_by_name = {s["name"]: s for s in roster_data}
    if TARGET_NAME not in roster_by_name:
        log(f"エラー: ロスターに「{TARGET_NAME}」が見つかりません。")
        sys.exit(1)
    swimmer_record = roster_by_name[TARGET_NAME]
    existing_keys = existing_keys_for(swimmer_record)
    log(f"対象: {TARGET_NAME}  (手元の履歴: {len(swimmer_record.get('history', []))}件)")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
            ),
            locale="ja-JP",
            viewport={"width": 412, "height": 915},
        )
        page = context.new_page()
        try:
            do_login(page)
        except Exception as e:
            log(f"ログイン処理でエラー: {e}")
            browser.close()
            sys.exit(1)

        found = search_and_open_swimmer(page, TARGET_NAME)
        if not found:
            log("選手が検索結果に見つかりませんでした（OUHS所属として一致しない）。")
            browser.close()
            sys.exit(1)

        year_options = list_year_options(page)
        if not year_options:
            log("※ 年度セレクタが見つかりませんでした。想定外のページ構造です。調査を中止します。")
            browser.close()
            sys.exit(1)
        log(f"サイト側の年度セレクタ: {year_options}")

        all_online_entries = []
        for yt in year_options:
            try:
                select_year_and_submit(page, yt)
            except Exception as e:
                log(f"  {yt}: 年度切り替えに失敗しました: {e}")
                continue
            entries = collect_entries_for_current_view(page)
            log(f"  {yt}: {len(entries)}件")
            all_online_entries.extend(entries)

        browser.close()

    log("")
    log(f"オンライン側で見つかった記録（リレー含む、延べ数）: 合計 {len(all_online_entries)}件")

    missing = []
    seen_keys = set()
    dup_count = 0
    relay_count = 0
    for e in all_online_entries:
        if e["is_relay"]:
            relay_count += 1
            continue
        key = entry_dedup_key(e)
        if key in seen_keys:
            dup_count += 1
            continue
        seen_keys.add(key)
        if key not in existing_keys:
            missing.append(e)

    log(
        f"個人種目のみ（重複除去後）: {len(seen_keys)}件  "
        f"(リレー除外: {relay_count}件 / サイト内重複: {dup_count}件)"
    )
    log(f"手元に無い記録（取りこぼしの可能性）: {len(missing)}件")
    for e in missing:
        log(f"  {e['date']} {e['style']} {e['distance']} {e['time']} {e['game_raw']}")

    log("")
    log("EXIT: audit_done")


if __name__ == "__main__":
    main()

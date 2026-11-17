#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OUHS SWIM TEAM ロスター: スイムレコードモバイル 自動巡回スクリプト

GitHub Actions のスケジュール実行専用。やること:
  1. sp.j-swim.jp にブラウザ自動操作（Playwright）でログイン
  2. ロスター(index.html)に登録されている全選手について「スイマー検索」→
     本人のページを開き、記録一覧を全ページ分読み取る
  3. 既存ロスターにまだ無い（＝新規の）レースについてだけ、そのレースの
     「詳細ページ」(record_00.php) を個別に開き、リアクションタイムと
     ラップタイムを追加で取得する。この詳細ページ取得は1件ごとに例外を分離
     してあるので、どれかが失敗してもそのレース自体（タイム・大会名など）は
     通常通り追加され、リアクション・ラップだけが空欄になる（全体は止まらない）
  4. 既存ロスターと突合し、新しい記録があれば追加（重複・リレーは自動スキップ）
     ＋ベストタイムカード(byStroke)を自動再計算
  5. 新しい記録が1件でもあれば LINE に通知する
     （index.htmlの実際の書き込みはこのスクリプトが行い、git commit/push は
     ワークフロー側のシェルステップで行う）

環境変数:
  SWIMRECORD_ID              スイムレコードモバイルのログインID（必須）
  SWIMRECORD_PASSWORD        同パスワード（必須）
  LINE_CHANNEL_ACCESS_TOKEN  LINE Messaging APIのチャネルアクセストークン（任意）
  ROSTER_PATH                ロスターHTMLファイルのパス（省略時 "index.html"）

注意:
  このサイトはページ遷移ごとに発行される暗号化トークンをURLに埋め込む古い
  フィーチャーフォン向けの作りになっているため、固定URLへの直接アクセスは
  使えない。そのため全ての遷移は「実際にリンクをクリックする」形で行う。
  サイトのレイアウトが変わると選手検索・ログインの処理が壊れる可能性がある
  （その場合はデバッグ用スクリーンショットを参照して selector を調整すること）。
"""
import json
import os
import re
import sys
import unicodedata

from playwright.sync_api import TimeoutError as PWTimeoutError
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from line_notify import send_line_broadcast
from merge_lib import academic_year, load_roster, seconds_to_time, time_to_seconds
from merge_swimrecord import merge_swimmer
from swimrecord_parse import parse_swimrecord_text

BASE_URL = "https://sp.j-swim.jp/"
ROSTER_PATH = os.environ.get("ROSTER_PATH", "index.html")
LOGIN_ID = os.environ.get("SWIMRECORD_ID")
LOGIN_PW = os.environ.get("SWIMRECORD_PASSWORD")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_screenshots")
MAX_PAGES_PER_SWIMMER = 15  # ページネーションの無限ループ防止用の安全上限

# OUHS（大阪体育大学）の表記ゆれ。検索結果一覧でこの括弧付き所属表記が付いた
# リンクだけを本人として採用する（同姓同名の他大学選手との混同を避けるため）。
AFFIL_RE_TMPL = r'^{name}\s*[（(](?:大阪体育大学|OUHS大体大)[)）]$'


def log(msg):
    print(msg, flush=True)


def norm_nospace(s):
    s = unicodedata.normalize("NFKC", s or "")
    return s.replace(" ", "").replace("　", "")


def save_debug_screenshot(page, label):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, f"{label}.png")
        page.screenshot(path=path, full_page=True)
        log(f"  → デバッグ用スクリーンショットを保存しました: {path}")
    except Exception as e:
        log(f"  → スクリーンショットの保存に失敗: {e}")


def click_text(page, text, timeout=10000):
    """表示テキストが完全一致するリンク/ボタンをクリックする。
    このサイトは遷移ごとにURLに暗号化トークンを含むため、固定URLへの
    goto() は使わず、必ずこの関数のように実際のリンクをクリックする。"""
    loc = page.get_by_text(text, exact=True).first
    loc.wait_for(state="visible", timeout=timeout)
    loc.click()
    page.wait_for_load_state("domcontentloaded")


def do_login(page):
    page.goto(BASE_URL, wait_until="domcontentloaded")

    if page.get_by_text("ログアウト", exact=True).count() > 0:
        log("既にログイン状態です（想定外ですが続行します）。")
        return

    # 「ログイン」リンク/ボタンを探してクリック
    login_entry = page.get_by_text("ログイン", exact=False).first
    try:
        login_entry.wait_for(state="visible", timeout=10000)
    except PWTimeoutError:
        # 想定外の画面になっている可能性が高いので、原因調査のために現在の
        # ページの状態をログとスクリーンショットの両方に残してから失敗させる。
        log("  ※ トップページに「ログイン」という文字列が見つかりませんでした。")
        log(f"  現在のURL: {page.url}")
        log(f"  ページタイトル: {page.title()}")
        save_debug_screenshot(page, "login_entry_not_found")
        try:
            body_text = page.locator("body").inner_text(timeout=5000)
            log("  ページ本文（先頭1000文字）:")
            log(body_text[:1000])
        except Exception as e2:
            log(f"  ページ本文の取得にも失敗しました: {e2}")
        raise
    login_entry.click()
    page.wait_for_load_state("domcontentloaded")

    # ID/パスワード欄を汎用セレクタで特定（サイト側の name 属性が不明なため、
    # 「最初のテキスト系input」「最初のpassword input」という構造的な位置で判定）
    id_input = page.locator('input[type="text"], input[type="email"], input[type="tel"]').first
    pw_input = page.locator('input[type="password"]').first
    id_input.wait_for(state="visible", timeout=10000)
    id_input.fill(LOGIN_ID)
    pw_input.fill(LOGIN_PW)

    submit = page.locator('input[type="submit"], button[type="submit"]').first
    if submit.count() == 0:
        submit = page.get_by_text("ログイン", exact=False).last
    submit.click()
    page.wait_for_load_state("domcontentloaded")

    if page.get_by_text("ログアウト", exact=True).count() == 0:
        save_debug_screenshot(page, "login_failed")
        raise RuntimeError(
            "ログインに失敗しました（ログイン後に「ログアウト」リンクが見つかりません）。"
            "IDまたはパスワードが間違っている可能性、またはサイト側のログイン画面の"
            "構造が変わった可能性があります。デバッグ用スクリーンショットを確認してください。"
        )
    log("ログインに成功しました。")


def search_and_open_swimmer(page, full_name):
    last_first = re.split(r"[ 　]", full_name, maxsplit=1)
    last = last_first[0]
    first = last_first[1] if len(last_first) > 1 else ""

    page.goto(BASE_URL, wait_until="domcontentloaded")
    click_text(page, "スイマー検索")

    text_inputs = page.locator('input[type="text"]')
    n = text_inputs.count()
    if n >= 2:
        text_inputs.nth(0).fill(last)
        text_inputs.nth(1).fill(first)
    elif n == 1:
        text_inputs.nth(0).fill(last + first)
    else:
        raise RuntimeError("スイマー検索フォームの入力欄が見つかりませんでした。")

    submit = page.locator('input[type="submit"], button[type="submit"]').first
    if submit.count() == 0:
        submit = page.get_by_text("検索", exact=False).first
    submit.click()
    page.wait_for_load_state("domcontentloaded")

    target_nospace = norm_nospace(full_name)
    affil_re = re.compile(AFFIL_RE_TMPL.format(name=re.escape(target_nospace)))
    # 検索結果一覧から、所属が大阪体育大学/OUHS大体大 と完全一致するリンクだけを拾う
    all_links = page.locator("a")
    n_links = all_links.count()
    for i in range(n_links):
        try:
            txt = all_links.nth(i).inner_text(timeout=2000)
        except PWTimeoutError:
            continue
        candidate_nospace = norm_nospace(txt)
        if affil_re.match(candidate_nospace):
            all_links.nth(i).click()
            page.wait_for_load_state("domcontentloaded")
            return True
    return False


def select_year_if_possible(page, year_text):
    try:
        select = page.locator("select").first
        if select.count() > 0:
            options = select.locator("option")
            for i in range(options.count()):
                if year_text in (options.nth(i).inner_text() or ""):
                    select.select_option(label=options.nth(i).inner_text())
                    page.wait_for_load_state("domcontentloaded")
                    return
    except Exception:
        pass
    try:
        year_link = page.get_by_text(year_text, exact=False).first
        if year_link.count() > 0:
            year_link.click()
            page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass


def entry_dedup_key(e):
    """merge_swimmer と全く同じロジックで (year, date(mm/dd), distance, style, time) の
    重複判定キーを作る。既存ロスターに無いレースだけ詳細ページを開くための事前判定に使う。"""
    y, mo, da = e["date"].split("/")
    cal_year = int(y)
    mo_i = int(mo)
    year = academic_year(cal_year, mo_i)
    md = f"{mo_i:02d}/{int(da):02d}"
    return (year, md, e["distance"], e["style"], e["time"])


def existing_keys_for(swimmer_record):
    keys = set()
    for h in swimmer_record.get("history", []):
        keys.add((h["year"], h["date"], h["distance"], h["style"], h["time"]))
    return keys


def parse_record_detail(text):
    """「レコードページ(詳細)」(record_00.php) の本文テキストから、リアクションタイムと
    ラップタイム（50mごとの通過タイム）を抽出する。どちらのセクションも無い場合（リレー
    種目や、サイト側がまだこの情報を公表していない大会など）は None のまま返す。"""
    reaction = None
    m = re.search(r"リアクションタイム\s*\n+\s*([\d.]+)", text)
    if m:
        try:
            reaction = float(m.group(1))
        except ValueError:
            reaction = None

    laps = None
    lap_m = re.search(r"ラップタイム\s*\n((?:\s*┗[^\n]+\n?)+)", text)
    if lap_m:
        lines = [l.strip().lstrip("┗").strip() for l in lap_m.group(1).strip().split("\n") if l.strip()]
        laps = []
        prev_secs = 0.0
        for i, cum_raw in enumerate(lines):
            d = 50 * (i + 1)
            cum_norm = cum_raw if ":" in cum_raw else "0:" + cum_raw
            try:
                cum_secs = time_to_seconds(cum_norm)
            except ValueError:
                continue
            split_secs = cum_secs - prev_secs
            laps.append({"d": d, "cum": cum_norm, "split": seconds_to_time(split_secs)})
            prev_secs = cum_secs
        if not laps:
            laps = None
    return {"reaction": reaction, "laps": laps}


def fetch_detail_for_new_entries(page, entries, existing_keys):
    """ページ上の各レース行について、まだロスターに無い（＝新規の）レースだけ、
    その行の詳細ページ(record_00.php)を開いてリアクション・ラップを取得する。
    1件ごとに例外を分離しているので、どれか1件の詳細ページ取得に失敗しても、
    そのレース自体（タイム・大会名など）は通常通り追加され、リアクション・ラップ
    だけが空欄になる。失敗してもページ遷移自体は必ず元のスイマーページに戻す。"""
    detail_links = page.locator('a[href*="record_00.php"]')
    link_count = detail_links.count()
    if link_count != len(entries):
        log(f"  ※ 行数({len(entries)})と詳細リンク数({link_count})が一致しないため、"
            f"このページではリアクション・ラップの取得をスキップします。")
        return entries

    for i, e in enumerate(entries):
        if e["is_relay"]:
            continue
        if entry_dedup_key(e) in existing_keys:
            continue  # 既存の記録。詳細ページを開く必要はない
        try:
            detail_links.nth(i).click()
            page.wait_for_load_state("domcontentloaded")
            detail_text = page.locator("body").inner_text(timeout=10000)
            parsed = parse_record_detail(detail_text)
            if parsed["reaction"] is not None:
                e["reaction"] = parsed["reaction"]
            if parsed["laps"]:
                e["laps"] = parsed["laps"]
        except Exception as ex:
            log(f"  ※ 詳細ページ取得に失敗（{e['date']} {e['style']}{e['distance']}）: {ex}")
            save_debug_screenshot(page, f"detail_failed_{i}")
        finally:
            try:
                page.go_back(wait_until="domcontentloaded")
            except Exception as ex:
                log(f"  ※ 詳細ページから戻れませんでした: {ex}")
    return entries


def collect_entries_with_detail(page, existing_keys):
    all_entries = []
    for _ in range(MAX_PAGES_PER_SWIMMER):
        try:
            body_text = page.locator("body").inner_text(timeout=10000)
        except PWTimeoutError:
            break
        page_entries = parse_swimrecord_text(body_text)
        try:
            page_entries = fetch_detail_for_new_entries(page, page_entries, existing_keys)
        except Exception as ex:
            log(f"  ※ このページでの詳細ページ取得処理中にエラー: {ex}")
        all_entries.extend(page_entries)

        next_link = page.get_by_text("次へ", exact=True)
        if next_link.count() > 0:
            try:
                next_link.first.click()
                page.wait_for_load_state("domcontentloaded")
                continue
            except Exception:
                break
        break
    return all_entries


def process_swimmer(page, full_name, current_year, swimmer_record):
    log(f"=== {full_name} ===")
    found = search_and_open_swimmer(page, full_name)
    if not found:
        log("  OUHS所属として検索結果に見つかりませんでした（スキップ）。")
        return {"name": full_name, "added": 0, "added_entries": [], "error": None}

    select_year_if_possible(page, str(current_year))
    existing_keys = existing_keys_for(swimmer_record)
    entries = collect_entries_with_detail(page, existing_keys)
    if not entries:
        log("  記録を抽出できませんでした（0件 or ページ構造が想定と異なる）。")
        return {"name": full_name, "added": 0, "added_entries": [], "error": None}

    report = merge_swimmer(ROSTER_PATH, full_name, entries, dry_run=False)
    if "error" in report:
        log(f"  ロスターに該当選手が見つかりません: {report['error']}")
        return {"name": full_name, "added": 0, "added_entries": [], "error": report["error"]}

    log(f"  追加: {report['added']}件 / 重複: {report['dup']}件 / リレー除外: {report['relay_skip']}件")
    if report["auto_cleaned"]:
        log("  ※ 大会名が自動クリーニングされました（要確認）:")
        for raw, canon in report["auto_cleaned"]:
            log(f"      {raw}  →  {canon}")
    return {"name": full_name, "added": report["added"], "added_entries": report["added_entries"], "error": None}


def build_notification_text(results_with_new):
    lines = ["【OUHS SWIM TEAM】新しい記録が見つかりました"]
    for r in results_with_new:
        lines.append("")
        lines.append(f"■ {r['name']}（{r['added']}件）")
        for e in r["added_entries"][:5]:
            lines.append(f"  {e['date']} {e['style']} {e['distance']} {e['time']} {e['game']}")
        if len(r["added_entries"]) > 5:
            lines.append(f"  …他{len(r['added_entries']) - 5}件")
    lines.append("")
    lines.append("ロスターページで詳細をご確認ください。")
    return "\n".join(lines)


def main():
    if not LOGIN_ID or not LOGIN_PW:
        log("エラー: SWIMRECORD_ID / SWIMRECORD_PASSWORD が設定されていません。")
        sys.exit(1)

    _, _, roster_data = load_roster(ROSTER_PATH)
    swimmer_names = [s["name"] for s in roster_data]
    roster_by_name = {s["name"]: s for s in roster_data}
    log(f"監視対象: ロスター全{len(swimmer_names)}名")

    import datetime
    current_year = datetime.date.today().year

    results = []
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            do_login(page)
        except Exception as e:
            log(f"ログイン処理でエラー: {e}")
            browser.close()
            sys.exit(1)

        for name in swimmer_names:
            try:
                result = process_swimmer(page, name, current_year, roster_by_name[name])
                results.append(result)
                if result["error"]:
                    errors.append((name, result["error"]))
            except Exception as e:
                log(f"  選手処理中にエラー: {e}")
                save_debug_screenshot(page, f"error_{norm_nospace(name)}")
                errors.append((name, str(e)))

        browser.close()

    total_added = sum(r["added"] for r in results)
    results_with_new = [r for r in results if r["added"] > 0]

    log("")
    log(f"=== 完了: 合計 {total_added}件の新しい記録を追加しました ===")
    if errors:
        log(f"※ {len(errors)}名の処理でエラーが発生しました: {[n for n, _ in errors]}")

    if results_with_new:
        notif_text = build_notification_text(results_with_new)
        send_line_broadcast(notif_text)
    else:
        log("新しい記録はありませんでした。通知・コミットは行いません。")

    # 変更があったかどうかをシェル側（ワークフロー）が判断できるよう、
    # 終了コードではなくファイルの有無で伝える（git diffで検知するため不要だが、
    # ログ上で分かりやすくするために明示しておく）
    if total_added == 0:
        log("EXIT: no_changes")
    else:
        log("EXIT: changes_made")


if __name__ == "__main__":
    main()

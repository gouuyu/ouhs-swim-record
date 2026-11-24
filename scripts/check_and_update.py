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
import subprocess
import sys
import time
import unicodedata

from playwright.sync_api import TimeoutError as PWTimeoutError
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from line_notify import send_line_broadcast
from merge_lib import (
    academic_year,
    load_roster,
    seconds_to_time,
    time_to_seconds,
    update_data_retrieval_date,
)
from merge_swimrecord import merge_swimmer
from para_check import PARA_PNO, fetch_para_rows, merge_para_history, parse_para_row
from swimrecord_parse import parse_swimrecord_text

BASE_URL = "https://sp.j-swim.jp/"
ROSTER_PATH = os.environ.get("ROSTER_PATH", "index.html")
LOGIN_ID = os.environ.get("SWIMRECORD_ID")
LOGIN_PW = os.environ.get("SWIMRECORD_PASSWORD")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_screenshots")
MAX_PAGES_PER_SWIMMER = 30  # ページネーションの無限ループ防止用の安全上限

# 詳細ページ(record_00.php)のクリックが、サイト側のページ構造変化等が原因で
# 繰り返し失敗することがある。1件ごとに既定の30秒待ってタイムアウトすると、
# 失敗が続いた場合に全体の処理時間が大きく伸びてしまう（2026-11、ほぼ全選手で
# 失敗が続き、合計90分以上かかった実例あり）。そのため、
#   ・1回あたりの待ち時間を既定の30秒から5秒に短縮する
#   ・累計でDETAIL_FETCH_FAILURE_LIMIT回失敗したら、今回の実行ではこれ以降
#     リアクション・ラップタイムの取得自体をスキップする（通常のタイム・
#     大会名などの記録は、このあとも通常通り追加される。次回の実行では
#     また最初から取得を試みる＝一時的な不具合なら自然に復旧する）
# という安全策を入れている。
DETAIL_FETCH_TIMEOUT_MS = 5000
DETAIL_FETCH_FAILURE_LIMIT = 5
_detail_fetch_state = {"failures": 0, "disabled": False}

# OUHS（大阪体育大学）の表記ゆれ。検索結果一覧でこの括弧付き所属表記が付いた
# リンクだけを本人として採用する（同姓同名の他大学選手との混同を避けるため）。
AFFIL_RE_TMPL = r'^{name}\s*[（(](?:{affils})[)）]$'
DEFAULT_AFFILIATIONS = ["大阪体育大学", "OUHS大体大"]

# ロスターには大阪体育大学の部員として載っているが、スイムレコードモバイル上の
# 登録所属はクラブ名になっている選手の例外リスト。
# （2026-11 棚卸し調査で発覚。上原心選手は大阪体育大学で練習しているが、
# 登録上の所属は「尾崎スイミングスクール泉佐野」のため、通常の所属名では
# 検索に一致しない。）
AFFILIATION_OVERRIDES = {
    "上原 心": ["尾崎スイミングスクール泉佐野"],
}

# ロスターの氏名に使われている漢字が、サイト側の検索フォームの文字セットに
# 無く入力しても姓名欄が空になってしまう選手の例外（2026-11 棚卸し調査で
# 発覚）。桒野元輝選手の「桒」がこれに該当し、名前（元輝）だけで検索すると、
# サイト上はカタカナ表記「ｸﾜﾉ ｹﾞﾝｷ」で表示される。
SEARCH_NAME_OVERRIDES = {
    "桒野 元輝": {"last": "", "first": "元輝", "match_text": "ｸﾜﾉｹﾞﾝｷ"},
}

# ロスターの表記とスイムレコードモバイル側の表示で、同じ人物なのに使われている
# 漢字の異体字が違うケース（2026-11 棚卸し調査で発覚。例: 德永央さん→サイトでは
# 「徳永央」、早﨑愛莉さん/岡﨑一彗さん→「崎」、桒野元輝さん→「桑野」で表示される）。
# 検索結果の所属判定で両者を同一視するため、比較前にこの変換をかける。
KANJI_VARIANT_MAP = {
    "德": "徳",
    "﨑": "崎",
    "桒": "桑",
}

# 同姓同名が多い名前だと、検索結果一覧自体が複数ページに渡ることがある
# （2026-11 棚卸し調査で発覚。例: 伊藤大智さん、藤田涼さんは本人が2ページ目に
# いた）。安全上限として、この回数までは「次へ」を辿って探す。
MAX_SEARCH_RESULT_PAGES = 10


def normalize_kanji_variants(s):
    for old, new in KANJI_VARIANT_MAP.items():
        s = s.replace(old, new)
    return s


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
    goto() は使わず、必ずこの関数のように実際のリンクをクリックする。

    ログイン後のページには、同じテキストのリンクがDOM上に複数（おそらく
    ヘッダー側の非表示コピーと、本文側の実際に見えているもの）存在する
    ケースがあることが分かった。.first は常にDOM上で最初に出てくる方を
    指すため、それがたまたま非表示側だと永久にタイムアウトしてしまう。
    そのため、候補を一つずつ試して「実際に見えている」ものを優先して
    クリックする。"""
    candidates = page.get_by_text(text, exact=True)
    deadline = time.monotonic() + (timeout / 1000)
    visible_loc = None
    while time.monotonic() < deadline:
        n = candidates.count()
        for i in range(n):
            cand = candidates.nth(i)
            try:
                if cand.is_visible():
                    visible_loc = cand
                    break
            except Exception:
                continue
        if visible_loc is not None:
            break
        page.wait_for_timeout(300)
    if visible_loc is None:
        # 最後の手段として、従来通り最初の一致を使う（エラーメッセージで
        # 原因が分かるように失敗はさせる）。
        visible_loc = candidates.first
    visible_loc.wait_for(state="visible", timeout=2000)
    visible_loc.click()
    page.wait_for_load_state("domcontentloaded")


def goto_by_link_text(page, text, timeout=10000):
    """表示テキストに一致するリンクのhref属性を取得し、goto()で直接遷移する。

    実際に試したところ、ホームページのナビゲーション項目（スイマー検索等）は
    カルーセルのようにDOM上で複製されており、is_visible()が一瞬trueを返しても、
    実際の.click()はPlaywrightのactionabilityチェック（表示されていて、
    安定していて、他の要素に隠れていないこと）に通らずタイムアウトする
    ケースがあった（おそらくCSSアニメーションで表示/非表示が切り替わる
    ため）。
    hrefの値自体はこのログインセッション用の正規のURL（トークン付き）な
    ので、クリック操作を使わずそれを読み取ってgoto()すれば、要素が
    クリック可能な状態になっているかどうかに関係なく確実に遷移できる。"""
    loc = page.locator("a").filter(has_text=text).first
    loc.wait_for(state="attached", timeout=timeout)
    href = loc.get_attribute("href")
    if not href:
        raise RuntimeError(f"「{text}」リンクのhref属性が取得できませんでした。")
    page.goto(href, wait_until="domcontentloaded")


def do_login(page):
    page.goto(BASE_URL, wait_until="domcontentloaded")

    if page.get_by_text("ログアウト", exact=True).count() > 0:
        log("既にログイン状態です（想定外ですが続行します）。")
        return

    # 実際にブラウザで未ログイン状態のトップページを確認したところ、
    # トップページ上には「ログイン」という文字列は一切存在しなかった
    # （ログイン中は「ログアウト」と表示される同じ位置が、未ログイン時は
    # 「初めての方へ」という文言になっていた）。そこを辿ると画像だけの
    # ボタン（テキストなし）のランディングページを経由して、最終的に
    # /login_select.php の「ID・パスワードでログイン」リンクから
    # /login/login.php のID/パスワード入力フォームに到達する、という
    # 3段階の画面遷移だった。
    #
    # 以前の実装は「ログイン」という文字列を探してクリックしようとしていた
    # ため、そもそもトップページに存在しないテキストを探し続けて
    # 必ず失敗していた。
    #
    # /login/login.php はセッショントークンを含まない固定URLで、未ログイン
    # 状態からの直接アクセスでも正しくID/パスワード入力フォームが表示される
    # ことをブラウザで直接確認済みなので、画像ボタンを辿るクリック操作を
    # 省略し、直接このURLへ遷移する。
    LOGIN_FORM_URL = "https://sp.j-swim.jp/login/login.php"
    page.goto(LOGIN_FORM_URL, wait_until="domcontentloaded")

    # サイト側の実装（name="mail" / name="password"）をブラウザで直接確認
    # 済みだが、念のため汎用セレクタも後方互換のフォールバックとして残す。
    id_input = page.locator(
        'input[name="mail"], input[type="text"], input[type="email"], '
        'input[type="tel"], input:not([type])'
    ).first
    pw_input = page.locator('input[name="password"], input[type="password"]').first
    try:
        id_input.wait_for(state="visible", timeout=10000)
    except PWTimeoutError:
        log("  ※ ログインフォームのページに移動しましたが、ID入力欄が見つかりませんでした。")
        log(f"  現在のURL: {page.url}")
        log(f"  ページタイトル: {page.title()}")
        save_debug_screenshot(page, "id_input_not_found")
        try:
            body_text = page.locator("body").inner_text(timeout=5000)
            log("  ページ本文（先頭1000文字）:")
            log(body_text[:1000])
        except Exception as e2:
            log(f"  ページ本文の取得にも失敗しました: {e2}")
        try:
            html = page.content()
            log("  フォーム部分のHTML（<form>タグの数: " + str(page.locator("form").count()) + "）:")
            inputs_n = page.locator("input").count()
            log(f"  ページ内のinputタグの数: {inputs_n}")
            for i in range(min(inputs_n, 10)):
                try:
                    outer = page.locator("input").nth(i).evaluate("el => el.outerHTML")
                    log(f"    input[{i}]: {outer}")
                except Exception:
                    pass
        except Exception as e3:
            log(f"  HTML構造の取得に失敗しました: {e3}")
        raise
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
    # 検索フォーム自体への入力は、ロスターの表記（異体字を含む元の文字列）の
    # ままにする。実際に試したところ、このサイトの検索は異体字入力の方で
    # ヒットし、結果の表示だけ標準字体になる（例: 「德永」で検索すると
    # 「徳永央」が見つかる）。逆に標準字体に変換してから検索すると、同じ
    # 選手でもヒットしなくなるケースがあった（早﨑さん・岡﨑さん・德永さんで
    # 確認済み）。そのため検索クエリは変換しない。異体字の同一視は、検索結果
    # の表記とロスター側の表記を比較する側（下のaffil_re）だけで行う。
    name_override = SEARCH_NAME_OVERRIDES.get(full_name)
    if name_override:
        last = name_override["last"]
        first = name_override["first"]
    else:
        last_first = re.split(r"[ 　]", full_name, maxsplit=1)
        last = last_first[0]
        first = last_first[1] if len(last_first) > 1 else ""

    page.goto(BASE_URL, wait_until="domcontentloaded")
    goto_by_link_text(page, "スイマー検索")

    text_inputs = page.locator('input[type="text"]')
    n = text_inputs.count()
    if n >= 2:
        text_inputs.nth(0).fill(last)
        text_inputs.nth(1).fill(first)
    elif n == 1:
        text_inputs.nth(0).fill(last + first)
    else:
        raise RuntimeError("スイマー検索フォームの入力欄が見つかりませんでした。")

    # 実際のフォームHTMLを確認したところ、この検索フォームに
    # input[type="submit"]/button[type="submit"] は存在せず、見た目上の
    # 「検索」ボタンは <a onclick="document.search_name.submit();
    # return false;">この名前で検索</a> という、JavaScript経由でフォームを
    # 送信する疑似ボタンだった（表示テキストも「検索」とは完全一致しない）。
    # ボタンの見た目やテキストを探すのではなく、氏名を入力したそのフォーム
    # 自体をJavaScript側から直接 submit() する方が、表示テキストや
    # ボタンの実装に依存せず確実。
    # evaluate() でJavaScript側からsubmit()を呼ぶと、その後のnavigation完了を
    # Playwrightの .click() のようには自動で追跡してくれない。単純に
    # wait_for_load_state() を続けて呼ぶだけだと、まだ始まっていない
    # navigationに対して早すぎるタイミングで「読み込み完了」とみなして
    # しまうことがあり、その食い違いが次の選手の処理でpage.goto()を呼んだ
    # 際に「別のnavigationに割り込まれた」というエラーになって表面化する
    # ことが実際にあった。expect_navigation() で明示的にこのsubmit()による
    # 遷移そのものを待つことで、取りこぼしなく確実に完了を待つ。
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
            text_inputs.nth(0).evaluate("el => el.form && el.form.submit()")
    except Exception as e:
        log(f"  ※ 検索フォームのsubmit()に失敗しました: {e}")
        save_debug_screenshot(page, "search_submit_failed")
        raise

    if name_override:
        # 比較対象の候補テキストは norm_nospace() で半角/全角カタカナの正規化
        # (NFKC)もかかった状態になるので、match_text側にも同じ正規化をかけて
        # 揃える（揃えないと半角カタカナのまま比較してしまい一致しない）。
        target_nospace = normalize_kanji_variants(norm_nospace(name_override["match_text"]))
    else:
        target_nospace = normalize_kanji_variants(norm_nospace(full_name))
    affils = AFFILIATION_OVERRIDES.get(full_name, DEFAULT_AFFILIATIONS)
    affil_re = re.compile(
        AFFIL_RE_TMPL.format(
            name=re.escape(target_nospace),
            affils="|".join(re.escape(a) for a in affils),
        )
    )

    # 検索結果一覧から、所属が一致するリンクだけを拾う。同姓同名が多い名前だと
    # 検索結果一覧自体が複数ページに渡ることがあるので、「次へ」を
    # MAX_SEARCH_RESULT_PAGES回まで辿る（検索結果一覧の「次へ」は通常のテキスト
    # リンクで、記録一覧のページ送りとは別物）。
    for _ in range(MAX_SEARCH_RESULT_PAGES):
        all_links = page.locator("a")
        n_links = all_links.count()
        for i in range(n_links):
            try:
                txt = all_links.nth(i).inner_text(timeout=2000)
            except PWTimeoutError:
                continue
            candidate_nospace = normalize_kanji_variants(norm_nospace(txt))
            if affil_re.match(candidate_nospace):
                all_links.nth(i).click()
                page.wait_for_load_state("domcontentloaded")
                return True

        next_link = page.get_by_text("次へ", exact=True)
        if next_link.count() > 0:
            try:
                next_link.first.click()
                page.wait_for_load_state("domcontentloaded")
                continue
            except Exception:
                break
        break
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
        if _detail_fetch_state["disabled"]:
            continue  # 詳細ページ取得が規定回数失敗したため、今回の実行ではスキップ
        try:
            # 2026-11診断: 詳細リンク(<a>)が <li class="time">...</li> を
            # 直接くるんだまま <ul> の子要素になっているため(サイト側のマーク
            # アップ不備。恐らく「R」バッジ機能追加と同時に崩れた)、ブラウザは
            # インライン要素(a)の中にブロック要素(li)がある状態を解決しようと
            # して a 自身の表示領域を幅0・高さ0に潰してしまう。これが原因で
            # Playwrightの .click() が「要素が見えない」としてタイムアウトし
            # 続けていた（診断スクリプトのbounding_box()で確認済み）。
            # 対策: クリックする代わりに href を直接読み取って goto する。
            # get_next_record_page_href() と同じ考え方＝クリック可否に依存しない。
            href = detail_links.nth(i).get_attribute("href", timeout=DETAIL_FETCH_TIMEOUT_MS)
            if not href:
                raise RuntimeError("詳細リンクのhref属性が取得できませんでした")
            page.goto(href, wait_until="domcontentloaded", timeout=DETAIL_FETCH_TIMEOUT_MS * 2)
            detail_text = page.locator("body").inner_text(timeout=10000)
            parsed = parse_record_detail(detail_text)
            if parsed["reaction"] is not None:
                e["reaction"] = parsed["reaction"]
            if parsed["laps"]:
                e["laps"] = parsed["laps"]
        except Exception as ex:
            log(f"  ※ 詳細ページ取得に失敗（{e['date']} {e['style']}{e['distance']}）: {ex}")
            save_debug_screenshot(page, f"detail_failed_{i}")
            _detail_fetch_state["failures"] += 1
            if (
                _detail_fetch_state["failures"] >= DETAIL_FETCH_FAILURE_LIMIT
                and not _detail_fetch_state["disabled"]
            ):
                _detail_fetch_state["disabled"] = True
                log(
                    f"  ※ 詳細ページ取得が{DETAIL_FETCH_FAILURE_LIMIT}回失敗したため、"
                    "今回の実行ではこれ以降のリアクション・ラップタイムの取得をスキップします"
                    "（通常のタイム・大会名などの記録は、このあとも通常通り追加されます。"
                    "次回の実行ではまた取得を試みます）。"
                )
        finally:
            try:
                page.go_back(wait_until="domcontentloaded")
            except Exception as ex:
                log(f"  ※ 詳細ページから戻れませんでした: {ex}")
    return entries


def get_next_record_page_href(page):
    """記録一覧のページ送りリンクを返す（無ければNone）。
    実際にブラウザで確認したところ、記録一覧のページ送りは「次へ」という
    文字列ではなく "< 1 2 3 4 5 6 >" のような番号付きリンクで、次ページへの
    リンクは表示テキストが完全に ">" だけのリンクだった（2026-11 棚卸し調査で
    発覚。以前は「次へ」を探していたため、1年度に11件以上記録がある選手は
    1ページ目しか見ておらず、記録が溜まり続ける選手ほど取りこぼしが増える
    状態になっていた）。hrefを直接goto()するので、クリック可否には依存しない。"""
    candidates = page.locator("a").filter(has_text=re.compile(r"^>$"))
    if candidates.count() > 0:
        return candidates.first.get_attribute("href")
    return None


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

        next_href = get_next_record_page_href(page)
        if next_href:
            try:
                page.goto(next_href, wait_until="domcontentloaded")
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


def build_notification_text(results_with_new, para_notify=None):
    lines = ["【OUHS SWIM TEAM】新しい記録が見つかりました"]
    for r in results_with_new:
        lines.append("")
        lines.append(f"■ {r['name']}（{r['added']}件）")
        for e in r["added_entries"][:5]:
            lines.append(f"  {e['date']} {e['style']} {e['distance']} {e['time']} {e['game']}")
        if len(r["added_entries"]) > 5:
            lines.append(f"  …他{len(r['added_entries']) - 5}件")
    for name, entries in (para_notify or []):
        lines.append("")
        lines.append(f"■ {name}（パラ水泳・{len(entries)}件）")
        for e in entries[:5]:
            cls_label = f"({e['cls']})" if e.get("cls") else ""
            lines.append(f"  {e['date']} {e['style']}{e['distance']}{cls_label} {e['time']} {e['game']}")
        if len(entries) > 5:
            lines.append(f"  …他{len(entries) - 5}件")
    lines.append("")
    lines.append("ロスターページで詳細をご確認ください。")
    return "\n".join(lines)


def git_commit_and_push(roster_path):
    """index.htmlに変更があれば、ここでgit commit・pushまで完結させる。

    以前は「index.htmlの更新・LINE通知はこのスクリプトが行い、commit・push は
    呼び出し元（GitHub Actionsワークフローのシェルステップ／run_check.ps1）が
    行う」という役割分担だった。しかし、PowerShellから直接
    `python scripts\\check_and_update.py` を手動実行した場合（run_check.ps1を
    経由しない場合）はcommit・pushが一切行われず、「LINEには新記録の通知が
    来たのに、サイトには反映されていない」という事故が実際に起きた
    （2026-11）。そのため、呼び出し経路によらず必ずここでpushまで完了させる
    ように一本化した。

    GitHub Actions環境では、ワークフロー側の`actions/checkout`がpush用の
    認証情報を既にgit configに設定済みなので、ここから素の`git push`を呼ぶ
    だけでそのまま使える。ローカルPCでは、通常はGitHub Desktopや
    run_check.ps1が既にgitのuser.name/user.emailを設定済みだが、万一未設定
    （真っさらな環境）でも落ちないよう、未設定の場合だけ既定値を設定する。

    gitコマンド自体が無い環境や、何らかの理由でpushに失敗した場合でも、
    index.htmlの更新・LINE通知は既に完了しているので、ここでは例外を外に
    投げずログに記録するだけにとどめる（run_check.ps1側の重複チェックが、
    ここでのpush失敗時の二重の安全網として残っている）。"""

    def _git(args):
        return subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    try:
        if not _git(["config", "user.name"]).stdout.strip():
            _git(["config", "user.name", "github-actions[bot]"])
        if not _git(["config", "user.email"]).stdout.strip():
            _git(["config", "user.email", "github-actions[bot]@users.noreply.github.com"])

        _git(["add", roster_path])
        diff = _git(["diff", "--cached", "--quiet", "--", roster_path])
        if diff.returncode == 0:
            log("index.htmlに変更はありませんでした。commit & push は行いません。")
            return

        import datetime as _dt

        msg = f"スイムレコード自動チェック: 記録を更新 ({_dt.date.today():%Y-%m-%d})"
        commit = _git(["commit", "-m", msg])
        if commit.returncode != 0:
            log(f"※ commitに失敗しました: {commit.stderr.strip()}")
            return
        log("index.htmlに変更を検出しました。commit & push します。")

        push = _git(["push"])
        if push.returncode == 0:
            log("pushしました。")
        else:
            log(f"※ pushに失敗しました（終了コード: {push.returncode}）: {push.stderr.strip()}")
    except FileNotFoundError:
        log(
            "※ gitコマンドが見つからないため、commit・pushはスキップしました"
            "（index.htmlの更新自体は完了しています。手動でコミットしてください）。"
        )
    except Exception as e:
        log(f"※ commit・push処理でエラー: {e}")


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
        # Playwrightの既定のヘッドレスChromiumは、自分自身を"HeadlessChrome"という
        # 自動化ツールだと分かる名前でサイトに名乗ってしまう。多くのサイトのWAF
        # （不正アクセス防御システム、今回はSiteGuard Server Edition）はこの文字列を
        # 見つけて機械的にブロックするため、通常のスマートフォン(Android Chrome)の
        # ブラウザと同じ情報を名乗るようにして、ブロックを回避する。
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

    # パラ水泳の記録チェック（日本パラ水泳連盟 選手・記録データベース、
    # paraswim3.jp）。通常の51名チェックとは完全に独立したステップとして実行
    # する。ログイン不要の別サイトであり、ここで何か失敗しても、通常チェックの
    # 結果やLINE通知が巻き込まれて止まらないようにするため（2026-11、動作確認用
    # のpara_check.pyでの検証を経て本体に組み込み）。
    para_added_total = 0
    para_notify = []
    try:
        with sync_playwright() as p:
            para_browser = p.chromium.launch()
            para_page = para_browser.new_page()
            for para_name, pno in PARA_PNO.items():
                try:
                    rows = fetch_para_rows(para_page, pno)
                    parsed = [e for e in (parse_para_row(para_name, v) for v in rows) if e is not None]
                    report = merge_para_history(ROSTER_PATH, para_name, parsed, dry_run=False)
                    if "error" in report:
                        log(f"  ※ パラ記録チェック: {report['error']}")
                        continue
                    if report["added"] > 0:
                        log(f"  パラ記録チェック: {para_name} に新しい記録 {report['added']}件")
                        para_added_total += report["added"]
                        para_notify.append((para_name, report["added_entries"]))
                    else:
                        log(f"  パラ記録チェック: {para_name} は新しい記録なし")
                except Exception as e:
                    log(f"  ※ パラ記録チェック（{para_name}）でエラー: {e}")
            para_browser.close()
    except Exception as e:
        log(f"※ パラ記録チェック全体でエラー: {e}")

    total_added = sum(r["added"] for r in results) + para_added_total
    results_with_new = [r for r in results if r["added"] > 0]

    # 「データ取得日」の表示は、新しい記録の有無に関わらず、チェックを
    # 実行するたびに毎回更新する（＝「最後にチェックした日」を表す）運用に
    # した。新記録があったときだけ更新する方式より処理は増えるが（index.html
    # が毎回変わるので毎週commit/pushが発生する）、サイトを見た人が
    # 「ちゃんと動き続けている」と分かるようにするため、あえてこちらを選んだ。
    try:
        current_date = datetime.date.today()
        if update_data_retrieval_date(ROSTER_PATH, current_date):
            log(f"「データ取得日」の表示を {current_date:%Y-%m-%d} に更新しました。")
        else:
            log("※「データ取得日」の表示箇所が見つからず、更新できませんでした"
                "（index.htmlのレイアウトが変わった可能性があります）。")
    except Exception as e:
        log(f"※「データ取得日」の更新に失敗しました: {e}")

    log("")
    log(f"=== 完了: 合計 {total_added}件の新しい記録を追加しました ===")
    if errors:
        log(f"※ {len(errors)}名の処理でエラーが発生しました: {[n for n, _ in errors]}")

    if results_with_new or para_notify:
        notif_text = build_notification_text(results_with_new, para_notify)
        send_line_broadcast(notif_text)
    else:
        log("新しい記録はありませんでした。通知は行いません。")

    # commit・push は呼び出し経路（GitHub Actions／タスクスケジューラ／手動実行）に
    # よらず、必ずここで完結させる（詳細はgit_commit_and_pushのdocstring参照）。
    # 新しい記録が無い回でも「データ取得日」表示は毎回更新されるため、
    # 基本的に毎回ここでcommit・pushが走る。
    git_commit_and_push(ROSTER_PATH)

    # 変更があったかどうかをシェル側（ワークフロー）が判断できるよう、
    # 終了コードではなくファイルの有無で伝える（git diffで検知するため不要だが、
    # ログ上で分かりやすくするために明示しておく）
    if total_added == 0:
        log("EXIT: no_changes")
    else:
        log("EXIT: changes_made")


if __name__ == "__main__":
    main()

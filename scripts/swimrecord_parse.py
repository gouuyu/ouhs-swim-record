import re

STYLE_MAP = {
    "自由形": "自由形",
    "平泳ぎ": "平泳ぎ",
    "背泳ぎ": "背泳ぎ",
    "ﾊﾞﾀﾌﾗｲ": "バタフライ",
    "個人ﾒﾄﾞﾚｰ": "個人メドレー",
}
RELAY_STYLES = {"ﾌﾘｰﾘﾚｰ", "ﾒﾄﾞﾚｰﾘﾚｰ"}
WATERWAY_MAP = {"短": "短水路", "長": "長水路"}


def _parse_one_page(body):
    """body: text already cut down to start right after one '絞込検索' token
    (or right at the first record line) and before that page's pagination block."""
    lines = [l.strip() for l in body.split('\n')]
    lines = [l for l in lines if l != '']  # drop blank lines

    date_re = re.compile(r'^\d{4}/\d{2}/\d{2}$')
    date_idxs = [idx for idx, l in enumerate(lines) if date_re.match(l)]

    entries = []
    for k, i in enumerate(date_idxs):
        date = lines[i]
        kind = lines[i + 1]        # 公式
        style_raw = lines[i + 2]
        distance = lines[i + 3]
        waterway_raw = lines[i + 4]
        time_val = lines[i + 5]
        # Everything from i+6 up to (but not including) the next date line is:
        # an optional 'R' marker, optional annotation tokens (大会新/日本新/等), then the game name
        # as the LAST line in that span.
        end = date_idxs[k + 1] if k + 1 < len(date_idxs) else len(lines)
        tail = lines[i + 6:end]
        game_raw = tail[-1] if tail else ''
        is_relay = style_raw in RELAY_STYLES
        style = STYLE_MAP.get(style_raw, style_raw)
        entries.append(dict(
            date=date, style=style, style_raw=style_raw, distance=distance + 'm',
            waterway=WATERWAY_MAP.get(waterway_raw, waterway_raw), time=time_val,
            game_raw=game_raw, is_relay=is_relay
        ))
    return entries


def parse_swimrecord_page(text):
    """Parse the record-list portion of ONE page of a スイマーページ get_page_text /
    copy-paste dump. Returns list of dicts:
    date(YYYY/MM/DD), style, distance, waterway, time, game_raw(raw), is_relay(bool)
    """
    start = text.find('絞込検索')
    if start == -1:
        start = 0
    else:
        start += len('絞込検索')
    body = text[start:]
    # stop at pagination block (lines of just '<' / digits / '>') - find first line that's just '<'
    end_m = re.search(r'\n<\n', body)
    if end_m:
        body = body[:end_m.start()]
    return _parse_one_page(body)


def parse_swimrecord_text(text):
    """Parse possibly-MULTIPLE pages pasted together into one file (e.g. the user visited
    page 1, 2, 3... of a swimmer's record list and copy-pasted each page's text into the
    same file, one after another). Splits on every '絞込検索' occurrence and parses each
    page independently, then concatenates all entries in the order found.

    Safe to use even for a single-page paste -- behaves the same as parse_swimrecord_page
    in that case.
    """
    chunks = text.split('絞込検索')
    entries = []
    for chunk in chunks[1:] if len(chunks) > 1 else chunks:
        end_m = re.search(r'\n<\n', chunk)
        body = chunk[:end_m.start()] if end_m else chunk
        entries.extend(_parse_one_page(body))
    return entries

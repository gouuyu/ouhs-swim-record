import json

TAG_OPEN = '<script id="roster-data" type="application/json">'
TAG_CLOSE = '</script>'


def load_roster(roster_path):
    with open(roster_path, encoding='utf-8') as f:
        content = f.read()
    start = content.find(TAG_OPEN)
    if start == -1:
        raise RuntimeError('roster-data tag not found in ' + roster_path)
    start += len(TAG_OPEN)
    end = content.find(TAG_CLOSE, start)
    if end == -1:
        raise RuntimeError('roster-data closing tag not found in ' + roster_path)
    json_text = content[start:end]
    data = json.loads(json_text)
    return content, (start, end), data


def save_roster(roster_path, content, span, data):
    start, end = span
    new_json = json.dumps(data, ensure_ascii=False, separators=(', ', ': '))
    new_content = content[:start] + new_json + content[end:]
    with open(roster_path, 'w', encoding='utf-8') as f:
        f.write(new_content)


def time_to_seconds(t):
    """'1:09.41' -> 69.41 ; '32.51' -> 32.51"""
    t = t.strip()
    if ':' in t:
        mm, ss = t.split(':')
        return int(mm) * 60 + float(ss)
    return float(t)


def academic_year(calendar_year, month):
    return calendar_year - 1 if month <= 3 else calendar_year


def mmdd(datestr):
    """'2026/06/14' -> '06/14'"""
    y, m, d = datestr.split('/')
    return f"{int(m):02d}/{int(d):02d}"


def seconds_to_time(secs):
    """69.41 -> '1:09.41' ; 32.5 -> '0:32.50' (matches the 'cum'/'split' time strings
    already used throughout the roster data, which always include the minute part,
    even "0:", unlike the main 'time' field which drops it under a minute)."""
    secs = round(secs, 2)
    if secs < 0:
        secs = 0.0
    m = int(secs // 60)
    s = secs - m * 60
    return f"{m}:{s:05.2f}"


def recompute_by_stroke(data):
    """Recompute each swimmer's byStroke (the career-personal-best cards shown on the
    main roster page) from their full 'history' list. This MUST be called after any
    merge that adds new history entries, because byStroke is a separate, pre-computed
    view -- appending to history alone does not update it, and a newly-added swim that
    happens to be a new personal best would otherwise silently fail to show up in the
    main "best time" cards. (This exact staleness bug was found and fixed once already
    in this project -- see the 2026-11 roster update notes.)

    For each (style, distance, waterway) combination, keeps whichever entry already in
    byStroke OR in history has the fastest 'seconds' value, preferring to keep an
    existing byStroke entry's richer fields (finaPoint/ranking/raceCount/laps, which
    typically come from the original SEIKO-PDF extraction and are not available from
    SwimRecord Mobile) whenever that entry is still the fastest -- i.e. this never
    discards finaPoint/laps data it doesn't have to.

    Mutates `data` in place. Returns a list of (name, style, distance, waterway,
    old_time_or_None, new_time) tuples describing every change made, for review.
    """
    changes = []
    for s in data:
        hist = s.get('history', [])
        bs = s.setdefault('byStroke', {})

        # best-by-key purely from history (always has at least date/time/game/seconds)
        best_hist = {}
        for h in hist:
            key = (h['style'], h['distance'], h['waterway'])
            if key not in best_hist or h['seconds'] < best_hist[key]['seconds']:
                best_hist[key] = h

        for style, entries in bs.items():
            for i, b in enumerate(entries):
                key = (style, b['distance'], b['waterway'])
                hb = best_hist.get(key)
                if hb and hb['seconds'] < b['seconds'] - 1e-6:
                    new_entry = {
                        'year': hb['year'], 'waterway': hb['waterway'], 'style': hb['style'],
                        'distance': hb['distance'], 'time': hb['time'],
                        'finaPoint': hb.get('finaPoint'), 'date': hb['date'], 'game': hb['game'],
                        'ranking': hb.get('ranking'), 'raceCount': hb.get('raceCount', 1),
                        'seconds': hb['seconds'],
                    }
                    if 'laps' in hb:
                        new_entry['laps'] = hb['laps']
                    if 'reaction' in hb:
                        new_entry['reaction'] = hb['reaction']
                    changes.append((s['name'], style, b['distance'], b['waterway'], b['time'], hb['time']))
                    entries[i] = new_entry
    return changes

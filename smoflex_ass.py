r"""Clean an ASS/SSA subtitle for the 3DS player, rebuilding animated song karaoke as plain lines.

    smoflex_ass.py in.ass out.srt [--report]

(Vendored from clownsec-moflex-encoder's ass_to_srt.py so this app has no outside dependency.
The builder uses write_ass(): an ASS track embedded raw -- karaoke layers and all -- can pass
the 1 MB a SUB section may hold, and the player then skips the whole track.)

Letting ffmpeg convert an anime ASS track wholesale produces nonsense: Re:Zero S02E02 came out
as 30,589 "cues" where the episode has 393 lines of dialogue. The opening theme is typeset as
per-FRAME karaoke -- 27,994 events, each 0.04 s long, each drawing ONE syllable with its own
blur and alpha so the glow animates. On a 3DS that renders as a strobing single letter, and it
buries the dialogue.

What survives as it is:
  * events whose text, once override tags are stripped, is non-empty
  * events at least MIN_DUR long -- per-frame karaoke is 0.04 s, real dialogue is never that
  * one copy of each (start, end, text): karaoke draws the same glyph twice, fill and shadow

Signs are KEPT. They are real information (a letter, a sign, a caption) and are timed like
dialogue, so the duration rule leaves them alone.

ANIMATION STYLES -- a style made of hundreds of tiny pieces, or of Aegisub templater output
("Dialogue: ...,fx,") -- are not dropped: every line they draw is REBUILT as one plain line.
  * Pieces are grouped by layer, row and tag signature (a letter layer and a syllable layer
    of the same song never mix), copies of a glyph are merged, and a line ends when the
    glyphs jump back to the left or something else is drawn where a glyph stood.
  * Words are split where the song's own space pieces sit, or else where the gap between two
    glyphs is wider than the letter gaps of that song (widths estimated per character).
  * TIMING always comes from the pieces: that is what the release shows in time with the music.
    The templater's source lines ("Comment: ...,karaoke,{\k31}a{\k15}shi") can be a second off
    (High School DxD S4: OP +1.00 s, ED +0.70 s on every line).
  * WORDING comes from a matching source line when there is one -- the fansub's own text.
  * A source line with no rebuilt match is still kept, moved by the offset measured on the lines
    that did match.
Validated against the source lines: High School DxD S4E02 (letter + syllable layers) and Re:Zero
S2E02 (per-frame syllables, per-frame whole lines) -- 65 of 65 lines word-for-word.
"""
import difflib, os, re, statistics, sys

MIN_DUR = 0.25            # seconds; per-frame karaoke is 0.04
TAG = re.compile(r'\{[^}]*\}')
DRAW = re.compile(r'\\p[1-9]')          # vector drawing commands, not text
KTAG = re.compile(r'\\(?:kf|ko|k|K)\d+(?:\.\d+)?')   # karaoke syllable timing: \k31 \K57 \kf20 \ko15
POS = re.compile(r'\\(?:pos|move)\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)')
TAGNAME = re.compile(r'\\(\d?[a-zA-Z]+)')


def ts(v):
    h, m, s = v.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)


def ass_ts(t):
    cs = max(0, int(round(t * 100)))
    return f'{cs//360000}:{cs//6000%60:02d}:{cs//100%60:02d}.{cs%100:02d}'


def srt_ts(t):
    ms = int(round(t * 1000))
    return f'{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}'


def plain(text):
    return TAG.sub('', text).replace('\\N', '\n').replace('\\n', '\n').replace('\\h', ' ').strip()


def parse(path, kind='Dialogue:'):
    fmt, events = None, []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')
            if line.startswith('Format:') and fmt is None and 'Text' in line:
                fmt = [x.strip() for x in line.split(':', 1)[1].split(',')]
            elif line.startswith(kind) and fmt:
                vals = line.split(':', 1)[1].split(',', len(fmt) - 1)
                events.append(dict(zip(fmt, [v.strip() for v in vals])))
    return events


def header(path):
    """PlayRes and each style's font size and alignment."""
    resx, resy, sty, fmt = 384.0, 288.0, {}, None
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if line.startswith('PlayResX:'):
                try: resx = float(line.split(':')[1])
                except ValueError: pass
            elif line.startswith('PlayResY:'):
                try: resy = float(line.split(':')[1])
                except ValueError: pass
            elif line.startswith('Format:') and 'Fontsize' in line:
                fmt = [x.strip() for x in line.split(':', 1)[1].split(',')]
            elif line.startswith('Style:') and fmt:
                v = [x.strip() for x in line.split(':', 1)[1].split(',')]
                try:
                    sty[v[fmt.index('Name')]] = float(v[fmt.index('Fontsize')])
                except (ValueError, IndexError):
                    pass
            elif line.startswith('[Events]'):
                break
    return resx, resy, sty


def unkaraoke(text):
    r"""Remove the per-syllable timing, keep every other tag (\fad, \pos...); drop emptied {}."""
    return re.sub(r'\{\}', '', KTAG.sub('', text))


def karaoke_styles(events):
    """Styles that are an effect layer, not subtitles -- decided from the events themselves.

    Matching on style NAMES ('op1-rom', 'kara') only works until a release names them something
    else. A per-frame karaoke layer is unmistakable in the data: hundreds of events, almost all
    far too short to read and only a character or two long.
    """
    stats = {}
    for e in events:
        raw = TAG.sub('', e.get('Text', '')).strip()
        try:
            dur = ts(e['End']) - ts(e['Start'])
        except Exception:
            continue
        s = stats.setdefault(e.get('Style', ''), {'n': 0, 'short': 0, 'tiny': 0})
        s['n'] += 1
        s['short'] += dur < MIN_DUR
        s['tiny'] += len(raw) <= 2
    out = set()
    for style, s in stats.items():
        if s['n'] >= 50 and (s['short'] / s['n'] > 0.5 or s['tiny'] / s['n'] > 0.5):
            out.add(style)
    return out


def is_fx(e):
    return e.get('Effect', '').strip().lower() == 'fx'


# ---------------- rebuilding lines from animation pieces ----------------

NARROW, SEMI, WIDE = set("ijlI'.,!|:;`"), set('tfr()-"'), set('mwMW')


def em(t):
    """Rough advance of a run of text, in em: enough to tell a word gap from a letter gap."""
    w = 0.0
    for c in t:
        w += (0.28 if c in NARROW else 0.38 if c in SEMI else 0.85 if c in WIDE
              else 0.30 if c == ' ' else 0.66 if c.isupper() else 0.52)
    return w


def rows_of(ys, rowh):
    """y -> row id: values closer than half a row apart share a row."""
    rows, rid, last = {}, -1, None
    for y in sorted(set(ys)):
        if last is None or y - last > rowh * 0.5:
            rid += 1
        rows[y] = rid
        last = y
    return rows


def rebuild_style(pieces, resx, resy, fsize):
    """Every line one animation style draws, as [(start, end, text, y)]."""
    GAP = 0.3
    tol = max(2.0, fsize * 0.12)       # the same glyph: animated copies drift a few pixels
    pts = []
    for e in pieces:
        raw = e.get('Text', '')
        if DRAW.search(raw):
            continue
        m = POS.search(raw)
        x, y = (float(m.group(1)), float(m.group(2))) if m else (resx / 2, resy * 0.9)
        try:
            a, b = ts(e['Start']), ts(e['End'])
        except Exception:
            continue
        t = TAG.sub('', raw).replace('\\N', ' ').replace('\\n', ' ').replace('\\h', ' ')
        if not t:
            continue
        sig = frozenset(TAGNAME.findall(''.join(re.findall(r'\{[^}]*\}', raw))))
        pts.append((a, b, x, y, t, e.get('Layer', '0').strip(), sig))
    rowid = rows_of([p[3] for p in pts], resy * 0.04)
    groups, marks = {}, {}
    for a, b, x, y, t, layer, sig in pts:
        row = rowid[y]
        if not t.strip():                             # a space piece: where a word ends
            marks.setdefault(row, []).append((x, a, b))
        else:
            groups.setdefault((layer, sig, row), []).append((a, b, x, y, t))
    best = {}
    for (layer, sig, row), ps in groups.items():
        ps.sort(key=lambda p: (round(p[0], 2), p[2]))
        lines = []
        for a, b, x, y, t in ps:
            ln = lines[-1] if lines else None
            hit = None                                # an unbroken copy of a glyph already placed
            for L in reversed(lines[-3:]):
                for sl in L['slots']:
                    if sl[1] == t and abs(sl[0] - x) <= tol and a <= sl[2] + GAP:
                        hit = (L, sl)
                        break
                if hit:
                    break
            if hit:
                hit[1][2] = max(hit[1][2], b)
                hit[0]['e'] = max(hit[0]['e'], b)
                continue
            new = ln is None or a > ln['e'] + GAP
            if not new:
                if any(abs(sl[0] - x) <= tol * 0.5 and sl[1] != t for sl in ln['slots']):
                    new = True                        # something else already stands here
                elif x < max(sl[0] for sl in ln['slots']) - tol and a > ln['s'] + 0.05:
                    new = True                        # back to the left: the next line
            if new:
                ln = {'s': a, 'e': b, 'slots': [], 'y': y}
                lines.append(ln)
            ln['slots'].append([x, t, b])
            ln['e'] = max(ln['e'], b)
        lines = [l for l in lines if l['e'] - l['s'] >= MIN_DUR
                 and len(re.sub(r'\s', '', ''.join(sl[1] for sl in l['slots']))) >= 2]
        score = sum(len(sl[1].strip()) for l in lines for sl in l['slots'])
        if score > best.get(row, (-1, None))[0]:      # the most complete layer of each row
            best[row] = (score, lines)
    out = []
    for row, (_, lines) in best.items():
        out += join_words(lines, marks.get(row, []))
    return merge_repeats(sorted(out))


def join_words(lines, marks):
    """Join each line's glyphs left to right, with a space where a word ends."""
    out, pairs = [], []
    for l in lines:
        sl = sorted(l['slots'], key=lambda s: s[0])
        texts = {s[1] for s in sl}
        if len(texts) == 1 and len(next(iter(texts)).strip()) >= 3:   # one line drawn at several spots
            out.append((l['s'], l['e'], re.sub(r'\s+', ' ', next(iter(texts))).strip(), l['y']))
            continue
        l['sl'] = sl
        for s1, s2 in zip(sl, sl[1:]):
            pairs.append((s2[0] - s1[0], (em(s1[1].strip()) + em(s2[1].strip())) / 2))
    k = None                                           # px per em, fitted on letter-to-letter pairs
    r = sorted(d / w for d, w in pairs if w > 0 and d > 0)
    if r:
        k = r[len(r) // 4]
    thr = otsu(sorted(d / k - w for d, w in pairs), 0.18) if k and not marks else 0.18
    for l in lines:
        if 'sl' not in l:
            continue
        sl = l['sl']
        mk = [x for x, a, b in marks if b >= l['s'] - 0.5 and a <= l['e'] + 0.5]
        s = sl[0][1]
        for s1, s2 in zip(sl, sl[1:]):
            if mk:
                sp = any(s1[0] < x < s2[0] for x in mk)
            elif k:
                sp = (s2[0] - s1[0]) / k - (em(s1[1].strip()) + em(s2[1].strip())) / 2 > thr
            else:
                sp = False
            s += (' ' if sp or s1[1].endswith(' ') or s2[1].startswith(' ') else '') + s2[1]
        out.append((l['s'], l['e'], re.sub(r'\s+', ' ', s).strip(), l['y']))
    return out


def otsu(vals, default):
    """Split gaps into 'inside a word' and 'between words' at the threshold that best separates them."""
    if len(vals) < 8:
        return default
    best, bt = -1.0, default
    for i in range(1, len(vals)):
        lo, hi = vals[:i], vals[i:]
        v = len(lo) * len(hi) * (sum(hi) / len(hi) - sum(lo) / len(lo)) ** 2
        if v > best:
            best, bt = v, (vals[i - 1] + vals[i]) / 2
    return max(bt, 0.08)


def merge_repeats(lines):
    """The same words drawn twice back to back (two copies of a sign) become one line."""
    out = []
    for a, b, t, y in lines:
        if out and out[-1][2] == t and a <= out[-1][1] + 0.3:
            pa, pb, pt, py = out[-1]
            out[-1] = (pa, max(pb, b), pt, py)
        else:
            out.append((a, b, t, y))
    return out


def norm(t):
    return re.sub(r'\s+', ' ', t).strip().lower()


def plan(path):
    """Decide what happens to every animation style.

    Returns (drop, lines, report):
      drop(e)  -> True for a Dialogue event that is an animation piece
      lines    -> [(start, end, style, text, top)] plain lines to add
      report   -> {'rebuilt': n, 'worded': n, 'from_source': n, 'lost': [styles]}
    """
    resx, resy, fsizes = header(path)
    dia = parse(path)
    com = [c for c in parse(path, 'Comment:')
           if plain(unkaraoke(c.get('Text', ''))) and not c.get('Effect', '').strip().lower().startswith(('template', 'code'))]
    kst = karaoke_styles(dia)
    fxst = {e.get('Style', '') for e in dia if is_fx(e)}
    anim = kst | fxst
    styles_with_dia = {e.get('Style', '') for e in dia}

    def drop(e):
        st = e.get('Style', '')
        return st in kst or (st in fxst and is_fx(e))

    lines, rep = [], {'rebuilt': 0, 'worded': 0, 'from_source': 0, 'lost': []}
    src_styles = {c.get('Style', '') for c in com}
    for st in sorted(anim | {s for s in src_styles if s not in styles_with_dia}):
        pieces = [e for e in dia if e.get('Style', '') == st and drop(e)]
        got = rebuild_style(pieces, resx, resy, fsizes.get(st, 48.0)) if pieces else []
        srcs = []
        for c in com:
            if c.get('Style', '') != st:
                continue
            # a source line: a templater 'karaoke' comment, a comment carrying \k timing, or any
            # comment of a style that is animation (whose words would otherwise be lost)
            if (c.get('Effect', '').strip().lower() == 'karaoke' or KTAG.search(c.get('Text', ''))
                    or st in anim):
                try:
                    srcs.append((ts(c['Start']), ts(c['End']), plain(unkaraoke(c['Text'])).replace('\n', ' ')))
                except Exception:
                    pass
        srcs = sorted(set(srcs))
        used, offs = set(), []
        for i, (a, b, t, y) in enumerate(got):        # wording from the matching source line
            best, bs = None, 0.0
            for j, (sa, sb, stx) in enumerate(srcs):
                if j in used or abs(sa - a) > 3.0:
                    continue
                r = difflib.SequenceMatcher(None, norm(t), norm(stx)).ratio()
                if r > bs:
                    best, bs = j, r
            if best is not None and bs >= 0.8:
                used.add(best)
                offs.append(a - srcs[best][0])
                if norm(srcs[best][2]) != norm(t):
                    rep['worded'] += 1
                t = srcs[best][2]
            lines.append((a, b, st, t, y < resy / 2))
            rep['rebuilt'] += 1
        shift = statistics.median(offs) if offs else 0.0
        top = (sum(1 for g in got if g[3] < resy / 2) >= len(got) / 2) if got else True
        for j, (sa, sb, stx) in enumerate(srcs):      # source lines nothing on screen matched
            if j not in used:
                lines.append((sa + shift, sb + shift, st, stx, top))
                rep['from_source'] += 1
        if st in anim and not got and not srcs:
            rep['lost'].append(st)
    return drop, lines, rep


# ---------------- output ----------------

def convert(path, report=False):
    drop, extra, rep = plan(path)
    events = [e for e in parse(path) if not drop(e)]
    kept, seen, dropped = [], set(), {'short': 0, 'empty': 0, 'dupe': 0, 'draw': 0}
    per_style = {}
    for e in events:
        style = e.get('Style', '')
        per_style.setdefault(style, [0, 0])
        per_style[style][0] += 1
        raw = e.get('Text', '')
        if DRAW.search(raw):
            dropped['draw'] += 1; continue
        txt = plain(raw)
        if not txt:
            dropped['empty'] += 1; continue
        try:
            a, b = ts(e['Start']), ts(e['End'])
        except Exception:
            continue
        if b - a < MIN_DUR:
            dropped['short'] += 1; continue
        key = (round(a, 2), round(b, 2), txt)
        if key in seen:
            dropped['dupe'] += 1; continue
        seen.add(key)
        kept.append((a, b, txt))
        per_style[style][1] += 1
    for a, b, st, t, top in extra:
        kept.append((a, b, t))
        per_style.setdefault(st, [0, 0])[1] += 1
    kept.sort(key=lambda x: (x[0], x[1]))
    if report:
        print(f'  {len(events)} events -> {len(kept)} cues '
              f'(dropped {dropped["short"]} short, {dropped["dupe"]} duplicate, '
              f'{dropped["empty"]} empty, {dropped["draw"]} drawings; '
              f'{rep["rebuilt"] + rep["from_source"]} song line(s) rebuilt)')
        for s, (tot, k) in sorted(per_style.items(), key=lambda x: -x[1][0]):
            print(f'      {s or "(none)":24s} {tot:6d} -> {k:5d}')
    convert.report = rep
    return kept


DROP_SECTIONS = ('[fonts]', '[graphics]', '[aegisub project garbage]', '[aegisub extradata]')


def write_ass(src, dst, report=False):
    """Write a CLEANED copy of an ASS file, still ASS, for a player that renders ASS itself.

    The header and styles are copied as they are. Dropped: Dialogue lines convert() drops, the
    animation pieces (replaced by the plain lines plan() rebuilds), Comment lines, and the
    [Fonts]/[Graphics] sections -- uuencoded font files that can run to megabytes inside a
    subtitle track and that no 3DS will ever load.
    Returns the number of Dialogue lines written.
    """
    drop, extra, rep = plan(src)
    events = parse(src)
    seen, kept, k = set(), 0, 0
    out, fmt, skip, fields, last_ev = [], None, False, [], None
    with open(src, encoding='utf-8', errors='replace') as f:
        for line in f:
            s = line.rstrip('\n').lstrip('\ufeff')
            if s.startswith('['):
                skip = s.strip().lower() in DROP_SECTIONS
            if skip:
                continue
            if s.startswith('Format:') and fmt is None and 'Text' in s:
                fmt = True
                fields = [x.strip() for x in s.split(':', 1)[1].split(',')]
                last_ev = len(out)
            if s.startswith('Comment:'):
                continue
            if s.startswith('Dialogue:') and fmt:
                e = events[k]; k += 1                     # parse() saw exactly these, in this order
                raw = e.get('Text', '')
                txt = plain(raw)
                try:
                    a, b = ts(e['Start']), ts(e['End'])
                except Exception:
                    continue
                key = (round(a, 2), round(b, 2), txt)       # one copy per line, the first layer wins
                if drop(e) or DRAW.search(raw) or not txt or b - a < MIN_DUR or key in seen:
                    continue
                seen.add(key); kept += 1
                out.append(s)
                last_ev = len(out)
                continue
            out.append(s)
    if extra and fields:
        add = []
        for a, b, st, t, top in sorted(extra):
            v = {'Layer': '0', 'Start': ass_ts(a), 'End': ass_ts(b), 'Style': st, 'Name': '',
                 'MarginL': '0', 'MarginR': '0', 'MarginV': '0', 'Effect': '',
                 'Text': ('{\\an8}' if top else '{\\an2}') + t.replace('\n', '\\N')}
            add.append('Dialogue: ' + ','.join(v.get(fn, '') for fn in fields))
        out[last_ev:last_ev] = add
        kept += len(add)
    with open(dst, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    if report:
        print(f'  {len(events)} events -> {kept} kept as ASS '
              f'({rep["rebuilt"] + rep["from_source"]} song line(s) rebuilt)')
    write_ass.report = rep
    return kept


def write_srt(cues, path):
    with open(path, 'w', encoding='utf-8') as f:
        for i, (a, b, txt) in enumerate(cues, 1):
            f.write(f'{i}\n{srt_ts(a)} --> {srt_ts(b)}\n{txt}\n\n')
    return len(cues)


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    n = write_srt(convert(args[0], report='--report' in sys.argv), args[1])
    print(f'  wrote {n} cues to {os.path.basename(args[1])}')

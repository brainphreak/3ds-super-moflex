r"""Convert an ASS/SSA subtitle to SRT, dropping the karaoke effect layers.

    smoflex_ass.py in.ass out.srt [--report]

(Vendored from clownsec-moflex-encoder's ass_to_srt.py so this app has no outside dependency.
The builder uses write_ass(): an ASS track embedded raw -- karaoke layers and all -- can pass
the 1 MB a SUB section may hold, and the player then skips the whole track.)

Letting ffmpeg convert an anime ASS track wholesale produces nonsense: Re:Zero S02E02 came out
as 30,589 "cues" where the episode has 393 lines of dialogue. The opening theme is typeset as
per-FRAME karaoke -- 27,994 events, each 0.04 s long, each drawing ONE character with its own
blur and alpha so the glow animates. On a 3DS that renders as a strobing single letter, and it
buries the dialogue.

What survives:
  * events whose text, once override tags are stripped, is non-empty
  * events at least MIN_DUR long -- per-frame karaoke is 0.04 s, real dialogue is never that
  * one copy of each (start, end, text): karaoke draws the same glyph twice, fill and shadow

Signs are KEPT. They are real information (a letter, a sign, a caption) and are timed like
dialogue, so the duration rule leaves them alone.

Karaoke is REBUILT, not lost. A fansub's song lyrics are written once as ordinary lines --
"Comment: ...,karaoke,{\k31}a{\k15}shi{\k27}ta" -- and Aegisub's templater generates the
animation from them as thousands of "Dialogue: ...,fx," pieces. The pieces are what the rules
above drop (and the few long enough to survive came out as stray letters: "Swit", "o", "n").
So wherever a style has those source lines, its fx pieces go and the source lines come back as
plain Dialogue with the timing tags removed: every sung line, once, where the style puts it.
"""
import os, re, sys

MIN_DUR = 0.25            # seconds; per-frame karaoke is 0.04
TAG = re.compile(r'\{[^}]*\}')
DRAW = re.compile(r'\\p[1-9]')          # vector drawing commands, not text
KTAG = re.compile(r'\\(?:kf|ko|k|K)\d+(?:\.\d+)?')   # karaoke syllable timing: \k31 \K57 \kf20 \ko15


def ts(v):
    h, m, s = v.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)


def srt_ts(t):
    ms = int(round(t * 1000))
    return f'{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}'


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


def is_karaoke_src(e):
    """A karaoke SOURCE line: a Comment the templater reads, with real words in it."""
    return e.get('Effect', '').strip().lower() == 'karaoke' and TAG.sub('', e.get('Text', '')).strip()


def unkaraoke(text):
    r"""Remove the per-syllable timing, keep every other tag (\fad, \pos...); drop emptied {}."""
    return re.sub(r'\{\}', '', KTAG.sub('', text))


def karaoke_source_styles(path):
    return {e.get('Style', '') for e in parse(path, 'Comment:') if is_karaoke_src(e)}


def is_generated_fx(e, src_styles):
    """A templater-generated animation piece of a style whose source lines we restore."""
    return e.get('Effect', '').strip().lower() == 'fx' and e.get('Style', '') in src_styles


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


def convert(path, report=False):
    srcs = [e for e in parse(path, 'Comment:') if is_karaoke_src(e)]
    src_styles = {e.get('Style', '') for e in srcs}
    events = [e for e in parse(path) if not is_generated_fx(e, src_styles)]
    effects = karaoke_styles(events) - src_styles
    for e in srcs:                                     # the song lines, back as ordinary cues
        e = dict(e); e['Text'] = unkaraoke(e.get('Text', '')); events.append(e)
    kept, seen, dropped = [], set(), {'short': 0, 'empty': 0, 'dupe': 0, 'draw': 0, 'fx': 0}
    per_style = {}
    for e in events:
        style = e.get('Style', '')
        per_style.setdefault(style, [0, 0])
        per_style[style][0] += 1
        if style in effects:
            dropped['fx'] += 1; continue
        raw = e.get('Text', '')
        if DRAW.search(raw):
            dropped['draw'] += 1; continue
        txt = TAG.sub('', raw).replace('\\N', '\n').replace('\\n', '\n').replace('\\h', ' ')
        txt = txt.strip()
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

    kept.sort(key=lambda x: (x[0], x[1]))
    if report:
        print(f'  {len(events)} events -> {len(kept)} cues '
              f'(dropped {dropped["short"]} short, {dropped["dupe"]} duplicate, '
              f'{dropped["empty"]} empty, {dropped["draw"]} drawings)')
        for s, (tot, k) in sorted(per_style.items(), key=lambda x: -x[1][0]):
            if tot:
                print(f'      {s or "(none)":24s} {tot:6d} -> {k:5d}')
    return kept


DROP_SECTIONS = ('[fonts]', '[graphics]', '[aegisub project garbage]', '[aegisub extradata]')


def write_ass(src, dst, report=False):
    """Write a CLEANED copy of an ASS file, still ASS: the same events convert() would keep, with
    their styling and override tags intact, for a player that renders ASS itself.

    The header and styles are copied as they are. Dropped: every Dialogue line convert() drops,
    Comment lines, and the [Fonts]/[Graphics] sections -- uuencoded font files that can run to
    megabytes inside a subtitle track and that no 3DS will ever load.
    Returns the number of Dialogue lines kept.
    """
    events = parse(src)
    src_styles = karaoke_source_styles(src)
    effects = karaoke_styles([e for e in events if not is_generated_fx(e, src_styles)]) - src_styles
    seen, kept, k, restored = set(), 0, 0, 0
    out, fmt, skip = [], None, False
    fields = []
    with open(src, encoding='utf-8', errors='replace') as f:
        for line in f:
            s = line.rstrip('\n').lstrip('﻿')
            if s.startswith('['):
                skip = s.strip().lower() in DROP_SECTIONS
            if skip:
                continue
            if s.startswith('Format:') and fmt is None and 'Text' in s:
                fmt = True
                fields = [x.strip() for x in s.split(':', 1)[1].split(',')]
            if s.startswith('Comment:'):
                # a karaoke source line comes back as a plain Dialogue; every other comment goes
                if not (fmt and src_styles):
                    continue
                vals = s.split(':', 1)[1].split(',', len(fields) - 1)
                e = dict(zip(fields, [v.strip() for v in vals]))
                if not is_karaoke_src(e):
                    continue
                txt_raw = unkaraoke(e.get('Text', ''))
                txt = TAG.sub('', txt_raw).replace('\\N', '\n').replace('\\n', '\n').replace('\\h', ' ').strip()
                try:
                    a, b = ts(e['Start']), ts(e['End'])
                except Exception:
                    continue
                key = (round(a, 2), round(b, 2), txt)
                if not txt or b - a < MIN_DUR or key in seen:
                    continue
                seen.add(key); kept += 1; restored += 1
                e['Effect'] = ''
                e['Text'] = txt_raw
                out.append('Dialogue: ' + ','.join(e.get(fn, '') for fn in fields))
                continue
            if s.startswith('Dialogue:') and fmt:
                e = events[k]; k += 1                     # parse() saw exactly these, in this order
                raw = e.get('Text', '')
                txt = TAG.sub('', raw).replace('\\N', '\n').replace('\\n', '\n').replace('\\h', ' ').strip()
                try:
                    a, b = ts(e['Start']), ts(e['End'])
                except Exception:
                    continue
                key = (round(a, 2), round(b, 2), txt)       # convert()'s key: one copy per line,
                                                            # the first layer (the fill) wins
                if (e.get('Style', '') in effects or is_generated_fx(e, src_styles) or DRAW.search(raw)
                        or not txt or b - a < MIN_DUR or key in seen):
                    continue
                seen.add(key); kept += 1
            out.append(s)
    with open(dst, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    if report:
        print(f'  {len(events)} events -> {kept} kept as ASS ({restored} karaoke line(s) rebuilt)')
    write_ass.restored = restored
    write_ass.lost = sorted(effects)       # effect styles with no source lines to rebuild from
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

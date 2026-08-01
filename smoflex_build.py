#!/usr/bin/env python3
"""smoflex_build — the SUPER MOFLEX build pipeline (importable; used by the GUI and CLI).

Turns (source video + encoded .moflex + options) into a self-contained SUPER MOFLEX:
  * English audio in-band (what the OFFICIAL 3DS player plays)
  * original-language audio + every subtitle in a trailer only our player reads
  * optional TMDB metadata + poster embedded so the library needs no scraping

Requires ffmpeg / ffprobe on PATH. Pure-Python otherwise (Tkinter GUI ships with Python).
"""
import contextlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import wave

import smoflex_core as core

# ---- external tools (ffmpeg/ffprobe) -------------------------------------------------------
# Windows users rarely have ffmpeg on PATH, so we also look beside the app and in the usual
# install spots, and the GUI lets them point at it by hand.

FFMPEG = 'ffmpeg'
FFPROBE = 'ffprobe'
EXE = '.exe' if sys.platform == 'win32' else ''
# no flashing console windows for every child process on Windows
_NOWIN = {'creationflags': 0x08000000} if sys.platform == 'win32' else {}

def _search_dirs():
    here = os.path.dirname(os.path.abspath(__file__))
    return [here, os.path.join(here, 'ffmpeg'), os.path.join(here, 'ffmpeg', 'bin'),
            '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/opt/local/bin',
            r'C:\Program Files\ffmpeg\bin', r'C:\ffmpeg\bin']

def find_tools():
    """Locate ffmpeg/ffprobe. Returns (ffmpeg, ffprobe); either may be None."""
    found = []
    for name in ('ffmpeg', 'ffprobe'):
        p = shutil.which(name)
        if not p:
            for d in _search_dirs():
                c = os.path.join(d, name + EXE)
                if os.path.isfile(c):
                    p = c
                    break
        found.append(p)
    return tuple(found)

def set_tools(ffmpeg=None, ffprobe=None):
    """Pin the binaries (GUI 'Locate ffmpeg…'). Passing the ffmpeg path alone finds ffprobe
    next to it, which is how every ffmpeg distribution ships."""
    global FFMPEG, FFPROBE
    if ffmpeg:
        FFMPEG = ffmpeg
        if not ffprobe:
            c = os.path.join(os.path.dirname(ffmpeg), 'ffprobe' + EXE)
            if os.path.isfile(c):
                FFPROBE = c
    if ffprobe:
        FFPROBE = ffprobe

def tools_ready():
    for exe in (FFMPEG, FFPROBE):
        if not (shutil.which(exe) or os.path.isfile(exe)):
            return False
    return True

class Cancelled(Exception):
    """Raised when the caller's cancel flag is set; the GUI turns it into 'Cancelled'."""

def _run(cmd, cancel=None, capture=False):
    """subprocess.run with a cancel flag (threading.Event). Kills the child on cancel."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                         stderr=subprocess.PIPE, text=True, **_NOWIN)
    while True:
        try:
            out, err = p.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                p.kill()
                p.communicate()
                raise Cancelled()
    return p.returncode, (out or ''), (err or '')

class _LogStream:
    """File-like sink so the core builder's print() output lands in the GUI log."""
    def __init__(self, log):
        self.log = log
        self.buf = ''
    def write(self, s):
        self.buf += s
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            if line.strip():
                self.log(line.rstrip())
    def flush(self):
        if self.buf.strip():
            self.log(self.buf.rstrip())
        self.buf = ''

# ---- language code helpers ----------------------------------------------------------------

LANG3 = {          # 2-letter / loose -> 3-letter code used on the button + CC cycler
    'en': 'ENG', 'eng': 'ENG', 'ja': 'JPN', 'jpn': 'JPN', 'jp': 'JPN',
    'fr': 'FRE', 'fre': 'FRE', 'de': 'GER', 'ger': 'GER', 'it': 'ITA', 'ita': 'ITA',
    'es': 'SPA', 'spa': 'SPA', 'es-419': 'LAT', '419': 'LAT', 'pt': 'POR', 'por': 'POR',
    'ru': 'RUS', 'rus': 'RUS', 'ko': 'KOR', 'kor': 'KOR', 'zh': 'CHI', 'chi': 'CHI',
    'tr': 'TUR', 'tur': 'TUR', 'pl': 'POL', 'pol': 'POL', 'el': 'GRE', 'gre': 'GRE',
    'fi': 'FIN', 'fin': 'FIN', 'tl': 'TGL', 'tgl': 'TGL', 'nl': 'DUT', 'sv': 'SWE',
}
def to3(code):
    return LANG3.get(code.lower(), code.upper()[:3])

def ffprobe(path):
    rc, out, err = _run([FFPROBE, '-v', 'error', '-show_streams', '-of', 'json', path], capture=True)
    if rc != 0 or not out.strip():
        raise RuntimeError(f'ffprobe could not read {os.path.basename(path)}: {err.strip()[:200]}')
    return json.loads(out)['streams']

def parse_name(fn):
    b = re.sub(r'\.(mkv|mp4|moflex|m4v|mov)$', '', os.path.basename(fn), flags=re.I)
    ep = re.search(r'[. _]S(\d+)E(\d+)', b, re.I)
    se = (int(ep.group(1)), int(ep.group(2))) if ep else None
    head = b[:ep.start()] if ep else b
    ym = re.search(r'[(. _](19\d\d|20\d\d)[). _]', head)
    year = int(ym.group(1)) if ym else 0
    if ym:
        head = head[:ym.start()]
    return re.sub(r'[._]+', ' ', head).strip(' -'), year, se

# ---- TMDB (optional; the user supplies their own read token) -------------------------------

def tmdb(token, path, **params):
    q = '&'.join(f'{k}={urllib.request.quote(str(v))}' for k, v in params.items())
    req = urllib.request.Request(f'https://api.themoviedb.org/3/{path}' + (f'?{q}' if q else ''))
    req.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def fetch_meta(token, title, year, se, log):
    """Return an info dict from TMDB (best-effort; returns {} on any failure)."""
    try:
        info = {}
        if se:
            hit = tmdb(token, 'search/tv', query=title,
                       **({'first_air_date_year': year} if year else {}))['results'][0]
            det = tmdb(token, f"tv/{hit['id']}")
            info = dict(title=det['name'], year=int((det.get('first_air_date') or '0')[:4] or year or 0),
                        genres=[g['name'] for g in det['genres']], showdesc=det.get('overview', ''),
                        runtime=(det.get('episode_run_time') or [0])[0], poster=det.get('poster_path'))
            ep = tmdb(token, f"tv/{hit['id']}/season/{se[0]}/episode/{se[1]}")
            info['eptitle'] = ep.get('name', '')
            info['date'] = ep.get('air_date', '')
            info['desc'] = (f'S{se[0]}.E{se[1]} "{ep["name"]}" - {ep.get("overview","")}'
                            if ep.get('name') else info['showdesc'])
            if ep.get('runtime'):
                info['runtime'] = ep['runtime']
        else:
            hit = tmdb(token, 'search/movie', query=title, **({'year': year} if year else {}))['results'][0]
            det = tmdb(token, f"movie/{hit['id']}")
            info = dict(title=det['title'], year=int((det.get('release_date') or '0')[:4] or year or 0),
                        genres=[g['name'] for g in det['genres']], desc=det.get('overview', ''),
                        runtime=det.get('runtime') or 0, poster=det.get('poster_path'))
        log(f"TMDB: {info['title']} ({info.get('year')})  {', '.join(info.get('genres', []))}")
        return info
    except Exception as ex:
        log(f"TMDB lookup failed ({ex}) — continuing without it")
        return {}

IMAGE_SUBS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvdsub', 'xsub', 'pgssub'}

def _duration(video):
    """Container duration in seconds, or None if ffprobe won't say."""
    rc, out, _ = _run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                       '-of', 'default=nk=1:nw=1', video], capture=True)
    try:
        return float(out.strip()) if rc == 0 else None
    except ValueError:
        return None

_TS = re.compile(r'(\d\d):(\d\d):(\d\d)[,.](\d\d\d)')

def shift_srt(src, dst, offset):
    """Copy an .srt with every cue moved by `offset` seconds (cues that would land before
    zero are clamped to it)."""
    off_ms = int(round(offset * 1000))

    def bump(m):
        ms = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))) * 1000 \
             + int(m.group(4))
        ms = max(0, ms + off_ms)
        return f'{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}'

    with open(src, encoding='utf-8', errors='replace') as f:
        body = f.read()
    with open(dst, 'w', encoding='utf-8') as f:
        f.write(_TS.sub(bump, body))
    return dst

def probe_source(video):
    """Summarize a source file for the GUI: audio tracks, text subs, image subs.
    Returns {'audio': [(index, label)], 'subs': [(index, label)], 'imagesubs': n}."""
    streams = ffprobe(video)
    out = {'audio': [], 'subs': [], 'imagesubs': 0}
    for s in streams:
        tags = s.get('tags', {}) or {}
        lang = to3(tags.get('language', 'und'))
        if s['codec_type'] == 'audio':
            desc = f"{lang}  {s.get('codec_name','?')} {s.get('channels','?')}ch"
            if tags.get('title'):
                desc += f"  ({tags['title'][:40]})"
            out['audio'].append((s['index'], desc))
        elif s['codec_type'] == 'subtitle':
            if s.get('codec_name') in IMAGE_SUBS:
                out['imagesubs'] += 1
            else:
                out['subs'].append((s['index'], f"{lang}  {s.get('codec_name','?')}"))
    return out

def build(video, moflex, out_path=None, out_dir=None, extra_srts=None, boost=True,
          is3d=None, fmt='converted', tmdb_token=None, title=None, year=None, poster=None,
          audio_in=None, audio_alt=None, offset=0.0, srt_offset=0.0, meta=None,
          log=print, cancel=None):
    """Run the whole pipeline. Returns the output path. `log` is a callback for progress text,
    `cancel` an optional threading.Event that aborts at the next checkpoint."""
    extra_srts = extra_srts or []

    def ck():
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    if not tools_ready():
        raise RuntimeError('ffmpeg / ffprobe not found — install ffmpeg or point the app at it.')
    # scratch next to the output when we can: temp dirs are often on a small system volume and
    # the WAVs run ~600 MB per hour of audio.
    scratch_root = os.path.abspath(out_dir or os.path.dirname(os.path.abspath(out_path or moflex)))
    work = tempfile.mkdtemp(prefix='smoflex_',
                            dir=scratch_root if os.path.isdir(scratch_root) else None)
    try:
        t_parsed, y_parsed, se = parse_name(video)
        title = title or t_parsed
        year = year if year else y_parsed
        if is3d is None:
            enl = os.path.basename(moflex).lower()
            is3d = any(k in enl for k in ('(3d)', 'sbs', 'lrf', 'over-under', 'hsbs'))
        log(f"Title: {title!r}  Year: {year or '?'}  {'3D' if is3d else '2D'}  "
            + (f'S{se[0]:02d}E{se[1]:02d}' if se else '(movie)'))

        streams = ffprobe(video)
        audio = [s for s in streams if s['codec_type'] == 'audio']
        subs = [s for s in streams if s['codec_type'] == 'subtitle']
        if not audio:
            raise RuntimeError('source has no audio')
        byidx = {s['index']: s for s in audio}
        # in-band = the track stock players get (English by default); alt rides in the trailer
        a_eng = (byidx.get(audio_in) if audio_in is not None else None) or \
                next((s for s in audio if (s.get('tags', {}) or {}).get('language') == 'eng'), audio[0])
        if audio_alt is not None:
            a_alt = byidx.get(audio_alt) if audio_alt >= 0 else None      # -1 = none
        else:
            a_alt = next((s for s in audio if s is not a_eng), None)
        if a_alt is a_eng:
            a_alt = None
        alt_lang = to3((a_alt.get('tags', {}) or {}).get('language', 'und')) if a_alt else None

        # An encode often starts from a different master than the source file: a distributor
        # logo the rip lacks, a trimmed head, and so on. `offset` slides everything taken from
        # the source (both audio tracks and every subtitle) onto the encode's timeline.
        pre, post = [], []
        if offset > 0:
            post = ['-af', f'adelay={int(round(offset * 1000))}:all=1']
        elif offset < 0:
            pre = ['-ss', f'{-offset:.3f}']
        dur = _duration(video)
        if offset and dur:          # keep the length the source had: silence in at the head,
            post += ['-t', f'{dur:.3f}']   # the same amount pushed off the tail

        def wav(stream, tag):
            p = os.path.join(work, f'a.{tag}.wav')
            rc, _, err = _run([FFMPEG, '-y', '-v', 'error'] + pre + ['-i', video,
                               '-map', f"0:{stream['index']}", '-ac', '2', '-ar', '44100']
                              + post + ['-c:a', 'pcm_s16le', p], cancel)
            if rc != 0 or not os.path.exists(p) or os.path.getsize(p) < 1024:
                raise RuntimeError(f'audio extraction failed: {err.strip()[:200]}')
            return p
        lang_of = lambda s: (s.get('tags', {}) or {}).get('language', 'und')
        log(f"Extracting audio (in-band {to3(lang_of(a_eng))}"
            + (f" + trailer {alt_lang}" if a_alt else '') + ') — this is the slow part…')
        eng_wav = wav(a_eng, 'eng')
        ck()
        alt_wav = wav(a_alt, 'alt') if a_alt else None
        ck()

        # subtitles: text tracks from the container, then any user-supplied SRTs
        srt_files = []; seen = set(); skipped = 0
        for st in subs:
            if st.get('codec_name') in IMAGE_SUBS:
                skipped += 1; continue
            c = to3(st.get('tags', {}).get('language', 'und'))
            if c in seen:
                continue
            p = os.path.join(work, f's.{c}.srt')
            rc, _, _ = _run([FFMPEG, '-y', '-v', 'error', '-i', video,
                             '-map', f"0:{st['index']}", p], cancel)
            if rc == 0 and os.path.exists(p) and os.path.getsize(p) > 0:
                srt_files.append((c, p, True)); seen.add(c)       # True = came from the source
        if skipped:
            log(f"NOTE: {skipped} image-based (PGS/VobSub) subtitle track(s) skipped — supply text .srt instead.")
        for p in extra_srts:
            m = re.search(r'\.([a-z0-9-]{2,6})\.srt$', os.path.basename(p), re.I)
            c = to3(m.group(1)) if m else 'SUB'
            if c not in seen:
                srt_files.append((c, p, False)); seen.add(c)      # False = supplied by hand
        # ENG is the default track (first wins), JPN next, everything else alphabetical
        _rank = {'ENG': 0, 'JPN': 1}
        srt_files.sort(key=lambda x: (_rank.get(x[0], 9), x[0]))
        # Subtitles pulled OUT of the source share its timeline, so they move with the audio.
        # Files supplied by hand were timed to whatever release they were made for -- often the
        # same master the encode came from -- so they get their own knob and default to
        # untouched. (Downloaded subs for a film whose encode has a longer head are a real case:
        # the audio needed +14.4 s while the subtitles were already correct.)
        moved = []
        for c, p, from_source in srt_files:
            sh = offset if from_source else srt_offset
            moved.append((c, shift_srt(p, os.path.join(work, f'sh.{c}.srt'), sh) if sh else p))
        srt_files = moved
        if offset or srt_offset:
            log(f'Offsets applied: audio {offset:+.3f} s, extracted subtitles {offset:+.3f} s, '
                f'supplied subtitles {srt_offset:+.3f} s.')
        log(f"Subtitles: {', '.join(c for c, _ in srt_files) or 'none'}")

        # metadata: defaults <- TMDB (if a token is set) <- whatever the caller filled in by hand
        info = {'title': title, 'year': year, 'genres': [], 'runtime': 0, 'date': '',
                'desc': '', 'showdesc': '', 'poster': poster}
        if tmdb_token:
            info.update({k: v for k, v in fetch_meta(tmdb_token, title, year, se, log).items() if v})
            if info.get('poster') and not poster:
                pj = os.path.join(work, 'poster.jpg')
                urllib.request.urlretrieve(f"https://image.tmdb.org/t/p/w500{info['poster']}", pj)
                info['poster'] = pj
        if meta:                       # hand-entered values always win over anything fetched
            info.update({k: v for k, v in meta.items() if v not in (None, '', [], 0)})
            log('Using the metadata you supplied'
                + (' (over TMDB)' if tmdb_token else '') + '.')
        if info.get('poster') and not os.path.exists(str(info['poster'])):
            log(f"Poster {info['poster']} not found — building without artwork.")
            info['poster'] = None

        gen = info.get('genres') or []
        if isinstance(gen, str):                      # accept "Fantasy, Animation" as typed
            gen = [g.strip() for g in gen.split(',') if g.strip()]
        nfo = os.path.join(work, 'info.nfo')
        with open(nfo, 'w') as f:
            f.write(f"title={info['title']}\nyear={info['year']}\n"
                    f"category={info.get('category') or ('TV Shows' if se else 'Movies')}\n"
                    f"genres={', '.join(gen)}\nruntime={info.get('runtime', 0)}\n"
                    f"date={info.get('date','')}\nis3d={1 if is3d else 0}\n"
                    f"format={fmt if is3d else ''}\ndesc={info.get('desc','')}\n")
            if info.get('showdesc'):
                f.write(f"showdesc={info['showdesc']}\n")

        # output name
        if not out_path:
            tag3d = ' (3D)' if is3d else ''
            if se:
                et = info.get('eptitle', '')
                prefix = f"{info['title']} ({info['year']}){tag3d} (S) - S{se[0]:02d}e{se[1]:02d}"
                if et:
                    room = 130 - len(prefix) - 10
                    if len(et) > room > 0:
                        et = et[:room].rsplit(' ', 1)[0].rstrip(' -!?,') + '…'
                base = prefix + (f" - {et}" if et else '') + '.moflex'
            else:
                base = f"{info['title']} ({info['year']}){tag3d} (S).moflex"
            for a, b in (('/', ' - '), (':', ' -'), ('*', ''), ('?', ''), ('"', "'"),
                         ('<', ''), ('>', ''), ('|', '')):
                base = base.replace(a, b)
            base = re.sub(r'\s+', ' ', base).strip()
            out_path = os.path.join(out_dir or os.path.dirname(os.path.abspath(moflex)), base)

        # assemble via the core builder
        args = [moflex, out_path, '--strip-audio', '--audio', eng_wav, '--lang', 'ENG', '--nfo', nfo]
        if boost:
            args.append('--normalize')
        if alt_wav:
            args += ['--trailer-audio', alt_wav, '--trailer-lang', alt_lang]
        for c, p in srt_files:
            args += ['--trailer-srt', p]
        if info.get('poster') and os.path.exists(info['poster']):
            args += ['--art', info['poster']]
        ck()
        log('Assembling SUPER MOFLEX (encoding audio + rewriting the container)…')
        with contextlib.redirect_stdout(_LogStream(log)):
            core.main_args(args)                   # in-process call (no subprocess)

        # verify the trailer wrote cleanly (an SMB/NAS write once zeroed a trailer tail, so the
        # output is always walked back after writing rather than trusted)
        secs = verify(out_path)
        log('Verified trailer: ' + ', '.join(f'{k}x{v}' if v > 1 else k for k, v in secs.items()))
        # drop the full-size poster next to the output (catalog artwork convention)
        if info.get('poster') and os.path.exists(info['poster']):
            shutil.copyfile(info['poster'], re.sub(r'\.moflex$', '.jpg', out_path, flags=re.I))
        log(f"DONE → {out_path}")
        return out_path
    finally:
        shutil.rmtree(work, ignore_errors=True)

def verify(path):
    """Walk the CSXTRA trailer of a finished file. Returns {section: count}; raises on damage."""
    known = {b'NFO0', b'ART5', b'LNG0', b'SUB0', b'SUB1', b'AUD0', b'AUD1'}
    secs = {}
    with open(path, 'rb') as f:
        f.seek(0, 2); fsz = f.tell()
        if fsz < 32:
            raise RuntimeError('verify: output is too small to be a moflex')
        f.seek(-16, 2); tail = f.read(16)
        if tail[8:] != b'CSXTRA01':
            raise RuntimeError('verify: footer missing after write')
        poff = struct.unpack('<Q', tail[:8])[0]
        if poff >= fsz - 16:
            raise RuntimeError('verify: payload offset points past the file')
        p = poff
        while p + 8 <= fsz - 16:
            f.seek(p); h = f.read(8); cc = h[:4]; ln = struct.unpack('<I', h[4:])[0]
            if cc not in known or ln == 0 or p + 8 + ln > fsz - 16:
                raise RuntimeError(f'verify: corrupt trailer section at byte {p}')
            secs[cc.decode()] = secs.get(cc.decode(), 0) + 1
            p += 8 + ln
        if p != fsz - 16:
            raise RuntimeError('verify: trailer walk did not reach the footer')
    return secs

# ---- command line (the GUI is smoflex_gui.py; this is the same pipeline headless) ------------

def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog='smoflex_build',
                                 description='Build a SUPER MOFLEX from a source video + an encoded .moflex')
    ap.add_argument('video', help='source MKV/MP4 (dual audio, ideally text subtitles)')
    ap.add_argument('moflex', help='the mobiclip-encoded .moflex of the same title')
    ap.add_argument('-o', '--out', help='output file path')
    ap.add_argument('-d', '--outdir', help='output folder (name is built from the metadata)')
    ap.add_argument('--srt', action='append', default=[], metavar='FILE',
                    help='extra subtitle file, repeatable; language read from "name.eng.srt"')
    ap.add_argument('--no-normalize', action='store_true', help='keep the source levels')
    ap.add_argument('--2d', dest='two_d', action='store_true', help='force 2D (default: from the name)')
    ap.add_argument('--3d', dest='three_d', action='store_true', help='force 3D')
    ap.add_argument('--format', choices=['native', 'converted'], default='converted',
                    help='3D origin recorded in the metadata')
    ap.add_argument('--title'), ap.add_argument('--year', type=int)
    ap.add_argument('--offset', type=float, default=0.0, metavar='SECS',
                    help='slide the source audio (and the source\'s own subtitle tracks) onto '
                         'the encode\'s timeline: positive if the encode starts earlier (extra '
                         'logo/leader the source lacks), negative if it starts later. Only '
                         'needed when the encode did not come from this exact file.')
    ap.add_argument('--srt-offset', type=float, default=0.0, metavar='SECS',
                    help='same, for --srt files only; they are usually already timed to the '
                         'encode\'s master, so this defaults to 0')
    ap.add_argument('--poster', help='poster image to embed (default: TMDB, if a token is set)')
    ap.add_argument('--genres', help='comma separated, e.g. "Fantasy, Animation"')
    ap.add_argument('--runtime', type=int, help='minutes')
    ap.add_argument('--category', help='library category (default: Movies, or TV Shows for SxxEyy)')
    ap.add_argument('--date', help='release / air date, YYYY-MM-DD')
    ap.add_argument('--eptitle', help='episode title (used in the output filename)')
    ap.add_argument('--desc', help='description text')
    ap.add_argument('--tmdb', default=os.environ.get('TMDB_TOKEN'),
                    help='TMDB read token for metadata (or set TMDB_TOKEN)')
    ap.add_argument('--ffmpeg', help='path to ffmpeg if it is not on PATH')
    a = ap.parse_args(argv)
    if a.ffmpeg:
        set_tools(a.ffmpeg)
    is3d = True if a.three_d else (False if a.two_d else None)
    try:
        meta = {'title': a.title, 'year': a.year, 'genres': a.genres, 'runtime': a.runtime,
                'category': a.category, 'date': a.date, 'eptitle': a.eptitle, 'desc': a.desc}
        build(a.video, a.moflex, out_path=a.out, out_dir=a.outdir, extra_srts=a.srt,
              boost=not a.no_normalize, is3d=is3d, fmt=a.format, tmdb_token=a.tmdb,
              title=a.title, year=a.year, poster=a.poster, offset=a.offset,
              srt_offset=a.srt_offset, meta=meta)
    except (RuntimeError, Cancelled) as ex:
        print(f'error: {ex}', file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(_cli(sys.argv[1:]))

#!/usr/bin/env python3
"""SUPER MOFLEX Builder — a small desktop front end for smoflex_build.

Runs anywhere Python does (macOS, Windows, Linux): Tkinter ships with Python, and the only
external requirement is ffmpeg. Pick the source video and the encoded .moflex, press Build.

    python3 smoflex_gui.py
"""
import json
import os
import queue
import subprocess
import sys
import threading
import traceback
import webbrowser

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import smoflex_build as sb

APP = 'SUPER MOFLEX Builder'
CONFIG = os.path.join(os.path.expanduser('~'), '.smoflex_gui.json')
VIDEO_TYPES = [('Video files', '*.mkv *.mp4 *.m4v *.mov *.avi *.ts'), ('All files', '*.*')]
MOFLEX_TYPES = [('Moflex video', '*.moflex'), ('All files', '*.*')]
SRT_TYPES = [('Subtitles', '*.srt *.ass *.ssa *.vtt'), ('All files', '*.*')]


def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG, 'w') as f:
            json.dump(cfg, f, indent=1)
    except Exception:
        pass                      # a read-only home directory must not break the app


class App(ttk.Frame):
    def __init__(self, root):
        super().__init__(root, padding=10)
        self.root = root
        self.cfg = load_config()
        self.q = queue.Queue()            # worker -> UI log lines
        self.cancel = None                # threading.Event while a build runs
        self.worker = None
        self.srts = []                    # extra subtitle files
        self.audio_tracks = []            # [(stream index, label)] from the source probe

        self.grid(sticky='nsew')
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        if self.cfg.get('ffmpeg'):
            sb.set_tools(self.cfg['ffmpeg'])
        else:
            found, _ = sb.find_tools()
            if found:
                sb.set_tools(found)

        self._build_ui()
        self._poll_log()
        if not sb.tools_ready():
            self.log('ffmpeg was not found. Install it, or use Tools ▸ Locate ffmpeg… to point '
                     'this app at the binary.')

    # ---- layout ---------------------------------------------------------------------------

    def _build_ui(self):
        r = 0
        self.v_video = tk.StringVar()
        self.v_moflex = tk.StringVar()
        self.v_outdir = tk.StringVar(value=self.cfg.get('outdir', ''))
        self.v_title = tk.StringVar()
        self.v_year = tk.StringVar()
        self.v_token = tk.StringVar(value=self.cfg.get('tmdb', ''))
        self.v_norm = tk.BooleanVar(value=True)
        self.v_offset = tk.StringVar(value='0')
        self.v_3d = tk.StringVar(value='Auto')
        self.v_fmt = tk.StringVar(value='converted')
        self.v_status = tk.StringVar(value='Ready.')
        self.v_ain = tk.StringVar()
        self.v_aalt = tk.StringVar()

        r = self._file_row(r, 'Source video', self.v_video, self._pick_video,
                           'The original MKV/MP4 — its audio tracks and subtitles are the source')
        r = self._file_row(r, 'Encoded .moflex', self.v_moflex, self._pick_moflex,
                           'Your mobiclip encode of the same title (video comes from here)')
        r = self._file_row(r, 'Output folder', self.v_outdir, self._pick_outdir,
                           'Where the finished file lands; the name is built from the metadata')

        # audio track choice
        af = ttk.LabelFrame(self, text='Audio', padding=6)
        af.grid(row=r, column=0, columnspan=3, sticky='ew', pady=(8, 2))
        af.columnconfigure(1, weight=1)
        af.columnconfigure(3, weight=1)
        ttk.Label(af, text='In-band (all players):').grid(row=0, column=0, sticky='w')
        self.cb_ain = ttk.Combobox(af, textvariable=self.v_ain, state='readonly', width=28)
        self.cb_ain.grid(row=0, column=1, sticky='ew', padx=4)
        ttk.Label(af, text='Second track:').grid(row=0, column=2, sticky='w', padx=(10, 0))
        self.cb_aalt = ttk.Combobox(af, textvariable=self.v_aalt, state='readonly', width=28)
        self.cb_aalt.grid(row=0, column=3, sticky='ew', padx=4)
        ttk.Checkbutton(af, text='Normalize both to −1 dBFS (volume only)',
                        variable=self.v_norm).grid(row=1, column=0, columnspan=4, sticky='w',
                                                   pady=(6, 0))
        ttk.Label(af, text='Sync offset:').grid(row=2, column=0, sticky='w', pady=(6, 0))
        off = ttk.Frame(af)
        off.grid(row=2, column=1, columnspan=3, sticky='w', pady=(6, 0))
        ttk.Entry(off, textvariable=self.v_offset, width=8).pack(side='left', padx=(4, 4))
        ttk.Label(off, text='seconds — only when the encode came from a different master than '
                            'this source. Positive if the encode starts earlier (a leader the '
                            'source lacks). Moves the audio and the source\'s own subtitles; '
                            'added .srt files are left alone.', foreground='#666', wraplength=430,
                  justify='left').pack(side='left')
        r += 1

        # subtitles
        sf = ttk.LabelFrame(self, text='Extra subtitles (text .srt — image subs cannot be used)',
                            padding=6)
        sf.grid(row=r, column=0, columnspan=3, sticky='ew', pady=2)
        sf.columnconfigure(0, weight=1)
        self.lst = tk.Listbox(sf, height=4, activestyle='none')
        self.lst.grid(row=0, column=0, rowspan=2, sticky='ew')
        ttk.Button(sf, text='Add…', command=self._add_srt).grid(row=0, column=1, sticky='ew', padx=4)
        ttk.Button(sf, text='Remove', command=self._del_srt).grid(row=1, column=1, sticky='ew', padx=4)
        ttk.Label(sf, text='Name them "Movie.eng.srt", "Movie.jpn.srt" … so each track is labelled.',
                  foreground='#666').grid(row=2, column=0, columnspan=2, sticky='w', pady=(4, 0))
        r += 1

        # metadata / options
        of = ttk.LabelFrame(self, text='Metadata', padding=6)
        of.grid(row=r, column=0, columnspan=3, sticky='ew', pady=2)
        of.columnconfigure(1, weight=1)
        ttk.Label(of, text='Title override:').grid(row=0, column=0, sticky='w')
        ttk.Entry(of, textvariable=self.v_title).grid(row=0, column=1, sticky='ew', padx=4)
        ttk.Label(of, text='Year:').grid(row=0, column=2, sticky='w', padx=(8, 0))
        ttk.Entry(of, textvariable=self.v_year, width=6).grid(row=0, column=3, sticky='w', padx=4)
        ttk.Label(of, text='3D:').grid(row=1, column=0, sticky='w', pady=(6, 0))
        ttk.Combobox(of, textvariable=self.v_3d, values=['Auto', '2D', '3D'], state='readonly',
                     width=6).grid(row=1, column=1, sticky='w', padx=4, pady=(6, 0))
        ttk.Label(of, text='3D origin:').grid(row=1, column=2, sticky='w', padx=(8, 0), pady=(6, 0))
        ttk.Combobox(of, textvariable=self.v_fmt, values=['converted', 'native'], state='readonly',
                     width=10).grid(row=1, column=3, sticky='w', padx=4, pady=(6, 0))
        ttk.Label(of, text='TMDB token:').grid(row=2, column=0, sticky='w', pady=(6, 0))
        ttk.Entry(of, textvariable=self.v_token, show='•').grid(row=2, column=1, columnspan=2,
                                                                sticky='ew', padx=4, pady=(6, 0))
        ttk.Button(of, text='Get one', width=8,
                   command=lambda: webbrowser.open('https://www.themoviedb.org/settings/api')
                   ).grid(row=2, column=3, sticky='w', padx=4, pady=(6, 0))
        ttk.Label(of, text='Optional: fills in title, year, genres, description and poster so the '
                           'player needs no lookup.', foreground='#666'
                  ).grid(row=3, column=0, columnspan=4, sticky='w', pady=(4, 0))
        r += 1

        # actions
        bar = ttk.Frame(self)
        bar.grid(row=r, column=0, columnspan=3, sticky='ew', pady=(8, 4))
        bar.columnconfigure(2, weight=1)
        self.btn_build = ttk.Button(bar, text='Build SUPER MOFLEX', command=self._start)
        self.btn_build.grid(row=0, column=0)
        self.btn_cancel = ttk.Button(bar, text='Cancel', command=self._cancel, state='disabled')
        self.btn_cancel.grid(row=0, column=1, padx=6)
        ttk.Label(bar, textvariable=self.v_status).grid(row=0, column=2, sticky='w', padx=8)
        self.bar = ttk.Progressbar(bar, mode='indeterminate', length=120)
        self.bar.grid(row=0, column=3, sticky='e')
        r += 1

        # log
        lf = ttk.Frame(self)
        lf.grid(row=r, column=0, columnspan=3, sticky='nsew')
        self.rowconfigure(r, weight=1)
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.txt = tk.Text(lf, height=12, wrap='word', state='disabled',
                           font=('Menlo' if sys.platform == 'darwin' else 'Consolas', 10))
        self.txt.grid(row=0, column=0, sticky='nsew')
        sc = ttk.Scrollbar(lf, command=self.txt.yview)
        sc.grid(row=0, column=1, sticky='ns')
        self.txt['yscrollcommand'] = sc.set

        # menu
        m = tk.Menu(self.root)
        tools = tk.Menu(m, tearoff=0)
        tools.add_command(label='Locate ffmpeg…', command=self._pick_ffmpeg)
        tools.add_command(label='Check a built file…', command=self._verify_file)
        tools.add_separator()
        tools.add_command(label='About', command=self._about)
        m.add_cascade(label='Tools', menu=tools)
        self.root.config(menu=m)

    def _file_row(self, r, label, var, cmd, hint):
        ttk.Label(self, text=label + ':').grid(row=r, column=0, sticky='w', pady=2)
        ttk.Entry(self, textvariable=var).grid(row=r, column=1, sticky='ew', padx=4, pady=2)
        ttk.Button(self, text='Browse…', command=cmd).grid(row=r, column=2, pady=2)
        ttk.Label(self, text=hint, foreground='#666').grid(row=r + 1, column=1, columnspan=2,
                                                           sticky='w')
        return r + 2

    # ---- pickers --------------------------------------------------------------------------

    def _pick_video(self):
        p = filedialog.askopenfilename(title='Source video', filetypes=VIDEO_TYPES,
                                       initialdir=self.cfg.get('videodir', ''))
        if not p:
            return
        self.v_video.set(p)
        self.cfg['videodir'] = os.path.dirname(p)
        save_config(self.cfg)
        self._probe(p)

    def _pick_moflex(self):
        p = filedialog.askopenfilename(title='Encoded .moflex', filetypes=MOFLEX_TYPES,
                                       initialdir=self.cfg.get('moflexdir', ''))
        if p:
            self.v_moflex.set(p)
            self.cfg['moflexdir'] = os.path.dirname(p)
            if not self.v_outdir.get():
                self.v_outdir.set(os.path.join(os.path.dirname(p), 'super'))
            save_config(self.cfg)

    def _pick_outdir(self):
        p = filedialog.askdirectory(title='Output folder', initialdir=self.v_outdir.get() or '')
        if p:
            self.v_outdir.set(p)
            self.cfg['outdir'] = p
            save_config(self.cfg)

    def _pick_ffmpeg(self):
        p = filedialog.askopenfilename(title='Locate the ffmpeg binary')
        if p:
            sb.set_tools(p)
            self.cfg['ffmpeg'] = p
            save_config(self.cfg)
            self.log(f'ffmpeg: {p}' + ('' if sb.tools_ready() else '  (ffprobe still missing!)'))

    def _add_srt(self):
        for p in filedialog.askopenfilenames(title='Subtitle files', filetypes=SRT_TYPES,
                                             initialdir=self.cfg.get('srtdir', '')):
            if p not in self.srts:
                self.srts.append(p)
                self.lst.insert('end', os.path.basename(p))
                self.cfg['srtdir'] = os.path.dirname(p)
        save_config(self.cfg)

    def _del_srt(self):
        for i in reversed(self.lst.curselection()):
            self.lst.delete(i)
            del self.srts[i]

    # ---- source probe ---------------------------------------------------------------------

    def _probe(self, path):
        if not sb.tools_ready():
            self.log('Cannot read the source until ffmpeg is available.')
            return
        try:
            info = sb.probe_source(path)
        except Exception as ex:
            self.log(f'Could not read {os.path.basename(path)}: {ex}')
            return
        self.audio_tracks = info['audio']
        labels = [f'{i}: {d}' for i, d in info['audio']]
        self.cb_ain['values'] = labels
        self.cb_aalt['values'] = ['(none)'] + labels
        # default: English in-band, the first other track in the trailer
        eng = next((k for k, (i, d) in enumerate(info['audio']) if d.startswith('ENG')), 0)
        self.v_ain.set(labels[eng] if labels else '')
        alt = next((k for k in range(len(labels)) if k != eng), None)
        self.v_aalt.set(labels[alt] if alt is not None else '(none)')
        t, y, se = sb.parse_name(path)
        self.log(f'{os.path.basename(path)}: {len(info["audio"])} audio track(s), '
                 f'{len(info["subs"])} text subtitle track(s)'
                 + (f', {info["imagesubs"]} image subtitle track(s) that cannot be used'
                    if info['imagesubs'] else ''))
        self.log(f'Parsed as: {t!r} ({y or "year?"})'
                 + (f' S{se[0]:02d}E{se[1]:02d}' if se else ' — movie'))
        if info['imagesubs'] and not info['subs']:
            self.log('This disc rip carries picture subtitles only. Add text .srt files above if '
                     'you want subtitles in the finished file.')

    def _sel_index(self, var):
        """Stream index for a combobox row. None = 'decide automatically' (nothing chosen yet),
        -1 = the explicit '(none)' row; never conflate the two or a silent path would drop the
        second audio track."""
        s = var.get()
        if not s:
            return None
        if s == '(none)':
            return -1
        try:
            return int(s.split(':', 1)[0])
        except ValueError:
            return None

    # ---- build ----------------------------------------------------------------------------

    def _start(self):
        video, moflex = self.v_video.get().strip(), self.v_moflex.get().strip()
        if not (video and os.path.isfile(video)):
            return messagebox.showerror(APP, 'Choose the source video first.')
        if not (moflex and os.path.isfile(moflex)):
            return messagebox.showerror(APP, 'Choose the encoded .moflex first.')
        if not sb.tools_ready():
            return messagebox.showerror(APP, 'ffmpeg / ffprobe not found.\n\nInstall ffmpeg, or use '
                                             'Tools ▸ Locate ffmpeg… to point at the binary.')
        outdir = self.v_outdir.get().strip() or os.path.join(os.path.dirname(moflex), 'super')
        os.makedirs(outdir, exist_ok=True)
        self.cfg['outdir'] = outdir
        self.cfg['tmdb'] = self.v_token.get().strip()
        save_config(self.cfg)

        year = self.v_year.get().strip()
        try:
            offset = float(self.v_offset.get().strip() or 0)
        except ValueError:
            return messagebox.showerror(APP, 'Sync offset must be a number of seconds, e.g. 14.45')
        opts = dict(out_dir=outdir, extra_srts=list(self.srts), boost=self.v_norm.get(),
                    offset=offset,
                    is3d={'Auto': None, '2D': False, '3D': True}[self.v_3d.get()],
                    fmt=self.v_fmt.get(), tmdb_token=self.v_token.get().strip() or None,
                    title=self.v_title.get().strip() or None,
                    year=int(year) if year.isdigit() else None,
                    audio_in=self._sel_index(self.v_ain),
                    audio_alt=self._sel_index(self.v_aalt))

        self.cancel = threading.Event()
        self.btn_build['state'] = 'disabled'
        self.btn_cancel['state'] = 'normal'
        self.bar.start(12)
        self.v_status.set('Building…')
        self.log('─' * 60)

        def run():
            try:
                out = sb.build(video, moflex, log=self.q.put, cancel=self.cancel, **opts)
                self.q.put(('done', out))
            except sb.Cancelled:
                self.q.put(('cancelled', None))
            except Exception as ex:
                self.q.put(str(ex) if isinstance(ex, RuntimeError) else traceback.format_exc())
                self.q.put(('failed', str(ex)))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _cancel(self):
        if self.cancel:
            self.cancel.set()
            self.v_status.set('Cancelling…')

    def _finish(self, state, payload):
        self.bar.stop()
        self.btn_build['state'] = 'normal'
        self.btn_cancel['state'] = 'disabled'
        self.cancel = None
        if state == 'done':
            self.v_status.set('Done.')
            self.log(f'Finished: {payload}')
            if messagebox.askyesno(APP, f'Built:\n\n{os.path.basename(payload)}\n\nShow the file?'):
                self._reveal(payload)
        elif state == 'cancelled':
            self.v_status.set('Cancelled.')
            self.log('Cancelled — no output was written.')
        else:
            self.v_status.set('Failed.')
            messagebox.showerror(APP, f'Build failed:\n\n{payload}')

    @staticmethod
    def _reveal(path):
        try:
            if sys.platform == 'darwin':
                subprocess.run(['open', '-R', path])
            elif sys.platform == 'win32':
                subprocess.run(['explorer', '/select,', os.path.normpath(path)])
            else:
                subprocess.run(['xdg-open', os.path.dirname(path)])
        except Exception:
            pass

    # ---- misc -----------------------------------------------------------------------------

    def _verify_file(self):
        p = filedialog.askopenfilename(title='Check a SUPER MOFLEX', filetypes=MOFLEX_TYPES)
        if not p:
            return
        try:
            secs = sb.verify(p)
            names = {'NFO0': 'library info', 'ART5': 'poster', 'LNG0': 'in-band language',
                     'SUB0': 'subtitle', 'SUB1': 'subtitle', 'AUD0': 'audio track',
                     'AUD1': 'audio track'}
            body = '\n'.join(f'  {names.get(k, k)}  ×{v}' for k, v in secs.items())
            messagebox.showinfo(APP, f'{os.path.basename(p)} is a valid SUPER MOFLEX:\n\n{body}')
        except Exception as ex:
            messagebox.showwarning(APP, f'{os.path.basename(p)}\n\n{ex}\n\n'
                                        'A plain moflex (no trailer) reports this too.')

    def _about(self):
        messagebox.showinfo(APP,
                            f'{APP}\n\nBuilds a self-contained SUPER MOFLEX: one audio track in '
                            'band so stock players keep working, plus a second language, every '
                            'subtitle, the library info and the poster in a trailer that only '
                            'players which know about it read.\n\nNeeds ffmpeg. Nothing else.')

    def log(self, line):
        self.txt['state'] = 'normal'
        self.txt.insert('end', line.rstrip() + '\n')
        self.txt.see('end')
        self.txt['state'] = 'disabled'

    def _poll_log(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple):
                    self._finish(*item)
                else:
                    self.log(item)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log)


def main():
    root = tk.Tk()
    root.title(APP)
    root.minsize(720, 620)
    try:                                   # crisper widgets than the 1990s default theme
        ttk.Style().theme_use('aqua' if sys.platform == 'darwin' else 'vista')
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()

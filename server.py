"""
YT Downloader + Projects — Local Flask Server
"""

import argparse
import os
import re as _re
import shutil
import sys
import threading
import traceback as _tb
import uuid
from datetime import datetime

# Force UTF-8 on Windows
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yt_dlp

# ---- Args ------------------------------------------------------------------

parser = argparse.ArgumentParser()
parser.add_argument('--library',  default=None)
parser.add_argument('--projects', default=None)
args = parser.parse_args()

# ---- Library dirs ----------------------------------------------------------

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
LIBRARY_DIR = args.library or os.path.join(BASE_DIR, 'library')
FMT_DIRS    = {fmt: os.path.join(LIBRARY_DIR, fmt) for fmt in ('mp3', 'mp4', 'wav')}
for d in FMT_DIRS.values():
    os.makedirs(d, exist_ok=True)

# ---- Projects dir ----------------------------------------------------------

PROJECTS_DIR = args.projects or None
if PROJECTS_DIR:
    os.makedirs(PROJECTS_DIR, exist_ok=True)

# ---- Flask -----------------------------------------------------------------

app = Flask(__name__)
CORS(app)
jobs: dict = {}

# ---- Helpers ---------------------------------------------------------------

def human_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024: return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} GB'

def sanitize(name):
    name = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = name.strip('. ')
    return name[:180] or 'download'

# ---- Audio analysis --------------------------------------------------------

try:
    import numpy as _np
    _HAS_NP = True
except ImportError:
    _HAS_NP = False

import subprocess as _sp, json as _js

def _ffmpeg():  return shutil.which('ffmpeg')  or 'ffmpeg'
def _ffprobe(): return shutil.which('ffprobe') or 'ffprobe'

def _get_duration(path):
    try:
        r = _sp.run([_ffprobe(), '-v', 'quiet', '-print_format', 'json',
                     '-show_streams', path],
                    capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            for s in _js.loads(r.stdout).get('streams', []):
                if s.get('codec_type') == 'audio':
                    return float(s.get('duration') or 0)
    except Exception as e:
        print(f'[analysis] ffprobe: {e}')
    return None

def _load_pcm(path, sr=22050, max_sec=60):
    if not _HAS_NP: return None, sr
    try:
        r = _sp.run([_ffmpeg(), '-i', path, '-t', str(max_sec),
                     '-ar', str(sr), '-ac', '1', '-f', 'f32le',
                     '-vn', '-loglevel', 'quiet', '-'],
                    capture_output=True, timeout=35)
        if r.returncode != 0 or len(r.stdout) < 2000:
            return None, sr
        return _np.frombuffer(r.stdout, dtype=_np.float32).copy(), sr
    except Exception as e:
        print(f'[analysis] decode: {e}')
        return None, sr

def _bpm(y, sr, hop=512):
    fl = 1024; n = (len(y) - fl) // hop
    if n < 20: return None
    energy = _np.array([_np.sum(y[i*hop:i*hop+fl]**2) for i in range(n)])
    onset  = _np.maximum(0, _np.diff(energy))
    ac     = _np.correlate(onset, onset, 'full')[len(onset)-1:]
    lo = max(1, int(sr * 60 / hop / 200))
    hi = min(len(ac)-1, int(sr * 60 / hop / 60))
    if lo >= hi: return None
    peak = int(_np.argmax(ac[lo:hi+1])) + lo
    return round(60.0 * sr / hop / peak, 1)

def _key(y, sr):
    n_fft, hop = 4096, 2048
    frames = [y[i:i+n_fft] for i in range(0, len(y)-n_fft, hop)]
    if not frames: return None
    chroma = _np.zeros(12)
    for frame in frames[:80]:
        win  = _np.hanning(len(frame))
        spec = _np.abs(_np.fft.rfft(frame * win))
        fq   = _np.fft.rfftfreq(len(frame), 1.0/sr)
        for j in range(1, len(fq)):
            f = fq[j]
            if not (65 < f < 4000): continue
            midi = 12 * _np.log2(f / 440.0) + 69
            chroma[int(round(float(midi))) % 12] += spec[j]
    if chroma.max() == 0: return None
    chroma /= chroma.max()
    notes = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']
    major = [6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88]
    minor = [6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17]
    best, best_r = None, -2.0
    for i in range(12):
        for prof, mode in [(major,'major'), (minor,'minor')]:
            r = float(_np.corrcoef(chroma, _np.roll(prof, i))[0, 1])
            if _np.isnan(r): r = 0.0
            if r > best_r: best_r, best = r, f'{notes[i]} {mode}'
    return best

def analyze_audio(filepath):
    """Only extract duration — BPM and key are entered manually by the user."""
    return {'duration': _get_duration(filepath), 'bpm': None, 'key': None}

# ---- Project helpers -------------------------------------------------------

_proj_locks = {}
_proj_lock_meta = threading.Lock()

def _proj_lock(pid):
    with _proj_lock_meta:
        if pid not in _proj_locks: _proj_locks[pid] = threading.Lock()
        return _proj_locks[pid]

def _slug(name):
    """Slugify a name — safe for filesystem, readable."""
    s = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)   # strip illegal chars
    s = _re.sub(r'[\s]+', ' ', s.strip())               # collapse whitespace
    s = s[:80] or 'project'
    return s

def _unique_dir(base_dir, desired_name):
    """Return a unique directory path under base_dir using desired_name."""
    path = os.path.join(base_dir, desired_name)
    if not os.path.exists(path):
        return path
    # Append _2, _3, etc. if collision
    i = 2
    while True:
        candidate = os.path.join(base_dir, f'{desired_name}_{i}')
        if not os.path.exists(candidate):
            return candidate
        i += 1

def _proj_dir(pid, name=None):
    """Return project directory path.
    If name given (new project), builds a readable folder name.
    Otherwise scans existing folders for matching project.json.
    """
    if name:
        safe = _slug(name) or 'project'
        return _unique_dir(PROJECTS_DIR, safe)
    # Scan for existing folder
    if PROJECTS_DIR and os.path.isdir(PROJECTS_DIR):
        for d in os.listdir(PROJECTS_DIR):
            fp = os.path.join(PROJECTS_DIR, d)
            if not os.path.isdir(fp): continue
            pjson = os.path.join(fp, 'project.json')
            if os.path.isfile(pjson):
                try:
                    with open(pjson, 'r', encoding='utf-8') as f:
                        data = _js.load(f)
                    if data.get('id') == pid:
                        return fp
                except Exception:
                    pass
    return os.path.join(PROJECTS_DIR, pid)

def _proj_json(pid, name=None):
    return os.path.join(_proj_dir(pid, name), 'project.json')

def _track_dir(pid, tid):
    return os.path.join(_proj_dir(pid), 'tracks', tid)

def load_project(pid):
    try:
        with open(_proj_json(pid), 'r', encoding='utf-8') as f:
            return _js.load(f)
    except Exception: return None

def save_project(pid, data):
    path = _proj_json(pid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        _js.dump(data, f, ensure_ascii=False, indent=2)

def _analyze_thread(pid, tid, filepath):
    """Get duration only. BPM and key are set manually by the user."""
    try:
        dur = _get_duration(filepath)
    except Exception as e:
        print(f'[analysis] duration error: {e}')
        dur = None
    with _proj_lock(pid):
        proj = load_project(pid)
        if proj:
            for t in proj.get('tracks', []):
                if t['id'] == tid:
                    t.update({'duration': dur, 'analyzing': False})
                    break
            save_project(pid, proj)

# ---- Progress hook / download worker ---------------------------------------

def make_hook(job_id):
    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            dl    = d.get('downloaded_bytes', 0)
            speed = d.get('speed') or 0
            jobs[job_id].update({'progress': int(dl/total*80) if total else 0,
                                  'speed': round(speed/1048576, 2) if speed else 0,
                                  'eta': d.get('eta') or 0})
        elif d['status'] == 'finished':
            jobs[job_id].update({'progress': 85, 'status': 'processing'})
    return hook

def download_worker(job_id, url, fmt, quality='320'):
    tmp = os.path.join(LIBRARY_DIR, '_tmp', job_id)
    os.makedirs(tmp, exist_ok=True)
    lib = FMT_DIRS.get(fmt) or FMT_DIRS.get('mp3')
    if fmt not in FMT_DIRS:
        lib = os.path.join(LIBRARY_DIR, fmt)
        os.makedirs(lib, exist_ok=True)

    # metadata post-processors — embed title/artist/album/cover where possible
    meta_pp = [{'key': 'FFmpegMetadata', 'add_metadata': True}]
    thumb_pp = [{'key': 'EmbedThumbnail', 'already_have_thumbnail': False}]

    base = {'outtmpl': os.path.join(tmp, '%(id)s.%(ext)s'),
            'progress_hooks': [make_hook(job_id)],
            'writethumbnail': True,
            'quiet': True, 'no_warnings': True, 'noplaylist': True}

    if fmt in ('mp3', 'aac', 'opus', 'm4a', 'flac'):
        codec = fmt
        pp = meta_pp + [{'key':'FFmpegExtractAudio','preferredcodec':codec,'preferredquality':str(quality)}]
        if fmt in ('mp3', 'm4a', 'aac', 'flac'):
            pp += thumb_pp
        opts = {**base, 'format': 'bestaudio/best', 'postprocessors': pp}
    elif fmt == 'wav':
        pp = meta_pp + [{'key':'FFmpegExtractAudio','preferredcodec':'wav'}]
        opts = {**base, 'format': 'bestaudio/best', 'postprocessors': pp}
    elif fmt in ('mp4', 'webm', 'mkv'):
        if quality == 'best' or quality is None:
            vfmt = 'bestvideo+bestaudio/best'
        else:
            vfmt = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best'
        pp   = meta_pp + thumb_pp
        opts = {**base, 'format': vfmt, 'merge_output_format': fmt, 'postprocessors': pp}
    else:
        jobs[job_id].update({'status': 'error', 'error': f'Unknown format: {fmt}'}); return

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info  = ydl.extract_info(url, download=True)
            title = info.get('title', 'download')
            vid   = info.get('id', 'download')
        files = [f for f in os.listdir(tmp)
                 if os.path.isfile(os.path.join(tmp, f)) and not f.endswith(('.jpg','.png','.webp'))]
        if not files: raise RuntimeError('No output file')
        src       = os.path.join(tmp, files[0])
        ext       = os.path.splitext(files[0])[1]
        safe_stem = sanitize(title) or sanitize(vid) or 'download'
        filename  = safe_stem + ext
        dest      = os.path.join(lib, filename)
        if os.path.exists(dest):
            filename = f'{safe_stem}_{uuid.uuid4().hex[:6]}{ext}'
            dest     = os.path.join(lib, filename)
        print(f'[worker] {src} -> {dest}')
        shutil.move(src, dest)
        jobs[job_id].update({'status':'done','progress':100,'file':dest,
                             'filename':filename,'format':fmt,'title':title})
    except Exception as e:
        print(f'[worker] EXCEPTION:\n{_tb.format_exc()}')
        jobs[job_id].update({'status':'error','error':str(e)})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ============================================================================
# ROUTES
# ============================================================================

@app.route('/info', methods=['POST'])
def video_info():
    """
    Smart info endpoint.
    - If the URL is a playlist/album/channel, returns is_playlist=True + entries list.
    - If single track, returns standard single-track metadata.
    """
    url = (request.json or {}).get('url','').strip()
    if not url: return jsonify({'error':'No URL'}), 400
    try:
        # First pass: flat extract to decide single vs playlist cheaply
        flat_opts = {
            'quiet': True, 'no_warnings': True,
            'extract_flat': 'in_playlist',
            'skip_download': True,
        }
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            flat = ydl.extract_info(url, download=False)

        entries_raw = flat.get('entries')

        if entries_raw is not None:
            # ── PLAYLIST / ALBUM ─────────────────────────────────
            # Do a second, richer pass without extract_flat so each
            # entry gets proper metadata (title, artist, thumbnail).
            # We limit depth to avoid downloading everything.
            rich_opts = {
                'quiet': True, 'no_warnings': True,
                'skip_download': True,
                'extract_flat': False,
                'playlistend': 200,      # cap at 200 entries for safety
            }
            try:
                with yt_dlp.YoutubeDL(rich_opts) as ydl:
                    rich = ydl.extract_info(url, download=False)
                entries_raw = rich.get('entries') or entries_raw
                flat        = rich
            except Exception:
                pass  # fall back to flat data

            entries = []
            for e in (entries_raw or []):
                if not e: continue
                dur = e.get('duration') or 0
                m, s = divmod(int(dur), 60)
                # Best thumbnail: prefer high-res
                thumb = ''
                if e.get('thumbnails'):
                    # pick largest
                    ts = sorted(e['thumbnails'], key=lambda t: (t.get('width') or 0), reverse=True)
                    thumb = ts[0].get('url','')
                elif e.get('thumbnail'):
                    thumb = e['thumbnail']
                entries.append({
                    'id':           e.get('id',''),
                    'title':        e.get('title') or e.get('track') or 'untitled',
                    'uploader':     e.get('uploader') or e.get('artist') or e.get('creator') or '',
                    'artist':       e.get('artist') or '',
                    'thumbnail':    thumb,
                    'duration_str': f'{m}:{s:02d}' if dur else '',
                })
            return jsonify({
                'is_playlist': True,
                'title':       flat.get('title') or flat.get('playlist_title') or 'Playlist',
                'entry_count': len(entries),
                'entries':     entries,
            })

        # ── SINGLE TRACK ─────────────────────────────────────────
        dur = flat.get('duration') or 0
        m, s = divmod(int(dur), 60)
        return jsonify({
            'is_playlist': False,
            'title':       flat.get('title'),
            'thumbnail':   flat.get('thumbnail'),
            'duration':    f'{m}:{s:02d}' if dur else None,
            'uploader':    flat.get('uploader') or flat.get('artist') or flat.get('creator'),
            'artist':      flat.get('artist'),
            'album':       flat.get('album'),
            'track':       flat.get('track'),
            'view_count':  f"{flat.get('view_count',0):,}" if flat.get('view_count') else None,
        })
    except Exception as e:
        print(f'[info] {_tb.format_exc()}')
        return jsonify({'error': str(e)}), 400

ALL_FMTS = {'mp3','mp4','wav','flac','aac','opus','m4a','webm','mkv'}

@app.route('/start', methods=['POST'])
def start_download():
    data = request.json or {}
    url  = data.get('url','').strip(); fmt = data.get('format','mp3').lower()
    qual = str(data.get('quality','320'))
    if not url:         return jsonify({'error':'No URL'}), 400
    if fmt not in ALL_FMTS: return jsonify({'error':f'Unknown format: {fmt}'}), 400
    jid = str(uuid.uuid4())
    jobs[jid] = {'status':'downloading','progress':0,'speed':0,'eta':0,
                 'file':None,'filename':None,'format':fmt,'title':None,'error':None}
    threading.Thread(target=download_worker, args=(jid,url,fmt,qual), daemon=True).start()
    return jsonify({'job_id':jid})

@app.route('/status/<jid>')
def job_status(jid):
    j = jobs.get(jid)
    if not j: return jsonify({'error':'Not found'}), 404
    return jsonify({k:j[k] for k in ('status','progress','speed','eta','title','filename','format','error')})

@app.route('/download/<jid>')
def serve_job_file(jid):
    j = jobs.get(jid)
    if not j or j['status'] != 'done': return jsonify({'error':'Not ready'}), 400
    return send_file(j['file'], as_attachment=True, download_name=j['filename'])

@app.route('/library')
def list_library():
    entries = []
    # scan all first-level subdirs of library (mp3, mp4, wav, flac, etc.)
    if not os.path.isdir(LIBRARY_DIR): return jsonify([])
    for dname in os.listdir(LIBRARY_DIR):
        if dname.startswith('_'): continue  # skip _tmp
        folder = os.path.join(LIBRARY_DIR, dname)
        if not os.path.isdir(folder): continue
        for fname in os.listdir(folder):
            fp = os.path.join(folder, fname)
            if not os.path.isfile(fp): continue
            st = os.stat(fp)
            entries.append({'filename':fname,'format':dname,'size':human_size(st.st_size),
                            'size_bytes':st.st_size,'mtime':st.st_mtime,
                            'mtime_display':datetime.fromtimestamp(st.st_mtime).strftime('%b %d, %Y %H:%M')})
    entries.sort(key=lambda x: x['mtime'], reverse=True)
    return jsonify(entries)

@app.route('/library/file')
def serve_library_file():
    fmt   = request.args.get('fmt','')
    fname = request.args.get('name','')
    stream = request.args.get('stream','0') == '1'
    if not fmt or not fname: return jsonify({'error':'Invalid'}), 400
    # search in any subdir matching fmt
    folder = os.path.join(LIBRARY_DIR, fmt)
    if not os.path.isdir(folder): return jsonify({'error':'Not found'}), 404
    fp = os.path.join(folder, fname)
    if not os.path.isfile(fp): return jsonify({'error':'Not found'}), 404
    if stream:
        return send_file(fp, conditional=True)
    return send_file(fp, as_attachment=True, download_name=fname)

@app.route('/library/delete', methods=['POST'])
def delete_library_file():
    data = request.json or {}; fmt = data.get('fmt',''); fname = data.get('name','')
    if not fmt or not fname: return jsonify({'error':'Invalid'}), 400
    fp = os.path.join(LIBRARY_DIR, fmt, fname)
    if not os.path.isfile(fp): return jsonify({'error':'Not found'}), 404
    os.remove(fp); return jsonify({'ok':True})

# ---- Projects routes -------------------------------------------------------

@app.route('/projects/set-path', methods=['POST'])
def set_projects_path():
    global PROJECTS_DIR
    p = (request.json or {}).get('path','').strip()
    if not p: return jsonify({'error':'No path'}), 400
    os.makedirs(p, exist_ok=True)
    PROJECTS_DIR = p
    print(f'[projects] dir -> {p}')
    return jsonify({'ok':True})

@app.route('/projects')
def list_projects():
    if not PROJECTS_DIR: return jsonify([])
    out = []
    for dname in os.listdir(PROJECTS_DIR):
        dpath = os.path.join(PROJECTS_DIR, dname)
        if not os.path.isdir(dpath): continue
        pjson_path = os.path.join(dpath, 'project.json')
        if not os.path.isfile(pjson_path): continue
        try:
            with open(pjson_path, 'r', encoding='utf-8') as f:
                proj = _js.load(f)
        except Exception:
            continue
        out.append({
            'id':          proj['id'],
            'name':        proj['name'],
            'status':      proj.get('status','wip'),
            'bpm':         proj.get('bpm'),
            'key':         proj.get('key'),
            'track_count': len(proj.get('tracks',[])),
            'created':     proj.get('created'),
            'cover_image': proj.get('cover_image'),
            'folder':      dpath,
        })
    out.sort(key=lambda x: x.get('created',''), reverse=True)
    return jsonify(out)

@app.route('/projects/create', methods=['POST'])
def create_project():
    if not PROJECTS_DIR: return jsonify({'error':'Projects path not set'}), 400
    data = request.json or {}; name = data.get('name','').strip()
    if not name: return jsonify({'error':'Name required'}), 400
    pid   = str(uuid.uuid4())
    pdir  = _proj_dir(pid, name)   # named folder: e.g. "my_track_a1b2c3d4"
    os.makedirs(os.path.join(pdir, 'tracks'), exist_ok=True)
    proj = {'id':pid,'name':name,'status':data.get('status','wip'),
            'bpm':data.get('bpm'),'key':data.get('key'),'notes':'',
            'cover_image':data.get('cover_image'),
            'created':datetime.now().isoformat(),'tracks':[],'folder':pdir}
    save_project(pid, proj)
    print(f'[projects] created: {name} -> {pdir}')
    return jsonify(proj)

@app.route('/projects/<pid>')
def get_project(pid):
    proj = load_project(pid)
    if not proj: return jsonify({'error':'Not found'}), 404
    proj['folder'] = _proj_dir(pid); return jsonify(proj)

@app.route('/projects/<pid>/update', methods=['POST'])
def update_project(pid):
    with _proj_lock(pid):
        proj = load_project(pid)
        if not proj: return jsonify({'error':'Not found'}), 404
        data = request.json or {}
        for k in ('name','status','bpm','key','notes','cover_image','created'):
            if k in data: proj[k] = data[k]
        save_project(pid, proj)
    proj['folder'] = _proj_dir(pid); return jsonify(proj)

@app.route('/projects/<pid>/delete', methods=['POST'])
def delete_project(pid):
    pdir = _proj_dir(pid)
    if not os.path.isdir(pdir): return jsonify({'error':'Not found'}), 404
    shutil.rmtree(pdir, ignore_errors=True); return jsonify({'ok':True})

AUDIO_EXTS = {'.mp3','.wav','.flac','.aac','.ogg','.m4a','.aiff','.wma'}

@app.route('/projects/<pid>/import-folder', methods=['POST'])
def import_folder(pid):
    """Expand a folder or zip file and return a list of audio file paths."""
    data = request.json or {}
    src  = data.get('path','').strip()
    if not src: return jsonify({'error':'No path'}), 400
    found = []

    if os.path.isdir(src):
        for root, dirs, files in os.walk(src):
            for fname in sorted(files):
                if os.path.splitext(fname)[1].lower() in AUDIO_EXTS:
                    found.append(os.path.join(root, fname))

    elif src.lower().endswith('.zip') and os.path.isfile(src):
        import zipfile, tempfile
        tmpdir = tempfile.mkdtemp(prefix='yt-dl-zip-')
        try:
            with zipfile.ZipFile(src, 'r') as zf:
                zf.extractall(tmpdir)
            for root, dirs, files in os.walk(tmpdir):
                for fname in sorted(files):
                    if os.path.splitext(fname)[1].lower() in AUDIO_EXTS:
                        found.append(os.path.join(root, fname))
        except Exception as e:
            return jsonify({'error': str(e)}), 400
    else:
        return jsonify({'error': 'Not a folder or zip file'}), 400

    print(f'[import-folder] found {len(found)} audio files in {src}')
    return jsonify({'imported': found})

@app.route('/projects/<pid>/tracks/add', methods=['POST'])
def add_track(pid):
    with _proj_lock(pid):
        proj = load_project(pid)
        if not proj: return jsonify({'error':'Not found'}), 404
        data = request.json or {}
        src  = data.get('path','').strip()
        name = data.get('name','').strip()
        if not src or not os.path.isfile(src): return jsonify({'error':'File not found'}), 400
        if not name: name = os.path.splitext(os.path.basename(src))[0]
        ext  = os.path.splitext(src)[1].lower() or '.mp3'
        tid  = str(uuid.uuid4())
        tdir = _track_dir(pid, tid); os.makedirs(tdir, exist_ok=True)
        v1   = f'v1{ext}'
        shutil.copy2(src, os.path.join(tdir, v1))
        track = {'id':tid,'name':name,'order':len(proj['tracks'])+1,'notes':'',
                 'current_version':1,
                 'versions':[{'version':1,'filename':v1,'added':datetime.now().isoformat(),
                               'original_name':os.path.basename(src)}],
                 'bpm':None,'key':None,'duration':None,'format':ext.lstrip('.'),
                 'gain':0.0,'pitch':0,'speed':1.0,'analyzing':True}
        proj['tracks'].append(track); save_project(pid, proj)
    threading.Thread(target=_analyze_thread,
                     args=(pid, tid, os.path.join(_track_dir(pid,tid), v1)),
                     daemon=True).start()
    return jsonify(track)

@app.route('/projects/<pid>/tracks/<tid>/update', methods=['POST'])
def update_track(pid, tid):
    with _proj_lock(pid):
        proj = load_project(pid)
        if not proj: return jsonify({'error':'Not found'}), 404
        data = request.json or {}
        for t in proj['tracks']:
            if t['id'] == tid:
                # user-settable fields including bpm and key
                for k in ('name','notes','gain','pitch','speed','order','bpm','key'):
                    if k in data: t[k] = data[k]
                save_project(pid, proj); return jsonify(t)
    return jsonify({'error':'Track not found'}), 404

@app.route('/projects/<pid>/tracks/<tid>/delete', methods=['POST'])
def delete_track(pid, tid):
    with _proj_lock(pid):
        proj = load_project(pid)
        if not proj: return jsonify({'error':'Not found'}), 404
        proj['tracks'] = [t for t in proj['tracks'] if t['id'] != tid]
        save_project(pid, proj)
    shutil.rmtree(_track_dir(pid, tid), ignore_errors=True)
    return jsonify({'ok':True})

@app.route('/projects/<pid>/tracks/<tid>/newversion', methods=['POST'])
def new_version(pid, tid):
    with _proj_lock(pid):
        proj = load_project(pid)
        if not proj: return jsonify({'error':'Not found'}), 404
        data  = request.json or {}; src = data.get('path','').strip()
        if not src or not os.path.isfile(src): return jsonify({'error':'File not found'}), 400
        track = next((t for t in proj['tracks'] if t['id']==tid), None)
        if not track: return jsonify({'error':'Track not found'}), 404
        ext   = os.path.splitext(src)[1].lower() or '.' + track['format']
        new_v = max(v['version'] for v in track['versions']) + 1
        vfile = f'v{new_v}{ext}'
        tdir  = _track_dir(pid, tid); os.makedirs(tdir, exist_ok=True)
        shutil.copy2(src, os.path.join(tdir, vfile))
        track['versions'].append({'version':new_v,'filename':vfile,
                                   'added':datetime.now().isoformat(),
                                   'original_name':os.path.basename(src)})
        track.update({'current_version':new_v,'bpm':None,'key':None,
                      'duration':None,'analyzing':True})
        save_project(pid, proj)
    threading.Thread(target=_analyze_thread,
                     args=(pid, tid, os.path.join(_track_dir(pid,tid), vfile)),
                     daemon=True).start()
    return jsonify(track)

@app.route('/projects/<pid>/tracks/<tid>/restore/<int:v>', methods=['POST'])
def restore_version(pid, tid, v):
    with _proj_lock(pid):
        proj  = load_project(pid)
        if not proj: return jsonify({'error':'Not found'}), 404
        track = next((t for t in proj['tracks'] if t['id']==tid), None)
        if not track: return jsonify({'error':'Track not found'}), 404
        if not any(x['version']==v for x in track['versions']):
            return jsonify({'error':'Version not found'}), 404
        track['current_version'] = v; track['analyzing'] = False
        save_project(pid, proj)
    return jsonify(track)

@app.route('/projects/<pid>/tracks/<tid>/stream')
def stream_track(pid, tid):
    proj  = load_project(pid)
    if not proj: return jsonify({'error':'Not found'}), 404
    track = next((t for t in proj['tracks'] if t['id']==tid), None)
    if not track: return jsonify({'error':'Track not found'}), 404
    ver   = next((v for v in track['versions'] if v['version']==track['current_version']), None)
    if not ver: return jsonify({'error':'No version'}), 404
    fp    = os.path.join(_track_dir(pid, tid), ver['filename'])
    if not os.path.isfile(fp): return jsonify({'error':'File missing'}), 404
    return send_file(fp, conditional=True)

@app.route('/library/filepath')
def library_filepath():
    """Return the absolute filesystem path of a library file."""
    fmt   = request.args.get('fmt','')
    fname = request.args.get('name','')
    if not fmt or not fname: return jsonify({'error':'Invalid'}), 400
    fp = os.path.join(LIBRARY_DIR, fmt, fname)
    if not os.path.isfile(fp): return jsonify({'error':'Not found'}), 404
    return jsonify({'path': fp})

@app.route('/library/thumb')
def library_thumb():
    """Return embedded cover art from an audio file using ffmpeg."""
    fmt   = request.args.get('fmt','')
    fname = request.args.get('name','')
    if not fmt or not fname: return jsonify({'error':'Invalid'}), 400
    fp = os.path.join(LIBRARY_DIR, fmt, fname)
    if not os.path.isfile(fp): return jsonify({'error':'Not found'}), 404
    try:
        result = _sp.run(
            [_ffmpeg(), '-i', fp, '-an', '-vcodec', 'png', '-f', 'image2', '-'],
            capture_output=True, timeout=8
        )
        if result.returncode == 0 and result.stdout:
            from flask import Response
            return Response(result.stdout, mimetype='image/png')
    except Exception as e:
        print(f'[thumb] {e}')
    return jsonify({'error':'No cover'}), 404

@app.route('/library/collections')
def list_collections():
    """List collection subfolders (playlists, albums, eps, singles)."""
    ctype = request.args.get('type', 'playlists')
    base  = os.path.join(LIBRARY_DIR, ctype)
    if not os.path.isdir(base): return jsonify([])
    out = []
    for dname in sorted(os.listdir(base)):
        dpath = os.path.join(base, dname)
        if not os.path.isdir(dpath): continue
        # count audio files and total size
        audio_exts = {'.mp3','.wav','.flac','.aac','.opus','.m4a','.ogg','.aiff','.wma'}
        files = [f for f in os.listdir(dpath)
                 if os.path.isfile(os.path.join(dpath,f))
                 and os.path.splitext(f)[1].lower() in audio_exts]
        total = sum(os.path.getsize(os.path.join(dpath,f)) for f in files)
        # look for a cover image
        cover = None
        for ext in ('.jpg','.jpeg','.png','.webp'):
            for candidate in ('cover','folder','thumb','artwork'):
                cp = os.path.join(dpath, candidate + ext)
                if os.path.isfile(cp): cover = cp; break
            if cover: break
        out.append({'name': dname, 'path': dpath, 'track_count': len(files),
                    'total_size': human_size(total), 'cover': cover})
    return jsonify(out)

@app.route('/library/collection/delete', methods=['POST'])
def delete_collection():
    path = (request.json or {}).get('path','').strip()
    if not path or not os.path.isdir(path): return jsonify({'error':'Not found'}), 404
    # safety: must be inside LIBRARY_DIR
    if not os.path.abspath(path).startswith(os.path.abspath(LIBRARY_DIR)):
        return jsonify({'error':'Forbidden'}), 403
    shutil.rmtree(path, ignore_errors=True)
    return jsonify({'ok': True})

# ---- Playlist routes -------------------------------------------------------

playlist_jobs: dict = {}

@app.route('/playlist/info', methods=['POST'])
def playlist_info():
    url = (request.json or {}).get('url','').strip()
    if not url: return jsonify({'error':'No URL'}), 400
    try:
        opts = {'quiet':True,'no_warnings':True,'extract_flat':'in_playlist',
                'skip_download':True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = info.get('entries') or []
        out_entries = []
        for e in entries:
            if not e: continue
            dur = e.get('duration') or 0
            m, s = divmod(int(dur), 60)
            out_entries.append({
                'id':           e.get('id'),
                'title':        e.get('title') or 'untitled',
                'uploader':     e.get('uploader') or e.get('artist') or '',
                'artist':       e.get('artist') or '',
                'thumbnail':    e.get('thumbnail') or e.get('thumbnails',  [{}])[-1].get('url','') if e.get('thumbnails') else e.get('thumbnail',''),
                'duration_str': f'{m}:{s:02d}' if dur else '',
            })
        return jsonify({'title': info.get('title','Playlist'), 'entries': out_entries})
    except Exception as e:
        print(f'[playlist/info] {_tb.format_exc()}')
        return jsonify({'error': str(e)}), 400

def _fmt_dur(secs):
    if not secs: return ''
    m, s = divmod(int(secs), 60)
    return f'{m}:{s:02d}'

def playlist_download_worker(job_id, url, fmt, quality, coll_type, pl_title):
    """Download every track in a playlist into a named collection subfolder."""
    safe_title = sanitize(pl_title) or 'playlist'
    dest_dir   = _unique_dir(os.path.join(LIBRARY_DIR, coll_type), safe_title)
    os.makedirs(dest_dir, exist_ok=True)
    tmp = os.path.join(LIBRARY_DIR, '_tmp', job_id)
    os.makedirs(tmp, exist_ok=True)

    meta_pp = [{'key': 'FFmpegMetadata', 'add_metadata': True}]
    # EmbedThumbnail deliberately omitted for playlists — it crashes on
    # entries that lack a thumbnail, killing the entire batch.

    base = {
        # Use title in filename — readable and avoids the "random ID" problem.
        # %(playlist_index)s pads to 3 digits automatically.
        'outtmpl':      os.path.join(tmp, '%(playlist_index)03d - %(title)s.%(ext)s'),
        'quiet':        True,
        'no_warnings':  True,
        'ignoreerrors': True,    # skip individual failed tracks, don't abort
    }

    if fmt in ('mp3', 'aac', 'opus', 'm4a', 'flac'):
        pp   = meta_pp + [{'key': 'FFmpegExtractAudio', 'preferredcodec': fmt,
                           'preferredquality': str(quality)}]
        opts = {**base, 'format': 'bestaudio/best', 'postprocessors': pp}
    elif fmt == 'wav':
        pp   = meta_pp + [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'wav'}]
        opts = {**base, 'format': 'bestaudio/best', 'postprocessors': pp}
    elif fmt in ('mp4', 'webm', 'mkv'):
        vfmt = ('bestvideo+bestaudio/best' if quality == 'best'
                else f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best')
        opts = {**base, 'format': vfmt, 'merge_output_format': fmt,
                'postprocessors': meta_pp}
    else:
        playlist_jobs[job_id].update({'status': 'error',
                                      'error': f'Unknown format: {fmt}'}); return

    def progress_hook(d):
        if d['status'] == 'finished':
            done = playlist_jobs[job_id].get('done', 0) + 1
            raw  = d.get('filename', '')
            playlist_jobs[job_id].update({
                'done':    done,
                'current': os.path.basename(raw),
            })

    opts['progress_hooks'] = [progress_hook]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info  = ydl.extract_info(url, download=True)
            total = len([e for e in (info.get('entries') or []) if e])
            playlist_jobs[job_id]['total'] = total

        # Move audio files from tmp → dest_dir with sanitised names
        audio_exts = {'.mp3', '.wav', '.flac', '.aac', '.opus',
                      '.m4a', '.ogg', '.wma', '.webm', '.mkv', '.mp4'}
        moved = 0
        for fname in sorted(os.listdir(tmp)):
            if os.path.splitext(fname)[1].lower() not in audio_exts:
                continue
            src = os.path.join(tmp, fname)
            dst = os.path.join(dest_dir, sanitize(fname) or fname)
            if os.path.exists(dst):
                dst = os.path.join(dest_dir,
                                   f'{uuid.uuid4().hex[:5]}_{sanitize(fname)}')
            shutil.move(src, dst)
            moved += 1

        done = playlist_jobs[job_id].get('done', moved)
        playlist_jobs[job_id].update({
            'status': 'done', 'done': done,
            'total':  total or done, 'title': pl_title,
        })
        print(f'[playlist] done: {done} tracks -> {dest_dir}')
    except Exception as e:
        print(f'[playlist] EXCEPTION:\n{_tb.format_exc()}')
        playlist_jobs[job_id].update({'status': 'error', 'error': str(e)})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

@app.route('/playlist/start', methods=['POST'])
def playlist_start():
    data      = request.json or {}
    url       = data.get('url','').strip()
    fmt       = data.get('format','mp3').lower()
    qual      = str(data.get('quality','320'))
    coll_type = data.get('type','playlist') + 's'   # e.g. "playlists"
    pl_title  = data.get('title','playlist')
    if not url: return jsonify({'error':'No URL'}), 400
    jid = str(uuid.uuid4())
    playlist_jobs[jid] = {'status':'downloading','done':0,'total':0,'current':'','error':None}
    threading.Thread(target=playlist_download_worker,
                     args=(jid, url, fmt, qual, coll_type, pl_title),
                     daemon=True).start()
    return jsonify({'job_id': jid})

@app.route('/playlist/status/<jid>')
def playlist_status(jid):
    j = playlist_jobs.get(jid)
    if not j: return jsonify({'error':'Not found'}), 404
    return jsonify(j)



if __name__ == '__main__':
    print('yt-dl server starting')
    print(f'library  -> {LIBRARY_DIR}')
    print(f'projects -> {PROJECTS_DIR}')
    print('http://localhost:5000')
    app.run(port=5000, debug=False, threaded=True)
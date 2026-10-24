#!/usr/bin/env python3
"""Linux desktop entry point; no third-party imports or credential storage."""
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'data' / 'desktop'
URL = 'http://127.0.0.1:8000'
PYTHON = ROOT / '.venv' / 'bin' / 'python'

def message(text, error=False):
    subprocess.run(['zenity', '--error' if error else '--info', '--title=韓文字幕學習', '--text=' + text], check=False)

def ready():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL + '/openapi.json', timeout=1) as response:
            doc = json.load(response)
        return doc.get('info', {}).get('title') == 'OCR Subtitle Learning System' and '/api/youtube/import' in doc.get('paths', {})
    except (OSError, ValueError):
        return False

def owned_pid():
    try:
        pid = int((STATE / 'server.pid').read_text())
        args = Path('/proc', str(pid), 'cmdline').read_bytes().split(b'\0')
        cwd = Path('/proc', str(pid), 'cwd').resolve()
        return pid if cwd == ROOT and b'uvicorn' in args and b'src.api.main:app' in args else None
    except (OSError, ValueError):
        return None

def main():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'launcher.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if '--stop' in sys.argv:
            pid = owned_pid()
            if pid is not None:
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    if owned_pid() is None: break
                    time.sleep(.1)
                if owned_pid() is not None:
                    raise RuntimeError('服務尚在關閉中，請稍後再試。')
                (STATE / 'server.pid').unlink(missing_ok=True)
            elif ready():
                raise RuntimeError('這個服務由其他方式啟動，請回到原本的 Terminal 按 Ctrl+C 停止。')
            if '--no-open' not in sys.argv:
                message('服務已停止。' if pid else '目前沒有由桌面圖示啟動的服務。')
            return
        if not ready():
            if not PYTHON.exists():
                raise RuntimeError('找不到專題的 .venv。請先完成 Python 環境安裝。')
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', 8000)) == 0:
                    raise RuntimeError('8000 連接埠已被其他程式使用，請先關閉該程式再試。')
            env = os.environ.copy()
            env['NO_BROWSER'] = '1'
            with (STATE / 'server.log').open('w') as log:
                if not (ROOT / 'data/dictionary_index.sqlite3').exists():
                    subprocess.run([str(PYTHON), 'scripts/build_dictionary_index.py'], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
                process = subprocess.Popen([str(PYTHON), '-m', 'uvicorn', 'src.api.main:app', '--host', '127.0.0.1', '--port', '8000'], cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            (STATE / 'server.pid').write_text(str(process.pid))
            for _ in range(120):
                if ready(): break
                if process.poll() is not None:
                    raise RuntimeError('專題啟動失敗。診斷紀錄：' + str(STATE / 'server.log'))
                time.sleep(.5)
            else:
                raise RuntimeError('啟動時間較長，請稍後再點圖示。診斷紀錄：' + str(STATE / 'server.log'))
        if '--no-open' not in sys.argv:
            webbrowser.open(URL)
        print(URL)

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if '--no-open' in sys.argv:
            print(str(exc), file=sys.stderr)
        else:
            message(str(exc), error=True)
        sys.exit(1)

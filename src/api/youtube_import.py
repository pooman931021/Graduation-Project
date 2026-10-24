"""Read YouTube captions and reuse the existing local Korean dictionary pipeline."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from src.export.csv_exporter import export_csv
from src.nlp.dictionary import DictionaryIndex
from src.nlp.tokenizer import get_tokenizer
from src.schemas import ExportRecord


class YouTubeImportError(Exception):
    pass


def parse_video_id(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
        raise ValueError('請貼上完整的 YouTube 影片連結。')
    host = (parsed.hostname or '').lower()
    if host == 'youtu.be':
        video_id = parsed.path.strip('/')
    elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com'}:
        parts = parsed.path.strip('/').split('/')
        if parsed.path == '/watch':
            video_id = parse_qs(parsed.query).get('v', [''])[0]
        elif len(parts) == 2 and parts[0] in {'shorts', 'embed', 'live'}:
            video_id = parts[1]
        else:
            video_id = ''
    else:
        video_id = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('無法辨識影片 ID，請貼上 youtube.com/watch 或 youtu.be 的影片連結。')
    return video_id


class _BoundedSession(requests.Session):
    def request(self, *args, **kwargs):
        kwargs.setdefault('timeout', 20)
        return super().request(*args, **kwargs)


def fetch_korean_captions(video_id: str) -> dict:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as error:
        raise YouTubeImportError('尚未安裝字幕工具，請在專案環境執行 pip install youtube-transcript-api。') from error
    try:
        with _BoundedSession() as session:
            tracks = list(YouTubeTranscriptApi(http_client=session).list(video_id))
            korean = sorted((track for track in tracks if track.language_code in {'ko', 'ko-KR'}),
                            key=lambda track: track.is_generated)
            if not korean:
                raise YouTubeImportError('這支影片沒有可取得的韓文字幕。可改用本機影片 OCR；語音辨識尚未加入。')
            track = korean[0]
            return {'snippets': track.fetch().to_raw_data(), 'language': track.language_code,
                    'is_generated': track.is_generated}
    except YouTubeImportError:
        raise
    except Exception as error:
        raise YouTubeImportError('暫時無法取得字幕，可能是影片限制、字幕未開放或 YouTube 拒絕連線。可稍後重試，或改用本機影片。') from error


def write_youtube_results(root: Path, video_id: str, fetched: dict, dictionary_path: str) -> dict:
    subtitles = []
    for snippet in fetched['snippets']:
        start, duration = float(snippet['start']), float(snippet['duration'])
        if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration < 0:
            continue
        text = str(snippet['text']).strip()
        if text:
            subtitles.append({'text': text, 'start': start, 'end': start + duration})
    subtitles.sort(key=lambda line: line['start'])
    if not subtitles:
        raise YouTubeImportError('字幕內容為空，請換另一支影片。')
    if len(subtitles) > 20000:
        raise YouTubeImportError('這支影片的字幕過長，請先選較短的影片。')
    warning = export_subtitle_dictionary(root, subtitles, dictionary_path, 'YouTube')
    source = {'type': 'youtube', 'youtube_id': video_id,
              'url': f'https://www.youtube.com/watch?v={video_id}',
              'language': fetched['language'], 'is_generated': fetched['is_generated'],
              'subtitle_count': len(subtitles), 'warning': warning}
    (root / 'youtube.json').write_text(json.dumps({'source': source, 'subtitles': subtitles}, ensure_ascii=False), encoding='utf-8')
    return source


def export_subtitle_dictionary(root: Path, subtitles: list[dict], dictionary_path: str, label: str, check=None):
    records = []
    warning = None
    try:
        tokenizer = get_tokenizer('mecab')
        with DictionaryIndex(dictionary_path) as dictionary:
            cache = {}
            for line in subtitles:
                if check: check()
                seen = set()
                for token in tokenizer.tokenize(line['text']):
                    # Leave particles and endings intact in the sentence, but do not add them as vocabulary.
                    if not token.pos.startswith(('NN', 'VV', 'VA', 'MAG', 'XR', 'SL')):
                        continue
                    if (token.word, token.dictionary_form) in seen:
                        continue
                    seen.add((token.word, token.dictionary_form))
                    form = token.dictionary_form
                    if form not in cache:
                        cache[form] = dictionary.lookup(form)
                    candidates = cache[form]
                    if not candidates and form != token.word:
                        if token.word not in cache:
                            cache[token.word] = dictionary.lookup(token.word)
                        candidates = cache[token.word]
                    for candidate in candidates:
                        records.append(ExportRecord(
                            原文=token.word, 中文=candidate.translation, 詞性=candidate.part_of_speech,
                            例句=candidate.example,
                            備註=f'字典原形={form}｜{label}字幕 {line["start"]:.3f}s-{line["end"]:.3f}s',
                        ))
    except Exception:
        if check: check()
        records = []
        warning = '字幕已載入，但本機斷詞或字典尚未就緒；仍可播放、點字及收藏，釋義暫不可用。'
    channel = root / 'channel1_mecab'
    channel.mkdir(parents=True, exist_ok=True)
    export_csv(records, channel / 'export.csv')
    return warning

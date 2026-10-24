"""Context-first vocabulary view, using actual companion captions when available.

Caption overlap is evidence for a dictionary-sense suggestion, not an AI translation.
Ambiguous words remain explicitly unresolved until a context model is configured.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from threading import Lock

from src.api.results_reader import read_channel_results, read_subtitles
from src.api.youtube_import import _BoundedSession
from src.api.ai_context import explain, AIError

_caption_lock = Lock()


def chinese_captions(root: Path, youtube_id: str) -> dict:
    cache = root / 'chinese_subtitles.json'
    with _caption_lock:
        if cache.exists():
            return json.loads(cache.read_text(encoding='utf-8'))
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            with _BoundedSession() as session:
                tracks = list(YouTubeTranscriptApi(http_client=session).list(youtube_id))
                preference = ['zh-Hant','zh-TW','zh-Hans','zh-CN','zh']
                tracks = sorted((track for track in tracks if track.language_code in preference),
                                key=lambda track:(track.is_generated,preference.index(track.language_code)))
                if not tracks:
                    result = {'status':'not_available','subtitles':[]}
                else:
                    track = tracks[0]
                    result = {'status':'available','language':track.language_code,
                              'is_generated':track.is_generated, 'subtitles':track.fetch().to_raw_data()}
        except Exception:
            # A connection error is not evidence that the video has no Chinese captions.
            return {'status':'connection_error','subtitles':[]}
        cache.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return result


def suggest_sense(meanings: list[dict], chinese: str) -> int | None:
    """Suggest only a unique phrase-level match; never default to dictionary order."""
    if not chinese:
        return None
    ranked = []
    normalized = re.sub(r'\s+', '', chinese)
    for index, meaning in enumerate(meanings):
        phrases = re.findall(r'[\u4e00-\u9fff]{2,}', meaning['translation'])
        score = max((len(phrase) for phrase in phrases if phrase in normalized), default=0)
        ranked.append((score,index))
    ranked.sort(reverse=True)
    if not ranked or ranked[0][0] < 2:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def resolve_context(data_root: Path, job_video_id: str, word: str, timestamp: float, channel: str,
                    ai_config: dict | None = None) -> dict:
    lines = read_subtitles(data_root, job_video_id)
    matches = [(i,line) for i,line in enumerate(lines) if abs(line['start'] - timestamp) < 0.01]
    if not matches:
        raise ValueError('找不到這個時間點的字幕。')
    index,line = matches[0]
    records = read_channel_results(data_root, job_video_id, channel)['records']
    meanings=[]; seen=set()
    for record in records:
        if record.get('原文') != word or record.get('timestamp_start') is None:
            continue
        if abs(record['timestamp_start'] - timestamp) >= 0.01:
            continue
        signature=(record.get('中文',''),record.get('詞性',''),record.get('例句',''))
        if signature in seen:
            continue
        seen.add(signature)
        meanings.append({'translation':signature[0],'pos':signature[1],'example':signature[2]})
    root=data_root/job_video_id
    chinese=''; caption_status='not_available'; source=None; exact_caption=False
    path=root/'youtube.json'
    if path.exists():
        source=json.loads(path.read_text(encoding='utf-8'))['source']
        companion=chinese_captions(root,source['youtube_id'])
        caption_status=companion['status']
        texts=[]
        matched_segments=[]
        for segment in companion['subtitles']:
            start=float(segment['start']);end=start+float(segment['duration'])
            overlap=min(end,line['end'])-max(start,line['start'])
            if overlap > 0 and str(segment['text']).strip() not in texts:
                texts.append(str(segment['text']).strip())
                matched_segments.append((start,end))
        chinese=' '.join(texts)
        exact_caption=(len(matched_segments)==1 and
                       abs(matched_segments[0][0]-line['start'])<0.05 and
                       abs(matched_segments[0][1]-line['end'])<0.05 and
                       re.sub(r'\W','',line['text'])==re.sub(r'\W','',word))
    selected=suggest_sense(meanings,chinese)
    primary=meanings[selected] if selected is not None else None
    status='caption_match' if selected is not None else 'unconfirmed'
    note='依中文字幕與字典比對，仍需確認語境。' if selected is not None else '尚未啟用 AI 語境判斷，這個單字的本句用法仍待確認。'
    if primary is None and len(meanings) == 1:
        primary=meanings[0]
        selected=0
        status='dictionary_reference'
        note='本機字典釋義，未使用 AI 判斷語境。'
    if exact_caption:
        primary={'translation':chinese,'pos':'字幕對照','example':line['text']}
        status='whole_caption'; selected=None
        note='此詞就是完整字幕句，直接對照影片同時間的中文字幕；未使用 AI 翻譯。'
    result = {'word':word,'sentence':line['text'],
            'previous_sentence':lines[index-1]['text'] if index else '',
            'next_sentence':lines[index+1]['text'] if index+1<len(lines) else '',
            'sentence_translation':chinese,'translation_source':'影片中文字幕' if chinese else None,
            'caption_status':caption_status,'primary_meaning':primary,
            'primary_status':status,
            'other_meanings':[meaning for i,meaning in enumerate(meanings) if i != selected],
            'note':note}
    result['ai_status'] = 'disabled'
    if ai_config:
        try:
            answer = explain(ai_config, word, line['text'], result['previous_sentence'],
                             result['next_sentence'], chinese, meanings)
            if answer['certain'] and answer['meaning'].strip():
                result['primary_meaning'] = {'translation':answer['meaning'], 'pos':answer['pos'],
                                             'example':line['text']}
                result['primary_status'] = 'ai_context'
                result['ai_status'] = 'ready'
                result['lemma'] = answer['lemma']
                result['note'] = answer['usage_note']
                result['other_meanings'] = [meaning for i,meaning in enumerate(meanings) if i != answer['sense_index']]
                result['companion_caption'] = chinese
                if answer['sentence_translation'].strip():
                    result['sentence_translation'] = answer['sentence_translation']
                    result['translation_source'] = 'AI 本句翻譯'
            else:
                result['ai_status'] = 'uncertain'
                result['note'] = 'AI 也無法確認此處詞義，先保留字幕與字典對照。'
        except AIError as error:
            result['ai_status'] = 'unavailable'
            result['note'] = str(error)
    return result

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.api.results_reader import read_subtitles
from src.api.youtube_import import parse_video_id, YouTubeImportError


@pytest.mark.parametrize('url',[
    'https://youtu.be/cuueUOtV0RY?si=test',
    'https://www.youtube.com/watch?v=cuueUOtV0RY',
    'https://youtube.com/shorts/cuueUOtV0RY',
])
def test_parse_supported_urls(url):
    assert parse_video_id(url) == 'cuueUOtV0RY'


@pytest.mark.parametrize('url', ['https://youtube.com.evil.example/watch?v=cuueUOtV0RY',
                               'file:///tmp/video', 'https://user:password@youtube.com/watch?v=cuueUOtV0RY',
                               'https://youtu.be/invalid'])
def test_reject_untrusted_or_invalid_urls(url):
    with pytest.raises(ValueError):
        parse_video_id(url)


def test_youtube_route_preserves_exact_subtitle_times_and_source(tmp_path, monkeypatch):
    import src.api.main as main
    monkeypatch.setattr(main, 'fetch_korean_captions', lambda _: {
        'snippets':[{'text':'친구와 함께 가요', 'start':2.125, 'duration':1.75}],
        'language':'ko', 'is_generated':False,
    })
    app = create_app(job_db_path=str(tmp_path/'jobs.db'),
                     data_output_root=str(tmp_path/'output'),upload_dir=str(tmp_path/'uploads'))
    client = TestClient(app)
    response = client.post('/api/youtube/import',json={'url':'https://youtu.be/cuueUOtV0RY'})
    assert response.status_code == 200
    body = response.json()
    assert body['source']['youtube_id'] == 'cuueUOtV0RY'
    job = body['job_id']
    status = client.get(f'/api/jobs/{job}/status').json()
    assert status['status'] == 'done'
    assert status['source']['subtitle_count'] == 1
    assert client.get(f'/api/jobs/{job}/subtitles').json()['subtitles'] == [
        {'text':'친구와 함께 가요', 'start':2.125, 'end':3.875}]
    result=client.get(f'/api/jobs/{job}/results?channel=channel1_mecab')
    assert result.status_code == 200


def test_missing_captions_does_not_create_a_fake_success(tmp_path, monkeypatch):
    import src.api.main as main
    def fail(_): raise YouTubeImportError('沒有韓文字幕')
    monkeypatch.setattr(main,'fetch_korean_captions',fail)
    app=create_app(job_db_path=str(tmp_path/'jobs.db'),data_output_root=str(tmp_path/'output'),upload_dir=str(tmp_path/'uploads'))
    client=TestClient(app)
    response=client.post('/api/youtube/import',json={'url':'https://youtu.be/cuueUOtV0RY'})
    assert response.status_code == 422
    assert response.json()['detail'] == '沒有韓文字幕'

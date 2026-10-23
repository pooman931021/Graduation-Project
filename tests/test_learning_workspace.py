"""Actual OCR timeline contract; dictionary examples must never become subtitles."""
import csv

from fastapi.testclient import TestClient

from src.api.main import create_app
from src.api.results_reader import read_subtitles


def test_timeline_deduplicates_ocr_without_using_dictionary_examples(tmp_path):
    folder = tmp_path / 'video' / 'ocr_on'
    folder.mkdir(parents=True)
    with (folder / 'ocr_results.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['frame_id','timestamp_sec','text','bbox','confidence'])
        writer.writeheader()
        for index, text in enumerate(['친구와 함께', '친구와 함께', '123', '학교에 가요']):
            writer.writerow(dict(frame_id=index, timestamp_sec=index, text=text,
                                 bbox='[[0,0],[1,0],[1,1],[0,1]]', confidence=0.9))
    result = read_subtitles(tmp_path, 'video')
    assert result == [dict(text='친구와 함께', start=0, end=1), dict(text='학교에 가요', start=3, end=3)]
    assert read_subtitles(tmp_path, 'missing') == []


def test_subtitles_route_checks_job_and_handles_unprocessed_video(tmp_path):
    app = create_app(job_db_path=str(tmp_path/'jobs.db'),
                     data_output_root=str(tmp_path/'output'), upload_dir=str(tmp_path/'uploads'))
    client = TestClient(app)
    assert client.get('/api/jobs/missing/subtitles').status_code == 404
    app.state.job_store.create_job(job_id='video', video_id='video',
                                   original_filename='video.mp4', include_channel2=False)
    response = client.get('/api/jobs/video/subtitles')
    assert response.status_code == 200
    assert response.json() == {'subtitles': []}

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.api import local_media as media
from src.api.ai_context import AIError
from src.api.job_store import JobStore
from src.api.results_reader import read_subtitles


@pytest.fixture
def env(tmp_path,monkeypatch):
    import src.api.main as main
    app=create_app(job_db_path=tmp_path/'jobs.db',data_output_root=str(tmp_path/'output'),upload_dir=str(tmp_path/'uploads'))
    calls=[]
    monkeypatch.setattr(main,'run_media_job',lambda **kwargs:calls.append(kwargs))
    return app,TestClient(app),calls,tmp_path


def upload(client,**data):
    return client.post('/api/upload',files={'file':('clip.mp4',b'test-video','video/mp4')},data=data)


def test_missing_key_rejected_before_saving_video(env):
    app,client,calls,path=env
    response=upload(client,recognition_mode='gemini_audio')
    assert response.status_code==400 and 'API' in response.json()['detail']
    assert calls==[] and not (path/'uploads').exists()


def test_local_modes_never_require_key_or_start_legacy_llm(env):
    _,client,calls,_=env
    for mode in ['local_audio','local_ocr']:
        response=upload(client,recognition_mode=mode)
        assert response.status_code==200
        assert calls[-1]['mode']==mode and calls[-1]['config'] is None
        assert calls[-1]['limit']==60


def test_cloud_uses_own_session_only(env):
    app,client,calls,_=env
    token=app.state.ai_sessions.save('test-only-key','gemini-3.5-flash-lite')
    client.cookies.set('learning_ai_session',token)
    assert upload(client,recognition_mode='gemini_ocr').status_code==200
    assert calls[-1]['config']['key']=='test-only-key'
    other=TestClient(app)
    assert upload(other,recognition_mode='gemini_ocr').status_code==400


@pytest.mark.parametrize('form',[{'recognition_mode':'unknown'},{'recognition_mode':'local_audio','whisper_model':'../../bad'},{'recognition_mode':'local_ocr','recognition_fps':'nan'},{'recognition_mode':'local_audio','recognition_seconds':'-1'}])
def test_invalid_options_are_rejected(env,form):
    _,client,calls,_=env
    assert upload(client,**form).status_code in (400,422)
    assert calls==[]


def test_audio_timestamp_validation_and_offset():
    assert media.normalize([{'text':'안녕','start':1,'end':3}],10,45)==[{'text':'안녕','start':46,'end':48}]
    for row in [{'text':'x','start':3,'end':2},{'text':'x','start':0,'end':100},{'text':'x','start':float('nan'),'end':1}]:
        with pytest.raises(media.MediaError): media.normalize([row],10)


def test_blank_frame_breaks_repeated_caption():
    lines=media.merge_frames([(0,'안녕'),(1,'안녕'),(2,''),(3,'안녕')],1,4)
    assert lines==[{'start':0,'end':2,'text':'안녕'},{'start':3,'end':4,'text':'안녕'}]


def make_store(tmp_path):
    store=JobStore(tmp_path/'jobs.db')
    store.create_job('job','job','clip.mp4',False)
    return store


def test_job_persists_real_subtitles_and_no_credentials(tmp_path,monkeypatch):
    store=make_store(tmp_path)
    monkeypatch.setattr(media,'recognize',lambda *_:([{'start':0,'end':2,'text':'안녕'}],2))
    monkeypatch.setattr('src.api.youtube_import.export_subtitle_dictionary',lambda *_:None)
    media.run_media_job('job','clip.mp4',store,str(tmp_path/'output'),'dict','gemini_audio',{'key':'secret-not-persisted','model':'test'})
    assert store.get_job('job')['status']=='done'
    assert read_subtitles(tmp_path/'output','job')==[{'start':0,'end':2,'text':'안녕'}]
    assert 'secret-not-persisted' not in (tmp_path/'output/job/local.json').read_text()


def test_empty_output_is_failure_not_fake_success(tmp_path,monkeypatch):
    store=make_store(tmp_path)
    monkeypatch.setattr(media,'recognize',lambda *_:([],2))
    media.run_media_job('job','clip.mp4',store,str(tmp_path/'output'),'dict','local_audio')
    assert store.get_job('job')['status']=='failed'
    assert not (tmp_path/'output/job/local.json').exists()


def test_cancelled_job_never_calls_provider(tmp_path,monkeypatch):
    store=make_store(tmp_path);store.update_job('job',status='cancelled')
    monkeypatch.setattr(media,'recognize',lambda *_:pytest.fail('cancelled job must not run'))
    media.run_media_job('job','clip.mp4',store,str(tmp_path/'output'),'dict','gemini_audio')
    assert store.get_job('job')['status']=='cancelled'


def test_upstream_exception_does_not_leak_key(tmp_path,monkeypatch):
    store=make_store(tmp_path)
    def fail(*_):raise RuntimeError('secret-not-persisted in request body')
    monkeypatch.setattr(media,'recognize',fail)
    media.run_media_job('job','clip.mp4',store,str(tmp_path/'output'),'dict','gemini_audio')
    result=store.get_job_with_stages('job')
    assert result['status']=='failed' and 'secret-not-persisted' not in json.dumps(result)


def test_gemini_multimodal_contract(monkeypatch):
    captured={}
    def fake(key,model,payload,**kwargs):
        captured.update(payload)
        assert kwargs['timeout']==(10,120)
        return '{"segments":[{"start":0,"end":1,"text":"안녕"}]}'
    monkeypatch.setattr(media,'_request',fake)
    answer=media._gemini({'key':'test','model':'test'},[{'type':'audio','data':'test','mime_type':'audio/wav'}],'transcribe',media.Transcript)
    assert answer.segments[0].text=='안녕'
    assert captured['input'][1]['type']=='audio'
    assert 'schema' in captured['response_format']


def test_whisper_tail_padding_does_not_discard_valid_segments():
    stats={}
    # A 15-second final chunk: one real crossing, padded model output, zero-duration tail.
    result=media.normalize([
        {'start':0,'end':2,'text':'안녕'},
        {'start':14.83,'end':16.03,'text':'마지막'},
        {'start':16.03,'end':18.03,'text':'out of audio'},
        {'start':10,'end':10,'text':'zero duration'},
    ],15,45,clip=True,stats=stats)
    assert result==[{'start':45,'end':47,'text':'안녕'},{'start':59.83,'end':60,'text':'마지막'}]
    assert stats=={'trimmed':1,'dropped':2}
    # Strict validation remains enabled for Gemini model output.
    with pytest.raises(media.MediaError):
        media.normalize([{'start':14.83,'end':16.03,'text':'마지막'}],15,45)


def test_model_timestamp_corrections_are_visible_in_job_warning(tmp_path,monkeypatch):
    store=make_store(tmp_path)
    def recognized(*args):
        args[-1].append('已略過 2 段超出音軌範圍的結果')
        return [{'start':0,'end':1,'text':'안녕'}],3
    monkeypatch.setattr(media,'recognize',recognized)
    monkeypatch.setattr('src.api.youtube_import.export_subtitle_dictionary',lambda *_:None)
    media.run_media_job('job','clip.mp4',store,str(tmp_path/'output'),'dict','local_audio')
    source=json.loads((tmp_path/'output/job/local.json').read_text())['source']
    assert '已略過 2 段' in source['warning']
    assert store.get_job('job')['status']=='done'


def test_obvious_decoder_loop_not_accepted_as_successful_speech():
    repeated=[{'text':'새우','start':i,'end':i+1} for i in range(10)]
    with pytest.raises(media.MediaError,match='大量重複'):
        media.check_speech_quality(repeated)
    media.check_speech_quality([{'text':text} for text in ['안녕하세요','오늘은 여행','날씨가 좋아요','맛있어요','다음에 만나요']])

import json

import pytest
from fastapi.testclient import TestClient

from src.api.ai_context import AISessions, AIError, explain, _request
from src.api.main import create_app


@pytest.fixture
def clients(tmp_path,monkeypatch):
    import src.api.main as main
    monkeypatch.setattr(main,'test_connection',lambda *_:None)
    app=create_app(job_db_path=str(tmp_path/'jobs.db'),data_output_root=str(tmp_path/'output'),upload_dir=str(tmp_path/'uploads'))
    return app,TestClient(app,base_url='http://127.0.0.1:8766'),TestClient(app,base_url='http://127.0.0.1:8766')


def test_credentials_are_optional_and_isolated_per_browser(clients):
    app,first,second=clients
    assert first.get('/api/ai/settings').json()['configured'] is False
    response=first.post('/api/ai/settings',json={'api_key':'fake-test-only-key','model':'gemini-3.5-flash-lite'},headers={'Origin':'http://127.0.0.1:8766'})
    assert response.status_code==200
    assert 'fake-test-only-key' not in response.text
    assert 'httponly' in response.headers['set-cookie'].lower()
    assert 'samesite=strict' in response.headers['set-cookie'].lower()
    assert first.get('/api/ai/settings').json()['configured'] is True
    assert second.get('/api/ai/settings').json()['configured'] is False
    assert first.delete('/api/ai/settings',headers={'Origin':'http://127.0.0.1:8766'}).status_code==200
    assert first.get('/api/ai/settings').json()['configured'] is False


def test_cross_origin_changes_are_rejected_before_testing_credentials(clients,monkeypatch):
    import src.api.main as main
    monkeypatch.setattr(main,'test_connection',lambda *_:pytest.fail('Must not call provider'))
    _,client,_=clients
    response=client.post('/api/ai/settings',json={'api_key':'fake-test-only-key'},headers={'Origin':'https://elsewhere.example'})
    assert response.status_code==403
    assert client.post('/api/ai/settings',json={'api_key':'fake-test-only-key'}).status_code==403


def test_failed_validation_does_not_enable_ai(clients,monkeypatch):
    import src.api.main as main
    def fail(*_): raise AIError('測試失敗')
    monkeypatch.setattr(main,'test_connection',fail)
    _,client,_=clients
    response=client.post('/api/ai/settings',json={'api_key':'fake-test-only-key'},headers={'Origin':'http://127.0.0.1:8766'})
    assert response.status_code==400
    assert client.get('/api/ai/settings').json()['configured'] is False


def test_session_expires_and_key_is_removed(monkeypatch):
    import src.api.ai_context as module
    now=[10]
    monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    store=AISessions();token=store.save('fake-key','model')
    assert store.get(token)
    now[0]+=8*3600+1
    assert store.get(token) is None
    assert not store._sessions


def test_provider_error_never_returns_credential_or_upstream_body(monkeypatch):
    import src.api.ai_context as module
    class Response:
        ok=False;status_code=403
        text='sensitive upstream text fake-test-only-key'
    monkeypatch.setattr(module.requests,'post',lambda *_args,**_kwargs:Response())
    with pytest.raises(AIError) as caught:_request('fake-test-only-key','gemini-test',{})
    assert 'fake-test-only-key' not in str(caught.value)
    assert 'sensitive' not in str(caught.value)


def test_context_uses_short_surrounding_sentences_and_validates_sense(monkeypatch):
    import src.api.ai_context as module
    module._cache.clear()
    captured={}
    def fake_request(key,model,payload):
        captured.update(payload)
        return json.dumps({'certain':True,'meaning':'你好','usage_note':'這裡用來打招呼。',
                           'sentence_translation':'大家好','lemma':'안녕','pos':'感嘆詞','sense_index':1})
    monkeypatch.setattr(module,'_request',fake_request)
    answer=explain({'key':'fake-test-only-key','model':'gemini-test'},'안녕','안녕','','今天',
                   '大家好',[{'translation':'安寧'},{'translation':'你好'}])
    assert answer['meaning']=='你好'
    assert 'response_format' in captured
    assert json.loads(captured['input'])['sentence']=='안녕'
    assert '不要硬選第一個義項' in captured['system_instruction']


def test_no_key_never_calls_ai_provider(tmp_path,monkeypatch):
    import src.api.word_context as module
    monkeypatch.setattr(module,'read_subtitles',lambda *_:[{'text':'친구','start':2,'end':3}])
    monkeypatch.setattr(module,'read_channel_results',lambda *_:{'records':[]})
    monkeypatch.setattr(module,'explain',lambda *_:pytest.fail('No API means no AI calls'))
    result=module.resolve_context(tmp_path,'job','친구',2,'channel1_mecab')
    assert result['ai_status']=='disabled'


def test_context_places_ai_meaning_first_and_folds_other_senses(tmp_path,monkeypatch):
    import src.api.word_context as module
    monkeypatch.setattr(module,'read_subtitles',lambda *_:[{'text':'안녕','start':2,'end':3}])
    monkeypatch.setattr(module,'read_channel_results',lambda *_:{'records':[
        {'原文':'안녕','中文':'安寧','timestamp_start':2,'詞性':'名詞'},
        {'原文':'안녕','中文':'你好','timestamp_start':2,'詞性':'感嘆詞'},
    ]})
    monkeypatch.setattr(module,'explain',lambda *_:{'certain':True,'meaning':'你好','usage_note':'用來打招呼。',
        'sentence_translation':'大家好','lemma':'안녕','pos':'感嘆詞','sense_index':1})
    result=module.resolve_context(tmp_path,'job','안녕',2,'channel1_mecab',ai_config={'key':'fake-test-only-key'})
    assert result['primary_meaning']['translation']=='你好'
    assert result['primary_status']=='ai_context'
    assert result['other_meanings'][0]['translation']=='安寧'
    assert len(result['other_meanings'])==1


@pytest.mark.parametrize('code,expected',[(500,'內部錯誤'),(503,'忙碌'),(413,'資料大小'),(429,'額度'),(418,'進一步檢查')])
def test_provider_failure_preserves_safe_http_code_without_body(monkeypatch,code,expected):
    import src.api.ai_context as module
    class Failed:
        ok=False
        status_code=code
        text='private-key-and-media-do-not-expose'
    calls=[]
    def post(*args,**kwargs):calls.append(1);return Failed()
    monkeypatch.setattr(module.requests,'post',post)
    with pytest.raises(AIError) as caught:
        module._request('secret-not-output','gemini-test',{})
    text=str(caught.value)
    assert f'HTTP {code}' in text and expected in text
    assert 'secret-not-output' not in text and 'private-key' not in text
    assert len(calls)==1  # No automatic retries or extra paid calls.

import csv
import json

from src.api.word_context import suggest_sense, resolve_context


def test_sense_selection_uses_caption_evidence_instead_of_first_dictionary_entry():
    meanings=[{'translation':'价格'},{'translation':'价值'}]
    assert suggest_sense(meanings,'这件事情很有价值') == 1
    assert suggest_sense(meanings,'这件事情非常特别') is None


def test_tied_and_single_character_matches_stay_unconfirmed():
    assert suggest_sense([{'translation':'朋友'},{'translation':'朋友，友人'}],'朋友来了') is None
    assert suggest_sense([{'translation':'好'}],'你好') is None


def test_context_preserves_video_sentence_and_separates_other_senses(tmp_path,monkeypatch):
    import src.api.word_context as module
    root=tmp_path/'job';(root/'channel1_mecab').mkdir(parents=True)
    (root/'youtube.json').write_text(json.dumps({
        'source':{'youtube_id':'cuueUOtV0RY'},
        'subtitles':[{'text':'그 가치가 커요','start':2,'end':4}],
    }))
    with (root/'channel1_mecab/export.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['原文','中文','詞性','例句','備註']);writer.writeheader()
        for translation in ['价格','价值']:
            writer.writerow({'原文':'가치','中文':translation,'詞性':'명사','例句':'字典例句', '備註':'字幕 2.0s-4.0s'})
    monkeypatch.setattr(module,'chinese_captions',lambda *_:{'status':'available','subtitles':[
        {'text':'很有价值','start':2,'duration':2},
        {'text':'不相关句子','start':10,'duration':2},
    ]})
    result=resolve_context(tmp_path,'job','가치',2,'channel1_mecab')
    assert result['sentence']=='그 가치가 커요'
    assert result['sentence_translation']=='很有价值'
    assert result['primary_meaning']['translation']=='价值'
    assert result['other_meanings'][0]['translation']=='价格'
    assert result['primary_status']=='caption_match'


def test_no_caption_does_not_invent_video_specific_meaning(tmp_path,monkeypatch):
    import src.api.word_context as module
    monkeypatch.setattr(module,'read_subtitles',lambda *_:[{'text':'안녕','start':2,'end':3}])
    monkeypatch.setattr(module,'read_channel_results',lambda *_:{'records':[
        {'原文':'안녕','中文':'安宁','timestamp_start':2,'詞性':'名詞'},
        {'原文':'안녕','中文':'你好','timestamp_start':2,'詞性':'感嘆詞'},
    ]})
    result=resolve_context(tmp_path,'job','안녕',2,'channel1_mecab')
    assert result['primary_meaning'] is None
    assert result['primary_status']=='unconfirmed'
    assert len(result['other_meanings'])==2


def test_whole_word_caption_can_use_exactly_aligned_chinese_without_guessing(tmp_path,monkeypatch):
    import src.api.word_context as module
    root=tmp_path/'job';root.mkdir()
    (root/'youtube.json').write_text(json.dumps({'source':{'youtube_id':'cuueUOtV0RY'}}))
    monkeypatch.setattr(module,'read_subtitles',lambda *_:[{'text':'안녕','start':2,'end':3}])
    monkeypatch.setattr(module,'read_channel_results',lambda *_:{'records':[]})
    monkeypatch.setattr(module,'chinese_captions',lambda *_:{'status':'available','subtitles':[
        {'text':'大家好','start':2,'duration':1},
    ]})
    result=resolve_context(tmp_path,'job','안녕',2,'channel1_mecab')
    assert result['primary_meaning']['translation']=='大家好'
    assert result['primary_status']=='whole_caption'


def test_broader_chinese_caption_does_not_become_a_single_word_translation(tmp_path,monkeypatch):
    import src.api.word_context as module
    root=tmp_path/'job';root.mkdir()
    (root/'youtube.json').write_text(json.dumps({'source':{'youtube_id':'cuueUOtV0RY'}}))
    monkeypatch.setattr(module,'read_subtitles',lambda *_:[{'text':'안녕','start':2,'end':3}])
    monkeypatch.setattr(module,'read_channel_results',lambda *_:{'records':[]})
    monkeypatch.setattr(module,'chinese_captions',lambda *_:{'status':'available','subtitles':[
        {'text':'大家好，今天我们要去纽约','start':1,'duration':5},
    ]})
    result=resolve_context(tmp_path,'job','안녕',2,'channel1_mecab')
    assert result['primary_meaning'] is None
    assert result['primary_status']=='unconfirmed'


def test_single_dictionary_meaning_available_without_ai_but_not_labeled_context(tmp_path,monkeypatch):
    import src.api.word_context as module
    monkeypatch.setattr(module,'read_subtitles',lambda *_:[{'text':'오늘 날씨가 좋아요','start':0,'end':3}])
    monkeypatch.setattr(module,'read_channel_results',lambda *_:{'records':[
        {'原文':'날씨','中文':'天气','timestamp_start':0,'詞性':'명사'},
    ]})
    monkeypatch.setattr(module,'explain',lambda *_: (_ for _ in ()).throw(AssertionError('No AI without API')))
    result=resolve_context(tmp_path,'job','날씨',0,'channel1_mecab')
    assert result['primary_meaning']['translation']=='天气'
    assert result['primary_status']=='dictionary_reference'
    assert result['ai_status']=='disabled'
    assert result['other_meanings']==[]

"""
測試 src/api/main.py（Phase 6 REST API 路由契約）。

用 `create_app()` 搭配 `tmp_path` 建立互相獨立的 app + JobStore + 輸出目錄，
並將 `src.api.main.run_full_pipeline`（背景工作函式）替換成假的、立即完成
的版本——路由層的契約（狀態碼、回應格式、404/400 訊息）不需要真正的
GPU/Ollama 也能完整驗證；真正跑通整條 pipeline 的驗證屬於
`tests/test_pipeline_runner.py` 與使用者本機的端到端測試範圍。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import src.api.main as main_mod  # noqa: E402
from src.api.main import create_app  # noqa: E402


def _write_export_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import pandas as pd
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    """建立一份獨立的 app + TestClient，並把背景 pipeline 換成立即完成的假版本。"""
    app = create_app(
        job_db_path=str(tmp_path / "jobs.sqlite3"),
        data_output_root=str(tmp_path / "output"),
        upload_dir=str(tmp_path / "uploads"),
    )

    def fake_run_full_pipeline(job_id, video_path, job_store, **kwargs):
        job_store.update_job(job_id, status="done", current_stage="finished")

    monkeypatch.setattr(main_mod, "run_full_pipeline", fake_run_full_pipeline)

    video_path = tmp_path / "fake.mkv"
    video_path.write_bytes(b"FAKEVIDEOBYTES")

    return {
        "client": TestClient(app), "tmp_path": tmp_path,
        "video_path": video_path, "app": app,
    }


def _upload(env, **form):
    with open(env["video_path"], "rb") as f:
        return env["client"].post(
            "/api/upload", files={"file": ("fake.mkv", f, "video/x-matroska")}, data=form,
        )


def test_upload_returns_job_id_and_queued_status_code(api_env):
    resp = _upload(api_env)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job_id"] == body["video_id"]
    assert len(body["job_id"]) > 0


def test_upload_rejects_unsupported_extension(api_env, tmp_path):
    bad_file = tmp_path / "not_a_video.txt"
    bad_file.write_text("hello")
    with open(bad_file, "rb") as f:
        resp = api_env["client"].post("/api/upload", files={"file": ("not_a_video.txt", f, "text/plain")})
    assert resp.status_code == 400
    assert "不支援" in resp.json()["detail"]


def test_upload_saves_file_and_job_becomes_done_via_fake_pipeline(api_env):
    resp = _upload(api_env)
    job_id = resp.json()["job_id"]

    status_resp = api_env["client"].get(f"/api/jobs/{job_id}/status")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "done"
    assert body["current_stage"] == "finished"
    assert body["available_channels"] == []  # 假 pipeline 沒有真的寫出任何 channel 結果


def test_upload_include_channel2_false_is_persisted(api_env):
    resp = _upload(api_env, include_channel2="false")
    job_id = resp.json()["job_id"]
    status = api_env["client"].get(f"/api/jobs/{job_id}/status").json()
    assert status["include_channel2"] == 0


def test_status_unknown_job_returns_404(api_env):
    resp = api_env["client"].get("/api/jobs/does-not-exist/status")
    assert resp.status_code == 404


# ----------------------------------------------------------------------
# Phase 7：/api/upload 新增的測試期間參數是否正確解析並轉呈給
# run_full_pipeline()（見 PROJECT_NOTES.md 第 16 節）。這裡改用會「記錄
# 收到的 kwargs」的假函式，而不是 api_env fixture 預設那個只會標記
# done 的假函式，藉此驗證轉呈的參數值本身是否正確。
# ----------------------------------------------------------------------

def _upload_with_capture(api_env, monkeypatch, **form):
    captured = {}

    def capturing_fake(job_id, video_path, job_store, **kwargs):
        captured.update(kwargs)
        job_store.update_job(job_id, status="done", current_stage="finished")

    monkeypatch.setattr(main_mod, "run_full_pipeline", capturing_fake)
    resp = _upload(api_env, **form)
    return resp, captured


def test_upload_default_params_forwarded_correctly(api_env, monkeypatch):
    resp, captured = _upload_with_capture(api_env, monkeypatch)
    assert resp.status_code == 200
    assert captured["include_channel1"] is True
    assert captured["channel1_engines"] is None
    assert captured["include_channel2"] is True
    assert captured["include_channel3"] is True
    assert captured["denoise"] is True
    assert captured["sample_rate_fps"] == 1.0
    assert captured["max_frames"] is None
    assert captured["model"] == main_mod.API_CONFIG["llm_model"]
    assert captured["base_url"] == main_mod.API_CONFIG["llm_base_url"]
    assert captured["config_overrides"] == {
        "min_confidence": None, "dedup_max_gap_sec": None,
        "clahe_clip_limit": None, "denoise_strength": None,
    }


def test_upload_channel_toggles_and_engine_subset_forwarded(api_env, monkeypatch):
    resp, captured = _upload_with_capture(
        api_env, monkeypatch,
        include_channel1="true", **{"channel1_engines": "okt, mecab"},
        include_channel2="false", include_channel3="false",
    )
    assert resp.status_code == 200
    assert captured["include_channel1"] is True
    assert captured["channel1_engines"] == ["okt", "mecab"]
    assert captured["include_channel2"] is False
    assert captured["include_channel3"] is False


def test_upload_denoise_off_forwarded(api_env, monkeypatch):
    resp, captured = _upload_with_capture(api_env, monkeypatch, denoise="false")
    assert resp.status_code == 200
    assert captured["denoise"] is False


def test_upload_test_parameters_forwarded(api_env, monkeypatch):
    resp, captured = _upload_with_capture(
        api_env, monkeypatch,
        sample_rate_fps="2.5", max_frames="20", model="qwen2.5:14b",
        base_url="http://custom-ollama:9999",
        min_confidence="0.6", dedup_max_gap_sec="2.0",
        clahe_clip_limit="4.0", denoise_strength="15",
    )
    assert resp.status_code == 200
    assert captured["sample_rate_fps"] == 2.5
    assert captured["max_frames"] == 20
    assert captured["model"] == "qwen2.5:14b"
    assert captured["base_url"] == "http://custom-ollama:9999"
    assert captured["config_overrides"] == {
        "min_confidence": 0.6, "dedup_max_gap_sec": 2.0,
        "clahe_clip_limit": 4.0, "denoise_strength": 15,
    }


def test_results_default_channel_is_channel3(api_env):
    resp = _upload(api_env)
    job_id = resp.json()["job_id"]

    _write_export_csv(
        api_env["tmp_path"] / "output" / job_id / "channel3" / "export.csv",
        [{"原文": "집", "中文": "家", "詞性": "명사", "例句": "", "備註": ""}],
    )

    results = api_env["client"].get(f"/api/jobs/{job_id}/results")
    assert results.status_code == 200
    body = results.json()
    assert body["channel"] == "channel3"
    assert body["record_count"] == 1


def test_results_with_explicit_channel_query_param(api_env):
    resp = _upload(api_env)
    job_id = resp.json()["job_id"]

    _write_export_csv(
        api_env["tmp_path"] / "output" / job_id / "channel1_okt" / "export.csv",
        [{"原文": "학교", "中文": "学校", "詞性": "명사", "例句": "", "備註": ""}],
    )

    results = api_env["client"].get(f"/api/jobs/{job_id}/results", params={"channel": "channel1_okt"})
    assert results.status_code == 200
    assert results.json()["channel"] == "channel1_okt"


def test_results_invalid_channel_returns_400(api_env):
    resp = _upload(api_env)
    job_id = resp.json()["job_id"]
    results = api_env["client"].get(f"/api/jobs/{job_id}/results", params={"channel": "not_a_real_channel"})
    assert results.status_code == 400


def test_results_channel_not_yet_produced_returns_404_with_context(api_env):
    resp = _upload(api_env)
    job_id = resp.json()["job_id"]
    results = api_env["client"].get(f"/api/jobs/{job_id}/results")  # channel3，尚未寫出任何檔案
    assert results.status_code == 404
    detail = results.json()["detail"]
    assert "尚無結果" in detail
    assert "工作狀態" in detail


def test_results_unknown_job_returns_404(api_env):
    resp = api_env["client"].get("/api/jobs/does-not-exist/results")
    assert resp.status_code == 404


def test_export_csv_returns_file_content(api_env):
    resp = _upload(api_env)
    job_id = resp.json()["job_id"]
    _write_export_csv(
        api_env["tmp_path"] / "output" / job_id / "channel3" / "export.csv",
        [{"原文": "집", "中文": "家", "詞性": "명사", "例句": "", "備註": ""}],
    )

    export_resp = api_env["client"].post(f"/api/jobs/{job_id}/export", params={"format": "csv"})
    assert export_resp.status_code == 200
    assert "text/csv" in export_resp.headers["content-type"]
    assert "집" in export_resp.text


def test_export_format_anki_is_rejected(api_env):
    resp = _upload(api_env)
    job_id = resp.json()["job_id"]
    export_resp = api_env["client"].post(f"/api/jobs/{job_id}/export", params={"format": "anki"})
    assert export_resp.status_code == 400
    assert "csv" in export_resp.json()["detail"]


def test_export_unknown_job_returns_404(api_env):
    resp = api_env["client"].post("/api/jobs/does-not-exist/export", params={"format": "csv"})
    assert resp.status_code == 404


def test_export_missing_channel_file_returns_404(api_env):
    resp = _upload(api_env)
    job_id = resp.json()["job_id"]
    export_resp = api_env["client"].post(f"/api/jobs/{job_id}/export", params={"format": "csv"})
    assert export_resp.status_code == 404


# ----------------------------------------------------------------------
# Phase 7：GET /api/jobs/{job_id}/frame（真實 OCR 影格截圖，見
# PROJECT_NOTES.md 第 16 節）
# ----------------------------------------------------------------------

def test_frame_route_returns_nearest_frame_image(api_env):
    resp = _upload(api_env)
    job_id = resp.json()["job_id"]
    video_id = resp.json()["video_id"]

    frames_dir = Path(api_env["tmp_path"]) / "output" / video_id / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    import csv
    with open(frames_dir / "manifest.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["frame_id", "timestamp_sec", "filename"])
        writer.writeheader()
        writer.writerow({"frame_id": 0, "timestamp_sec": 5.0, "filename": "frame_000000.jpg"})
    (frames_dir / "frame_000000.jpg").write_bytes(b"\xff\xd8\xfake-jpeg-bytes")

    frame_resp = api_env["client"].get(f"/api/jobs/{job_id}/frame", params={"timestamp": 5.2})
    assert frame_resp.status_code == 200
    assert frame_resp.headers["content-type"] == "image/jpeg"
    assert frame_resp.content == b"\xff\xd8\xfake-jpeg-bytes"


def test_frame_route_missing_manifest_returns_404(api_env):
    resp = _upload(api_env)
    job_id = resp.json()["job_id"]
    frame_resp = api_env["client"].get(f"/api/jobs/{job_id}/frame", params={"timestamp": 1.0})
    assert frame_resp.status_code == 404


def test_frame_route_unknown_job_returns_404(api_env):
    frame_resp = api_env["client"].get("/api/jobs/does-not-exist/frame", params={"timestamp": 1.0})
    assert frame_resp.status_code == 404


# ----------------------------------------------------------------------
# Phase 7：GET /api/environment（測試期間方便功能，見 PROJECT_NOTES.md 16 節）
#
# 這裡刻意用 monkeypatch 換掉真正的 check_environment()，只驗證路由本身的
# 契約（預設 dict_db/base_url 有沒有正確從 config 帶入、回應格式）。不呼叫
# 真正的 KoNLPy 檢查邏輯——那部分已經在 tests/test_environment_check.py 用
# 主執行緒直接驗證過。原因：check_konlpy_engines() 內部會啟動 JVM
# (JPype)，若在 TestClient 底層的背景執行緒（而非主執行緒）第一次啟動 JVM，
# 已實測會導致「該次要求本身其實正確回應，但整個 Python 行程之後無法正常
# 結束」的已知環境限制（見 PROJECT_NOTES.md 16.3 節），為了不讓整個測試
# 套件的執行也被這個限制拖累，路由測試一律搭配假的 check_environment()。
# ----------------------------------------------------------------------

def test_environment_route_uses_default_config_and_returns_items(api_env, monkeypatch):
    captured = {}

    def fake_check_environment(dict_db_path, ollama_base_url):
        captured["dict_db_path"] = dict_db_path
        captured["ollama_base_url"] = ollama_base_url
        return [{"name": "假項目", "ok": True, "detail": "來自 monkeypatch"}]

    monkeypatch.setattr(main_mod, "check_environment", fake_check_environment)

    resp = api_env["client"].get("/api/environment")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == [{"name": "假項目", "ok": True, "detail": "來自 monkeypatch"}]
    # 沒有指定 query string 時，應該帶入 config/settings.py 的預設值
    assert captured["dict_db_path"] == main_mod.DICTIONARY_CONFIG["index_db_path"]
    assert captured["ollama_base_url"] == main_mod.API_CONFIG["llm_base_url"]


def test_environment_route_accepts_query_overrides(api_env, monkeypatch):
    captured = {}

    def fake_check_environment(dict_db_path, ollama_base_url):
        captured["dict_db_path"] = dict_db_path
        captured["ollama_base_url"] = ollama_base_url
        return []

    monkeypatch.setattr(main_mod, "check_environment", fake_check_environment)

    resp = api_env["client"].get(
        "/api/environment",
        params={"dict_db": "/custom/dict.sqlite3", "base_url": "http://custom:1234"},
    )
    assert resp.status_code == 200
    assert captured["dict_db_path"] == "/custom/dict.sqlite3"
    assert captured["ollama_base_url"] == "http://custom:1234"


def test_get_job_logs_returns_logs_and_supports_after_id(api_env):
    resp = _upload(api_env)
    job_id = resp.json()["job_id"]
    job_store = api_env["app"].state.job_store

    job_store.record_log(job_id, "Log 1: pipeline started")
    job_store.record_log(job_id, "Log 2: frame sampled")

    logs_resp = api_env["client"].get(f"/api/jobs/{job_id}/logs")
    assert logs_resp.status_code == 200
    data = logs_resp.json()
    assert data["job_id"] == job_id
    assert len(data["logs"]) == 2
    assert data["logs"][0]["message"] == "Log 1: pipeline started"

    first_id = data["logs"][0]["id"]
    incr_resp = api_env["client"].get(f"/api/jobs/{job_id}/logs?after_id={first_id}")
    assert incr_resp.status_code == 200
    assert len(incr_resp.json()["logs"]) == 1
    assert incr_resp.json()["logs"][0]["message"] == "Log 2: frame sampled"


def test_cancel_job_route_sets_cancelled_status(api_env):
    resp = _upload(api_env)
    job_id = resp.json()["job_id"]

    cancel_resp = api_env["client"].post(f"/api/jobs/{job_id}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelling"

    status_resp = api_env["client"].get(f"/api/jobs/{job_id}/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "cancelled"


def test_system_shutdown_endpoint(api_env, monkeypatch):
    # 避免真實測試中殺死 pytest 行程
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("os._exit", lambda code: None)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    resp = api_env["client"].post("/api/system/shutdown")
    assert resp.status_code == 200
    assert resp.json()["status"] == "shutting_down"



def test_uploaded_video_can_be_restored_and_supports_range(api_env):
    response=_upload(api_env)
    job=response.json()['job_id']
    client=api_env['client']
    assert client.get(f'/api/jobs/{job}/status').json()['video_url']==f'/api/jobs/{job}/video'
    video=client.get(f'/api/jobs/{job}/video',headers={'Range':'bytes=0-3'})
    assert video.status_code==206
    assert video.content==b'FAKE'
    assert video.headers['content-range'].startswith('bytes 0-3/')
    assert client.get('/api/jobs/not-found/video').status_code==404


def test_video_repair_uses_original_without_altering_it(api_env,monkeypatch):
    import src.api.main as main
    captured=[]
    monkeypatch.setattr(main,'prepare_playback',lambda video,root:captured.append((video,root)))
    job=_upload(api_env).json()['job_id']
    response=api_env['client'].post(f'/api/jobs/{job}/prepare-video')
    assert response.status_code==200
    assert response.json()['video_url'].endswith('?compatible=1')
    assert captured[0][0].read_bytes()==b'FAKEVIDEOBYTES'

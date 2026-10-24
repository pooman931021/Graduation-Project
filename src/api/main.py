"""
Step 7 後端：REST API（Phase 6 已實作）
==========================================
提供前端呼叫之標準化 API，串接完整 pipeline（Phase 1-3 + Channel 1/2/3）。

使用者決策紀錄（2026-07-26，見 PROJECT_NOTES.md 第 15 節）：
  1) `POST /api/upload` 真正接受任意影片上傳，並觸發完整流程（見
     `src/api/pipeline_runner.py::run_full_pipeline`），而不只是包裝既有
     的 10 支測試影片。
  2) `GET /api/jobs/{job_id}/results` 預設回傳一組通道（`channel3`），
     其餘 5 組（channel1_okt/komoran/kkma/mecab、channel2）透過
     `?channel=` 查詢參數指定——因為「最終保留哪個通道」明確延後到
     專案尾聲才決定，API 不預先替使用者做這個選擇。
  3) 工作狀態採輕量持久化（`src/api/job_store.py`，SQLite），伺服器
     重啟後仍可查詢先前工作的進度與結果。
  4) 匯出只支援 CSV；Anki 本身就是吃 CSV 匯入，不需要另外接
     `src/export/anki_exporter.py`（該模組目前仍未經測試，Phase 6 故意
     不擴大範圍去驗證它）。

**重要限制**：本檔案可在沙盒中完整驗證路由的請求/回應契約與 job 狀態機
（見 `tests/test_api_main.py`，皆用假的背景工作函式，不需要真正 GPU/
Ollama）。但 `/api/upload` 觸發的背景流程一旦真的執行 Phase 3 OCR，需要
真實 GPU；Channel 2/3 則需要本機已啟動的 Ollama——這些「真正上傳一支
影片、等它跑完」的端到端驗證，只能由使用者在本機（RTX 4060 + Ollama）
執行，沙盒無法代勞。
"""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path
from typing import Optional
import json
from pydantic import BaseModel, Field, SecretStr
from urllib.parse import urlparse
import ipaddress
from fastapi import Response

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Phase 7 前端頁面目錄（frontend/index.html + app.js + style.css）。
_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

from config.settings import API_CONFIG, DICTIONARY_CONFIG
from src.api.environment_check import check_environment
from src.api.job_store import JobStore
from src.api.pipeline_runner import run_full_pipeline
from src.api.local_media import MODES, run_media_job, prepare_playback, MediaError
from src.api.youtube_import import parse_video_id, fetch_korean_captions, write_youtube_results, YouTubeImportError
from src.api.word_context import resolve_context
from src.api.ai_context import AISessions, AIError, COOKIE_NAME, DEFAULT_MODEL, test_connection
from src.api.results_reader import (
    ChannelResultsNotFound,
    VALID_CHANNELS,
    export_csv_path,
    list_available_channels,
    read_channel_results,
    read_subtitles,
    resolve_frame_path,
)


class YouTubeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class WordContextRequest(BaseModel):
    word: str = Field(min_length=1, max_length=120)
    timestamp: float = Field(ge=0, allow_inf_nan=False)
    channel: str = "channel1_mecab"


class AISettingsRequest(BaseModel):
    api_key: SecretStr
    model: str = Field(default=DEFAULT_MODEL, pattern=r"^[A-Za-z0-9_.-]{1,100}$")


def create_app(
    job_db_path: Optional[str] = None,
    data_output_root: Optional[str] = None,
    upload_dir: Optional[str] = None,
) -> FastAPI:
    """
    建立一份 FastAPI app 實例。

    刻意用工廠函式（而非只匯出一個模組層級的 `app`）讓 `tests/test_api_main.py`
    可以每個測試各自建立一份指向暫存目錄的 app + JobStore，彼此互不干擾、
    也不會污染真正的 `data/jobs.sqlite3`。真正部署（`uvicorn src.api.main:app`）
    時則使用檔案底部、依 `config/settings.py` 設定建立的預設 `app`。
    """
    job_db_path = job_db_path or API_CONFIG["job_db_path"]
    data_output_root = data_output_root or API_CONFIG["data_output_root"]
    upload_dir = Path(upload_dir or API_CONFIG["upload_dir"])

    import contextlib

    @contextlib.asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        import os
        if not os.environ.get("NO_BROWSER") and not os.environ.get("PYTEST_CURRENT_TEST"):
            import threading
            import time
            import webbrowser

            def _launch():
                time.sleep(1.0)
                host = API_CONFIG.get("host", "127.0.0.1")
                port = API_CONFIG.get("port", 8000)
                webbrowser.open(f"http://{host}:{port}")

            threading.Thread(target=_launch, daemon=True).start()
        yield

    app = FastAPI(title="OCR Subtitle Learning System", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_no_cache_header(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".js", ".css", ".html")):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.state.job_store = JobStore(job_db_path)
    app.state.data_output_root = data_output_root
    app.state.upload_dir = upload_dir
    app.state.ai_sessions = AISessions()

    def check_settings_origin(request: Request, write: bool = False):
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if origin and origin != expected:
            raise HTTPException(status_code=403, detail="請從專案網站內設定 AI。")
        if write and origin != expected:
            raise HTTPException(status_code=403, detail="請從專案網站內設定 AI。")
        # This project has no login system yet; do not expose credential setup over plain remote HTTP.
        if request.url.scheme != "https":
            host = request.url.hostname or ""
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            if not local:
                raise HTTPException(status_code=403, detail="遠端使用 AI 設定需要 HTTPS。")

    @app.get("/api/ai/settings")
    def get_ai_settings(request: Request, response: Response):
        check_settings_origin(request)
        response.headers["Cache-Control"] = "no-store"
        config = request.app.state.ai_sessions.get(request.cookies.get(COOKIE_NAME))
        return {"configured": bool(config), "model": config["model"] if config else DEFAULT_MODEL}

    @app.post("/api/ai/settings")
    def set_ai_settings(payload: AISettingsRequest, request: Request, response: Response):
        check_settings_origin(request, write=True)
        key = payload.api_key.get_secret_value().strip()
        if not 8 <= len(key) <= 512:
            raise HTTPException(status_code=400, detail="請填入有效的 Gemini API 金鑰。")
        try:
            test_connection(key, payload.model)
            token = request.app.state.ai_sessions.save(key, payload.model, request.cookies.get(COOKIE_NAME))
        except AIError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        response.set_cookie(COOKIE_NAME, token, httponly=True, secure=request.url.scheme == "https",
                            samesite="strict", max_age=8*3600)
        response.headers["Cache-Control"] = "no-store"
        return {"configured": True, "model": payload.model}

    @app.delete("/api/ai/settings")
    def clear_ai_settings(request: Request, response: Response):
        check_settings_origin(request, write=True)
        request.app.state.ai_sessions.clear(request.cookies.get(COOKIE_NAME))
        response.delete_cookie(COOKIE_NAME, httponly=True, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        return {"configured": False, "model": DEFAULT_MODEL}

    # ------------------------------------------------------------------
    # Phase 7：測試期間方便操作的功能區域（使用者決策紀錄，2026-07-26，見
    # PROJECT_NOTES.md 第 16 節）——成品完成後會要求整個移除。移除方式：
    # 拿掉這個路由 + `src/api/environment_check.py` 這個檔案即可，不影響
    # 其他任何路由。
    # ------------------------------------------------------------------
    @app.post("/api/youtube/import")
    def import_youtube(payload: YouTubeRequest, request: Request):
        try:
            video_id = parse_video_id(payload.url)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            fetched = fetch_korean_captions(video_id)
            job_id = uuid.uuid4().hex
            source = write_youtube_results(
                Path(request.app.state.data_output_root) / job_id,
                video_id, fetched, DICTIONARY_CONFIG["index_db_path"],
            )
        except YouTubeImportError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        store = request.app.state.job_store
        store.create_job(job_id, job_id, f"YouTube {video_id}", include_channel2=False)
        store.update_job(job_id, status="done", current_stage="youtube_subtitles")
        return {"job_id": job_id, "source": source}

    @app.get("/api/environment")
    async def get_environment(
        request: Request,
        dict_db: Optional[str] = Query(default=None),
        base_url: Optional[str] = Query(default=None),
    ):
        """一鍵確認所需環境是否齊全（只回報有/無 + 簡短細節，不要求使用者判讀技術訊息）。"""
        dict_db_path = dict_db or DICTIONARY_CONFIG.get("index_db_path")
        ollama_base_url = base_url or API_CONFIG["llm_base_url"]
        return {"items": check_environment(dict_db_path, ollama_base_url)}

    @app.post("/api/upload")
    async def upload_video(
        background_tasks: BackgroundTasks,
        request: Request,
        file: UploadFile = File(...),
        recognition_mode: Optional[str] = Form(None),
        recognition_seconds: float = Form(60.0, ge=0, le=14400, allow_inf_nan=False),
        recognition_fps: float = Form(1.0, ge=0.2, le=2, allow_inf_nan=False),
        whisper_model: str = Form('small'),
        include_channel1: bool = Form(True),
        channel1_engines: Optional[str] = Form(
            None, description="逗號分隔的子引擎清單，例如 'okt,mecab'；留空表示全部 4 個",
        ),
        include_channel2: bool = Form(True),
        include_channel3: bool = Form(True),
        denoise: bool = Form(True),
        sample_rate_fps: float = Form(1.0),
        max_frames: Optional[int] = Form(None, description="測試用：只取前 N 張影格，加快處理速度"),
        dict_db: Optional[str] = Form(None),
        model: Optional[str] = Form(None, description="LLM 模型名稱，留空使用 config 預設值"),
        base_url: Optional[str] = Form(None, description="Ollama 伺服器位址，留空使用 config 預設值"),
        min_confidence: Optional[float] = Form(None, description="OCR 最低信心值，留空使用 config 預設值"),
        dedup_max_gap_sec: Optional[float] = Form(None, description="去重時間間隔（秒），留空使用 config 預設值"),
        clahe_clip_limit: Optional[float] = Form(None, description="CLAHE 對比強化強度，留空使用 config 預設值"),
        denoise_strength: Optional[int] = Form(None, description="去雜訊強度，留空使用 config 預設值"),
    ):
        """
        接收影片檔案，儲存後立即觸發完整流程，回傳 job_id 供後續查詢。

        `include_channel1`/`channel1_engines`/`include_channel3`/`denoise`/
        `sample_rate_fps`/`max_frames`/`model`/`base_url`/`min_confidence`/
        `dedup_max_gap_sec`/`clahe_clip_limit`/`denoise_strength` 皆為 Phase 7
        新增的「測試期間方便操作」參數（見 PROJECT_NOTES.md 第 16 節），成品
        完成後使用者會要求移除或改為固定值。
        """
        # Some FastAPI versions interpret missing boolean form fields as unchecked.
        # Preserve this API's documented True defaults while respecting explicit false.
        submitted = await request.form()
        include_channel1 = include_channel1 if "include_channel1" in submitted else True
        include_channel2 = include_channel2 if "include_channel2" in submitted else True
        include_channel3 = include_channel3 if "include_channel3" in submitted else True
        denoise = denoise if "denoise" in submitted else True
        job_store: JobStore = request.app.state.job_store
        data_output_root = request.app.state.data_output_root
        upload_dir: Path = request.app.state.upload_dir

        ext = Path(file.filename or "").suffix.lower()
        allowed = API_CONFIG["allowed_video_extensions"]
        if ext not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"不支援的影片格式：{ext or '(無副檔名)'}（可用：{allowed}）",
            )

        media_config = None
        if recognition_mode is not None:
            if recognition_mode not in MODES:
                raise HTTPException(400, detail='請選擇有效的影片辨識方式。')
            if whisper_model not in {'tiny', 'base', 'small'}:
                raise HTTPException(400, detail='本機語音模型僅支援 tiny、base、small。')
            if recognition_mode.startswith('gemini_'):
                media_config = request.app.state.ai_sessions.get(request.cookies.get(COOKIE_NAME))
                if not media_config:
                    raise HTTPException(400, detail='請先在 Gemini 設定填入自己的 API，或選擇不需 API 的本機模式。')
        job_id = uuid.uuid4().hex
        upload_dir.mkdir(parents=True, exist_ok=True)
        saved_path = upload_dir / f"{job_id}{ext}"
        with open(saved_path, "wb") as out:
            shutil.copyfileobj(file.file, out)

        job_store.create_job(
            job_id=job_id, video_id=job_id,
            original_filename=file.filename or "", include_channel2=include_channel2,
        )

        if recognition_mode is not None:
            background_tasks.add_task(
                run_media_job, job_id=job_id, video_path=str(saved_path), job_store=job_store,
                data_output_root=data_output_root,
                dictionary_path=DICTIONARY_CONFIG.get('index_db_path'),
                mode=recognition_mode, config=media_config,
                limit=recognition_seconds, fps=recognition_fps, model_size=whisper_model,
            )
            return {'job_id': job_id, 'video_id': job_id, 'status': 'queued'}

        engines_list = (
            [e.strip() for e in channel1_engines.split(",") if e.strip()]
            if channel1_engines else None
        )
        config_overrides = {
            "min_confidence": min_confidence,
            "dedup_max_gap_sec": dedup_max_gap_sec,
            "clahe_clip_limit": clahe_clip_limit,
            "denoise_strength": denoise_strength,
        }

        background_tasks.add_task(
            run_full_pipeline,
            job_id=job_id,
            video_path=str(saved_path),
            job_store=job_store,
            include_channel1=include_channel1,
            channel1_engines=engines_list,
            include_channel2=include_channel2,
            include_channel3=include_channel3,
            denoise=denoise,
            sample_rate_fps=sample_rate_fps,
            max_frames=max_frames,
            dict_db=dict_db or DICTIONARY_CONFIG.get("index_db_path"),
            model=model or API_CONFIG["llm_model"],
            base_url=base_url or API_CONFIG["llm_base_url"],
            data_output_root=data_output_root,
            config_overrides=config_overrides,
        )

        return {"job_id": job_id, "video_id": job_id, "status": "queued"}

    @app.get("/api/jobs/{job_id}/status")
    async def get_job_status(job_id: str, request: Request):
        """查詢處理進度：整體狀態（queued/running/done/failed）+ 各階段的成功/失敗/耗時明細。"""
        job_store: JobStore = request.app.state.job_store
        job = job_store.get_job_with_stages(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"找不到工作 job_id={job_id}")

        source_path = Path(request.app.state.data_output_root) / job["video_id"] / "youtube.json"
        if source_path.exists():
            job["source"] = json.loads(source_path.read_text(encoding="utf-8"))["source"]
        local_path = Path(request.app.state.data_output_root) / job['video_id'] / 'local.json'
        if local_path.exists():
            job['recognition'] = json.loads(local_path.read_text(encoding='utf-8'))['source']
        ext=Path(job.get('original_filename') or '').suffix.lower()
        if ext in API_CONFIG['allowed_video_extensions'] and (Path(request.app.state.upload_dir)/(job_id+ext)).is_file():
            job['video_url']=f'/api/jobs/{job_id}/video'
            job['playback_ready']=(Path(request.app.state.data_output_root)/job['video_id']/'playback.mp4').is_file()
        job["available_channels"] = list_available_channels(
            request.app.state.data_output_root, job["video_id"],
        )
        return job

    @app.get("/api/jobs/{job_id}/logs")
    async def get_job_logs(job_id: str, request: Request, after_id: int = Query(default=0, ge=0)):
        """取得該工作之即時日誌清單，支援增量查詢（after_id）。"""
        job_store: JobStore = request.app.state.job_store
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"找不到工作 job_id={job_id}")
        logs = job_store.list_logs(job_id, after_id=after_id)
        return {"job_id": job_id, "logs": logs, "status": job["status"]}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request):
        """中斷目前執行中的影片處理工作（成品功能）。"""
        job_store: JobStore = request.app.state.job_store
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"找不到工作 job_id={job_id}")
        from src.api.pipeline_runner import request_job_cancellation
        request_job_cancellation(job_id)
        job_store.update_job(job_id, status="cancelled", error="使用者手動請求中斷")
        job_store.record_log(job_id, "[中斷請求] 使用者透過介面觸發中斷運算")
        return {"job_id": job_id, "status": "cancelling", "message": "已發送中斷請求"}

    @app.get("/api/jobs/{job_id}/results")
    async def get_job_results(
        job_id: str,
        request: Request,
        channel: str = Query(
            default=API_CONFIG["default_results_channel"],
            description=f"六選一：{VALID_CHANNELS}；預設 {API_CONFIG['default_results_channel']}"
                        "（最終保留哪個通道尚未決定，見 PROJECT_NOTES.md）",
        ),
    ):
        """取得處理完成後的字幕與翻譯結果（預設 channel3，可用 ?channel= 指定其他 5 組結果之一）。"""
        job_store: JobStore = request.app.state.job_store
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"找不到工作 job_id={job_id}")

        if channel not in VALID_CHANNELS:
            raise HTTPException(status_code=400, detail=f"不支援的 channel：{channel}（可用：{VALID_CHANNELS}）")

        try:
            return read_channel_results(request.app.state.data_output_root, job["video_id"], channel)
        except ChannelResultsNotFound as e:
            raise HTTPException(
                status_code=404,
                detail=f"{e}（目前工作狀態：{job['status']}，目前階段：{job['current_stage']}）",
            ) from e

    @app.post("/api/jobs/{job_id}/word-context")
    def get_word_context(job_id: str, payload: WordContextRequest, request: Request):
        job = request.app.state.job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="找不到這個工作")
        if payload.channel not in VALID_CHANNELS:
            raise HTTPException(status_code=400, detail="不支援的辨識通道")
        try:
            return resolve_context(Path(request.app.state.data_output_root), job["video_id"],
                                   payload.word, payload.timestamp, payload.channel,
                                   ai_config=request.app.state.ai_sessions.get(request.cookies.get(COOKIE_NAME)))
        except (ValueError, ChannelResultsNotFound) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/jobs/{job_id}/subtitles")
    def get_job_subtitles(job_id: str, request: Request):
        job = request.app.state.job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="找不到這個工作")
        return {"subtitles": read_subtitles(request.app.state.data_output_root, job["video_id"])}

    @app.post("/api/jobs/{job_id}/prepare-video")
    def prepare_job_video(job_id: str, request: Request):
        job=request.app.state.job_store.get_job(job_id)
        if job is None:
            raise HTTPException(404, detail='找不到這個工作')
        ext=Path(job.get('original_filename') or '').suffix.lower()
        folder=Path(request.app.state.upload_dir).resolve()
        path=(folder/(job_id+ext)).resolve()
        if ext not in API_CONFIG['allowed_video_extensions'] or path.parent!=folder or not path.is_file():
            raise HTTPException(404, detail='找不到原影片，請重新選取')
        try:
            prepare_playback(path,Path(request.app.state.data_output_root)/job['video_id'])
        except MediaError as error:
            raise HTTPException(400, detail=str(error)) from error
        return {'video_url':f'/api/jobs/{job_id}/video?compatible=1'}

    @app.get("/api/jobs/{job_id}/video")
    def get_job_video(job_id: str, request: Request):
        job = request.app.state.job_store.get_job(job_id)
        if job is None:
            raise HTTPException(404, detail='找不到這個工作')
        ext = Path(job.get('original_filename') or '').suffix.lower()
        if ext not in API_CONFIG['allowed_video_extensions']:
            raise HTTPException(404, detail='這個工作沒有本機影片')
        folder = Path(request.app.state.upload_dir).resolve()
        path = (folder / (job_id + ext)).resolve()
        if path.parent != folder or not path.is_file():
            raise HTTPException(404, detail='原影片已移除，請重新選取影片')
        proxy=Path(request.app.state.data_output_root)/job['video_id']/'playback.mp4'
        if proxy.exists():path=proxy
        return FileResponse(path, media_type='video/mp4' if path.suffix=='.mp4' else None,
                            headers={'Cache-Control':'private, no-cache'})

    @app.get("/api/jobs/{job_id}/frame")
    async def get_job_frame(job_id: str, request: Request, timestamp: float = Query(...)):
        """
        取得該工作對應影片、時間戳記最接近 `timestamp` 秒的真實 OCR 影格截圖
        （Phase 7 新增，見 PROJECT_NOTES.md 第 16 節）。前端可用
        `GET /api/jobs/{job_id}/results` 回傳的每筆紀錄裡的 `timestamp_start`
        呼叫本端點，在單字旁顯示對應畫面。
        """
        job_store: JobStore = request.app.state.job_store
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"找不到工作 job_id={job_id}")

        frame_path = resolve_frame_path(request.app.state.data_output_root, job["video_id"], timestamp)
        if frame_path is None:
            raise HTTPException(
                status_code=404,
                detail=f"找不到影片 {job['video_id']} 在時間戳記 {timestamp}s 附近的截圖（frames/manifest.csv 不存在或無影格）",
            )
        return FileResponse(frame_path, media_type="image/jpeg")

    @app.post("/api/jobs/{job_id}/export")
    async def export_job(
        job_id: str,
        request: Request,
        format: str = Query(default="csv"),
        channel: str = Query(default=API_CONFIG["default_results_channel"]),
    ):
        """匯出結果檔案。決策紀錄：只支援 CSV——Anki 本身即透過 CSV 匯入，不另外接 anki_exporter.py。"""
        if format != "csv":
            raise HTTPException(
                status_code=400,
                detail="目前只支援 format=csv。Anki 可直接匯入 CSV 檔案，不需要額外的 anki 格式（見決策紀錄）。",
            )

        job_store: JobStore = request.app.state.job_store
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"找不到工作 job_id={job_id}")

        if channel not in VALID_CHANNELS:
            raise HTTPException(status_code=400, detail=f"不支援的 channel：{channel}（可用：{VALID_CHANNELS}）")

        try:
            path = export_csv_path(request.app.state.data_output_root, job["video_id"], channel)
        except ChannelResultsNotFound as e:
            raise HTTPException(
                status_code=404,
                detail=f"{e}（目前工作狀態：{job['status']}，目前階段：{job['current_stage']}）",
            ) from e

        return FileResponse(
            path, media_type="text/csv", filename=f"{job['video_id']}_{channel}_export.csv",
        )

    @app.post("/api/system/shutdown")
    async def shutdown_system(request: Request, force: bool = Query(default=False)):
        """關閉系統服務並結束後端程序（成品功能）。"""
        import os
        import threading
        import time

        def _terminate():
            time.sleep(0.5)
            if force:
                os._exit(0)
            else:
                try:
                    import signal
                    os.kill(os.getpid(), signal.SIGTERM)
                except Exception:
                    pass
                time.sleep(1.0)
                os._exit(0)

        threading.Thread(target=_terminate, daemon=True).start()
        return {"status": "shutting_down", "message": "系統伺服器正在關閉，後端程序即將結束。"}

    # 掛載 Phase 7 前端頁面（放在所有 /api/* 路由「之後」註冊，確保 /api/*
    # 一律先被上面的具名路由比對到，不會被這裡的靜態檔案掛載攔截）。
    # 開發環境沒有 frontend/ 目錄時（理論上不會發生）就跳過，不讓整個 app
    # 建立失敗。
    if _FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

    return app


try:
    app = create_app()
except Exception:  # noqa: BLE001
    # 已知的沙盒限制（與 scripts/build_dictionary_index.py 建索引時遇到的
    # SQLite "disk I/O error" 同一原因：沙盒的 FUSE 掛載目錄不支援 SQLite
    # 一般的寫入/journal 機制，見 PROJECT_NOTES.md）。真正部署在使用者本機
    # 一般檔案系統上不會有此問題，這裡的 try/except 只是避免此限制讓整個
    # 模組在沙盒測試環境下完全無法 import；測試一律改用 create_app() 搭配
    # 暫存目錄路徑（見 tests/test_api_main.py），不依賴這個模組層級的 app。
    #
    # 2026-07-26 修正：這裡原本完全不印出例外內容，導致使用者本機真的遇到
    # 「缺少 python-multipart」問題時，只看到 uvicorn 端「TypeError: 'NoneType'
    # object is not callable」這種無法debug的錯誤，看不出真正原因。改成先把
    # 完整 traceback 印到 stderr 再退回 app=None——沙盒的已知限制仍然不會讓
    # 模組整個匯入失敗，但使用者本機若因為其他原因（例如套件未安裝）真的
    # 建立失敗，終端機會直接看到清楚的錯誤訊息，而不是被完全吃掉。
    import traceback
    print("=" * 60, file=sys.stderr)
    print("[錯誤] create_app() 建立失敗，以下為完整錯誤內容：", file=sys.stderr)
    traceback.print_exc()
    print("=" * 60, file=sys.stderr)
    app = None  # type: ignore[assignment]

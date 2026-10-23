"""
Phase 6：讀取通道結果 (Results Reader)
=========================================
把 Channel 1/2/3 已寫到磁碟的 `data/output/<video_id>/<channel>/export.csv`
與 `data/output/<video_id>/stats/<channel>_report.csv` 讀成 API 可直接
回傳的 JSON 結構，供 `GET /api/jobs/{job_id}/results` 使用。

決策紀錄（2026-07-26，使用者確認）：六組通道結果
（channel1_okt/komoran/kkma/mecab、channel2、channel3）不強制只回傳一組，
而是「預設回傳一組（channel3），其餘透過 `?channel=` 查詢參數指定」——
因為「最終保留哪個通道」本來就明確延後到專案尾聲才決定（見
PHASE5_MULTI_CHANNEL_PLAN.md／PROJECT_NOTES.md），API 不該替使用者預先
做這個決定。

Phase 7 擴充（2026-07-26，使用者決策紀錄，見 PROJECT_NOTES.md 第 16 節）：
前端需要在每個單字旁顯示「對應時間戳記的真實 OCR 影格」。既有的
`export.csv` 只在 `備註` 欄位的自由文字裡塞了時間戳記（例如
`"引擎=mecab｜詞性=NNG｜同形詞0‧詞義1｜字幕 15.0s-15.0s"`，見
`scripts/run_channel1.py`/`run_channel2.py`/`run_channel3.py`），沒有
結構化欄位。刻意選擇「保留既有 CSV 格式不變、在讀取時用正規表達式解析
出結構化欄位」而非「改寫三個 channel runner 的匯出格式」，理由：
  1) 已經有 10 支真實影片、6 個通道的正式資料是用舊格式產生的，改格式
     會讓這些既有真實資料全部作廢，需要重新產生。
  2) 三個 channel runner 的 `備註` 組字邏輯本身沒有問題，只是格式對
     「程式讀取」不友善，屬於「API 讀取端」的責任，不是「產生端」的
     責任，改在這裡處理更符合模組化原則。
"""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from src.export.csv_exporter import EXPORT_COLUMNS

VALID_CHANNELS = [
    "channel1_okt", "channel1_komoran", "channel1_kkma", "channel1_mecab",
    "channel2", "channel3",
]

# 比對「字幕 15.0s-15.0s」這種格式（見模組說明），三個 channel runner 的
# 備註組字方式雖然前綴不同（引擎=.../通道=...），但這個片段格式一致。
_TIMESTAMP_RE = re.compile(r"字幕\s+([\d.]+)s-([\d.]+)s")


def _extract_timestamp_range(note: str) -> tuple[Optional[float], Optional[float]]:
    """從 `備註` 欄位的自由文字中解析出 (timestamp_start, timestamp_end)，解析不到則回傳 (None, None)。"""
    if not isinstance(note, str):
        return None, None
    match = _TIMESTAMP_RE.search(note)
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


class ChannelResultsNotFound(Exception):
    """指定的 video_id/channel 尚未有 export.csv（可能該階段還沒跑、跑失敗，或該 job 未 include_channel2）。"""


def _clean_records(df: pd.DataFrame) -> list[dict]:
    """把 pandas 讀出的 NaN（空字串欄位）轉成 ""，確保 JSON 序列化不會出現 NaN。"""
    records = df.where(df.notna(), "").to_dict(orient="records")
    # 保險：某些數值型欄位的 NaN 在極少數情況下 .where 後仍可能殘留 float('nan')。
    for row in records:
        for k, v in row.items():
            if isinstance(v, float) and math.isnan(v):
                row[k] = ""
    return records


def _attach_structured_timestamps(records: list[dict]) -> list[dict]:
    """在每筆匯出紀錄上加上從 `備註` 解析出的 timestamp_start/timestamp_end（見模組說明）。"""
    for row in records:
        start, end = _extract_timestamp_range(row.get("備註", ""))
        row["timestamp_start"] = start
        row["timestamp_end"] = end
    return records


def list_available_channels(data_output_root: str | Path, video_id: str) -> list[str]:
    """回傳這支影片目前有哪些通道已產生 export.csv（供 status/錯誤訊息參考）。"""
    root = Path(data_output_root) / video_id
    return [ch for ch in VALID_CHANNELS if (root / ch / "export.csv").exists()]


def export_csv_path(data_output_root: str | Path, video_id: str, channel: str) -> Path:
    """回傳指定通道 export.csv 的路徑，檔案不存在時丟出 `ChannelResultsNotFound`。"""
    if channel not in VALID_CHANNELS:
        raise ValueError(f"不支援的 channel：{channel}（可用：{VALID_CHANNELS}）")
    path = Path(data_output_root) / video_id / channel / "export.csv"
    if not path.exists():
        raise ChannelResultsNotFound(
            f"影片 {video_id} 的 {channel} 尚無結果（{path} 不存在）"
            f"——可能該通道尚未執行、執行失敗，或該工作未包含此通道。"
        )
    return path


def read_channel_results(
    data_output_root: str | Path, video_id: str, channel: str,
) -> dict:
    """
    讀取單一通道的匯出結果與統計報告。

    Returns:
        {
            "video_id": str, "channel": str, "record_count": int,
            "records": [
                {
                    "原文":..., "中文":..., "詞性":..., "例句":..., "備註":...,
                    "timestamp_start": float | None, "timestamp_end": float | None,
                }, ...
            ],
            "stats": dict | None,  # RunReport 攤平後的欄位；沒有統計檔時為 None
        }

    `timestamp_start`/`timestamp_end`（Phase 7 新增）是從 `備註` 欄位解析
    出的結構化秒數，供前端配合 `GET /api/jobs/{job_id}/frame` 顯示對應的
    真實 OCR 影格；解析不到時皆為 `None`（理論上不會發生，因為三個 channel
    runner 一定會寫入這段文字，見模組說明）。

    「0 筆結果」的防禦性處理（2026-07-27，見 PROJECT_NOTES.md 第 17 節）：
    某支影片若完全沒有辨識出任何文字（例如 OCR 編碼格式不支援、擷取到
    0 張影格），`export.csv` 會是「只有標題列、0 筆資料」的合法檔案（見
    `src/export/csv_exporter.py` 的修復），這裡回傳 `record_count=0` 屬於
    正常、可預期的結果，不是錯誤。但為了保護「使用者在這個修復生效前就
    已經產生的舊檔案」（那些檔案可能連標題列都沒有），這裡額外用
    `pd.errors.EmptyDataError` 的 except 分支，把真正完全空白、pandas 讀
    不出任何欄位的檔案也視為「0 筆結果」而不是讓 API 直接 500。
    """
    export_path = export_csv_path(data_output_root, video_id, channel)

    try:
        export_df = pd.read_csv(export_path, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        export_df = pd.DataFrame(columns=EXPORT_COLUMNS)
    records = _attach_structured_timestamps(_clean_records(export_df))

    stats: Optional[dict] = None
    stats_path = Path(data_output_root) / video_id / "stats" / f"{channel}_report.csv"
    if stats_path.exists():
        stats_df = pd.read_csv(stats_path, encoding="utf-8")
        if not stats_df.empty:
            stats = _clean_records(stats_df)[0]

    return {
        "video_id": video_id,
        "channel": channel,
        "record_count": len(records),
        "records": records,
        "stats": stats,
    }


def resolve_frame_path(data_output_root: str | Path, video_id: str, timestamp: float) -> Optional[Path]:
    """
    找出這支影片在 `frames/manifest.csv`（Phase 1 輸出的原始截圖清單，
    未經過 Phase 2 前處理）裡，時間戳記最接近 `timestamp` 的那一張影格，
    回傳其檔案路徑；找不到 manifest 或裡面沒有任何影格時回傳 `None`。

    用途：`GET /api/jobs/{job_id}/frame?timestamp=...`（見 `main.py`）用
    這個函式決定要回傳哪一張真實 OCR 影格圖片給前端顯示。刻意使用 Phase 1
    的原始截圖（而非 Phase 2 前處理後的灰階+CLAHE 影像），因為使用者要看
    的是「這個單字出現時畫面長什麼樣子」，原始彩色截圖比前處理後的版本
    更容易辨識。
    """
    manifest_path = Path(data_output_root) / video_id / "frames" / "manifest.csv"
    if not manifest_path.exists():
        return None

    best_path: Optional[Path] = None
    best_diff: Optional[float] = None
    with open(manifest_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            diff = abs(float(row["timestamp_sec"]) - timestamp)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_path = manifest_path.parent / row["filename"]

    if best_path is not None and not best_path.exists():
        return None
    return best_path


def read_subtitles(data_output_root: str | Path, video_id: str) -> list[dict]:
    """Read actual OCR sentences; exported dictionary examples are not subtitles."""
    from src.dedup.deduplicator import deduplicate
    from src.nlp.text_cleaner import clean_ocr_text
    from src.ocr.results_io import load_ocr_results

    path = Path(data_output_root) / video_id / "ocr_on" / "ocr_results.csv"
    if not path.exists():
        return []
    cleaned = []
    for result in load_ocr_results(str(path)):
        text = clean_ocr_text(result.text)
        if text:
            cleaned.append(result.model_copy(update={"text": text}))
    return [
        {"text": subtitle.text, "start": subtitle.start_time, "end": subtitle.end_time}
        for subtitle in deduplicate(cleaned)
    ]

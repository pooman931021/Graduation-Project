/* Explicit recognition choices: no automatic paid fallback. */
window.LocalMedia = (() => {
  const $=id=>document.getElementById(id);
  const labels={local_audio:'本機語音 · 不需 API',local_ocr:'本機 OCR · 不需 API',gemini_audio:'Gemini 語音',gemini_ocr:'Gemini OCR'};
  let busy=false, restoredJob=null;
  function update() {
    const mode=$('recognition-mode').value, cloud=mode.startsWith('gemini_'), audio=mode.endsWith('audio');
    $('whisper-options').hidden=mode!=='local_audio';
    $('ocr-options').hidden=audio;
    $('recognition-notice').textContent=cloud
      ? `${audio?'音軌':'畫面下半部截圖'}會傳送至 Google Gemini，使用你設定的 API 額度。沒有設定 API 時不會開始，也不會自動切換服務。`
      : `${audio?'用 Whisper 聽影片中的韓文':'用 EasyOCR 讀取畫面下半部的字幕'}，在這台電腦運算，不需要 API。首次需連網下載模型；下載完成後可離線辨識，CPU 處理可能較久。${audio?" 語音模式仍在調校，請核對原音；畫面上的字卡不一定是實際說話內容。":""}`;
    $('media-settings-open').hidden=!cloud;
    $('recognition-settings').querySelectorAll('select,button').forEach(node=>{node.disabled=busy;});
  }
  $('recognition-mode').addEventListener('change',update);
  $('media-settings-open').addEventListener('click',()=>$('ai-settings-btn').click());
  update();
  return {
    restoreResult(id, result) {
      if (restoredJob===id || !result) return;
      restoredJob=id;
      if (labels[result.mode]) $('recognition-mode').value=result.mode;
      if (result.model_size && ['tiny','base','small','turbo'].includes(result.model_size)) $('whisper-model').value=result.model_size;
      update();
    },
    options() {
      const mode=$('recognition-mode').value;
      return {recognition_mode:mode,recognition_seconds:$('recognition-duration').value,
              recognition_fps:$('recognition-fps').value,whisper_model:$('whisper-model').value};
    },
    label:mode=>labels[mode]||mode,
    setBusy(value){busy=value;update();},
  };
})();

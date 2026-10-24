/* Optional credentials are server-side and isolated by an HttpOnly session cookie. */
window.AISettings = (() => {
  const $=id=>document.getElementById(id);
  let configured=false;
  function apply(settings) {
    configured=!!settings.configured;
    $('ai-settings-btn').textContent=configured?'Gemini：已啟用':'Gemini：未啟用';
    $('ai-model').value=settings.model || 'gemini-3.5-flash-lite';
    $('ai-disable-btn').disabled=!configured;
  }
  async function refresh() {
    try {
      const response=await fetch('/api/ai/settings',{cache:'no-store'});
      if (response.ok) apply(await response.json());
    } catch (_) { /* Dictionary mode remains available when settings cannot be read. */ }
  }
  function busy(value) {
    $('ai-save-btn').disabled=value; $('ai-disable-btn').disabled=value || !configured;
    $('ai-api-key').disabled=value; $('ai-model').disabled=value;
  }
  $('ai-settings-btn').addEventListener('click',()=>{
    $('ai-api-key').value='';
    $('ai-settings-status').textContent=configured?'已啟用。若想更換金鑰或模型，請重新輸入並測試。':'目前只使用字幕和字典，AI 尚未啟用。';
    $('ai-settings-dialog').showModal(); refresh();
  });
  $('ai-settings-close').addEventListener('click',()=>{
    $('ai-api-key').value=''; $('ai-settings-dialog').close();
  });
  $('ai-settings-dialog').addEventListener('close',()=>{ $('ai-api-key').value=''; });
  $('ai-settings-form').addEventListener('submit',async event=>{
    event.preventDefault();
    const key=$('ai-api-key').value.trim();
    if (!key) { $('ai-settings-status').textContent='請填入你的 API 金鑰；不填也可以繼續使用字典。'; return; }
    const model=$('ai-model').value.trim();
    $('ai-api-key').value=''; busy(true);
    $('ai-settings-status').textContent='正在向 Gemini 測試連線，這會使用少量 API 額度…';
    try {
      const response=await fetch('/api/ai/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key,model})});
      const result=await response.json();
      if (!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'設定未成功，請檢查金鑰與模型。');
      apply(result); $('ai-settings-status').textContent='連線成功，已為這次使用啟用 AI。重新點單字即可看語境解釋。';
      document.dispatchEvent(new Event('ai-settings-changed'));
    } catch (error) { $('ai-settings-status').textContent=error.message || '連線未成功，字典功能仍可使用。'; }
    finally { busy(false); }
  });
  $('ai-disable-btn').addEventListener('click',async()=>{
    busy(true);
    try {
      const response=await fetch('/api/ai/settings',{method:'DELETE'});
      if (!response.ok) throw new Error('暫時無法清除設定，請稍後重試。');
      apply(await response.json()); $('ai-api-key').value='';
      $('ai-settings-status').textContent='已清除金鑰，回到字幕與字典模式。';
      document.dispatchEvent(new Event('ai-settings-changed'));
    } catch(error) { $('ai-settings-status').textContent=error.message; }
    finally { busy(false); }
  });
  refresh();
  return {enabled:()=>configured};
})();

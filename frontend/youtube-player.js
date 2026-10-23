/* YouTube's official IFrame API handles playback; captions are read by the backend. */
window.YouTubePlayback = (() => {
  let player = null, ready = false, videoId = null, apiPromise = null, timer = null, pendingSeek = null;
  const notice = text => { document.getElementById('player-notice').textContent = text; };
  function api() {
    if (window.YT?.Player) return Promise.resolve();
    if (apiPromise) return apiPromise;
    apiPromise = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('YouTube 播放器載入逾時，請檢查網路後重試。')), 20000);
      const previous = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => { clearTimeout(timeout); previous?.(); resolve(); };
      const script = document.createElement('script'); script.src = 'https://www.youtube.com/iframe_api';
      script.onerror = () => { clearTimeout(timeout); reject(new Error('無法載入 YouTube 播放器。')); };
      document.head.append(script);
    }).catch(error => { apiPromise = null; throw error; });
    return apiPromise;
  }
  async function load(id) {
    const wrap = document.getElementById('youtube-player-wrap');
    document.getElementById('learning-video').pause();
    document.getElementById('learning-video').hidden = true;
    document.getElementById('video-placeholder').hidden = true;
    wrap.hidden = false;
    await api();
    if (player && videoId === id) return;
    videoId = id; ready = false; pendingSeek = null;
    if (player) { player.cueVideoById(id); ready = true; return; }
    player = new YT.Player('youtube-player', {
      videoId: id, width: '100%', height: '100%',
      playerVars: {playsinline:1, origin:window.location.origin, rel:0},
      events: {
        onReady: () => {
          ready = true;
          const frame = player.getIframe(); frame.title = 'YouTube 影片播放器';
          frame.setAttribute('referrerpolicy','strict-origin-when-cross-origin');
          if (pendingSeek !== null) { player.seekTo(pendingSeek, true); pendingSeek = null; }
          notice('YouTube 影片已就緒。點字幕時間跳轉，點單字查看釋義。');
        },
        onError: event => notice(`YouTube 無法播放（${event.data}），可能是影片限制嵌入或瀏覽器阻擋。字幕仍可查看，可用原始連結觀看。`),
      }
    });
    if (timer) clearInterval(timer);
    timer = setInterval(() => { if (ready && !wrap.hidden) document.dispatchEvent(new Event('youtube-timeupdate')); }, 300);
  }
  function seek(time) {
    if (!ready) { pendingSeek = time; notice('播放器準備中，完成後會跳至選取片段。'); return; }
    const duration = player.getDuration();
    if (duration > 0 && time >= duration) { notice('這筆字幕時間超出影片長度。'); return; }
    player.seekTo(time, true);
  }
  function hide() {
    if (ready) player.pauseVideo();
    document.getElementById('youtube-player-wrap').hidden = true;
  }
  return {load,seek,hide,pause:()=>{ if (ready) player.pauseVideo(); },currentTime:()=>ready ? player.getCurrentTime() : 0};
})();

/* Learning workspace: actual OCR sentences + channel dictionary records. */
window.Learning = (() => {
  const $ = id => document.getElementById(id);
  const video = $('learning-video');
  const popover = $('word-popover');
  const storageKey = 'korean-learning-library-v1';
  let videoUrl = null, selectedFile = null, resultFile = null, jobId = null;
  let subtitles = [], records = [], selected = null, anchor = null, active = '';
  let revision = 0;
  let source = null, currentMode = 'youtube', serverVideoJob = null, serverVideoName = '';
  const contextCache = new Map();
  let captionIndex = 0, browsingCaptions = false, captionAnimation = null;
  let library = [];
  try {
    const data = JSON.parse(localStorage.getItem(storageKey) || '[]');
    if (Array.isArray(data)) library = data.filter(item => item && typeof item.word === 'string' && Array.isArray(item.meanings));
  } catch (_) { $('library-status').textContent = '無法讀取收藏，仍可使用本次瀏覽的單字庫。'; }
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function button(text, action, className) {
    const node = element('button', text, className); node.type = 'button';
    node.addEventListener('click', action); return node;
  }
  function time(value) {
    if (!Number.isFinite(value)) return '無時間';
    const sec = Math.max(0, Math.floor(value));
    return `${Math.floor(sec / 60).toString().padStart(2, '0')}:${(sec % 60).toString().padStart(2, '0')}`;
  }
  function identity(file) { return file ? `${file.name}|${file.size}|${file.lastModified}` : null; }
  function close() {
    popover.hidden = true;
    if (anchor) anchor.setAttribute('aria-expanded', 'false');
  }
  function seek(seconds, fileIdentity = resultFile) {
    close();
    if (fileIdentity?.startsWith('youtube:')) {
      if (fileIdentity !== resultFile || currentMode !== 'youtube' || !source) {
        $('player-notice').textContent = '請先載入這筆收藏對應的 YouTube 影片。'; return;
      }
      if (!Number.isFinite(seconds)) return;
      window.YouTubePlayback.seek(seconds); return;
    }
    const originalSelected = selectedFile && (!fileIdentity || identity(selectedFile) === fileIdentity);
    const originalUploaded = serverVideoJob === jobId && fileIdentity === resultFile;
    if (!originalSelected && !originalUploaded) {
      $('player-notice').textContent = '請先重新選擇這筆結果對應的原影片，再跳轉片段。'; return;
    }
    if (video.readyState < 1 || !Number.isFinite(seconds) || seconds < 0 || seconds >= video.duration) {
      $('player-notice').textContent = '影片尚未就緒，或這筆字幕沒有可跳轉的時間。'; return;
    }
    video.currentTime = seconds;
    $('player-notice').textContent = `已跳至 ${time(seconds)}，可按播放繼續觀看。`;
  }
  function renderLibrary() {
    const list = $('library-list'); list.replaceChildren();
    $('library-count').textContent = String(library.length);
    $('library-export-btn').disabled = library.length === 0;
    if (!library.length) { list.append(element('p', '尚未收藏單字。點字幕中的單字，再按「加入單字庫」。', 'library-empty')); return; }
    library.forEach(item => {
      const card = element('article', undefined, 'saved-word');
      card.append(element('h3', item.word));
      const primary = item.meanings[0];
      card.append(element('p', primary?.translation || '本句詞義尚待確認'));
      if (item.sentence) card.append(element('p',item.sentence,'help'));
      if (item.sentenceTranslation) card.append(element('p',item.sentenceTranslation,'help'));
      if (item.meanings.length > 1) {
        const details=element('details');details.append(element('summary','其他收藏義項'));
        item.meanings.slice(1).forEach(meaning=>details.append(element('p',meaning.translation)));
        card.append(details);
      }
      if (item.fileName) card.append(element('p', item.fileName, 'help'));
      const actions = element('div', undefined, 'section-heading');
      const jump = button(`↗ ${time(item.start)} 回看片段`, () => seek(item.start, item.fileIdentity));
      jump.disabled = !Number.isFinite(item.start);
      actions.append(jump, button('移除', () => {
        library = library.filter(other => other.key !== item.key); persist(); renderLibrary(); updateSave();
      }));
      card.append(actions); list.append(card);
    });
  }
  function persist() {
    try { localStorage.setItem(storageKey, JSON.stringify(library)); $('library-status').textContent = '已保存在這個瀏覽器。'; }
    catch (_) { $('library-status').textContent = '瀏覽器無法儲存，這次收藏只會暫時保留；請匯出 CSV 備份。'; }
  }
  function updateSave() {
    const saved = selected && library.some(item => item.key === selected.key);
    $('save-word-btn').textContent = saved ? '✓ 已加入單字庫' : '加入單字庫';
    $('save-word-btn').disabled = !!saved || !!selected?.loading;
  }
  function positionPopover() {
    if (popover.hidden || !anchor) return;
    const rect = anchor.getBoundingClientRect();
    const width = popover.offsetWidth, height = popover.offsetHeight;
    popover.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - width - 12))}px`;
    const below = rect.bottom + 9;
    popover.style.top = `${Math.max(12, below + height < innerHeight - 12 ? below : rect.top - height - 9)}px`;
  }
  function showWord(node, word, matches, line) {
    if (currentMode === 'youtube') window.YouTubePlayback.pause(); else video.pause();
    close(); anchor = node; node.setAttribute('aria-expanded', 'true');
    const meanings = [...new Map(matches.map(record => [JSON.stringify([record.中文, record.詞性, record.例句]), {
      translation: record.中文 || '', pos: record.詞性 || '', example: record.例句 || ''
    }])).values()];
    selected = { key: JSON.stringify([resultFile || jobId, word, line.start]), word, meanings,
      start: line.start, jobId, fileIdentity: resultFile,
      fileName: source ? `YouTube ${source.youtube_id}` : selectedFile?.name || serverVideoName || '', sentence: line.text };
    $('popover-word').textContent = word;
    $('popover-search').href = 'https://www.google.com/search?q=' + encodeURIComponent(word + ' 뜻 신조어');
    selected.allMeanings = meanings;
    selected.meanings = [];
    selected.loading = true;
    const selection = selected;
    renderWordContext({sentence:line.text, other_meanings:meanings}, true);
    updateSave(); popover.hidden = false; positionPopover();
    const channel = $('channel-select').value || 'channel1_mecab';
    const cacheKey = JSON.stringify([jobId,channel,word,line.start]);
    const request = contextCache.get(cacheKey) || fetch(`/api/jobs/${encodeURIComponent(jobId)}/word-context`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({word,timestamp:line.start,channel}),
    }).then(async response => {
      if (!response.ok) throw new Error('context unavailable');
      const result = await response.json(); contextCache.set(cacheKey, Promise.resolve(result)); return result;
    });
    contextCache.set(cacheKey, request);
    request.then(result => {
      if (selected !== selection) return;
      selection.loading = false;
      selection.meanings = result.primary_meaning ? [result.primary_meaning] : [];
      selection.allMeanings = [...selection.meanings,...(result.other_meanings || [])];
      selection.sentenceTranslation = result.sentence_translation || '';
      selection.meaningSource = result.primary_status;
      if (!popover.hidden) { renderWordContext(result); updateSave(); positionPopover(); }
    }).catch(() => {
      contextCache.delete(cacheKey);
      if (selected !== selection) return;
      selection.loading = false;
      if (!popover.hidden) {
        renderWordContext({sentence:line.text, other_meanings:meanings,
          note:'暫時無法讀取影片語境，字典用法可在下方展開查看。'});
        updateSave(); positionPopover();
      }
    });
  }
  function renderWordContext(result, loading = false) {
    const content = $('popover-meanings'); content.replaceChildren();
    const dictionaryOnly = !result.primary_meaning || result.primary_status === 'dictionary_reference';
    const context = element('section', undefined, 'video-word-meaning' + (dictionaryOnly ? ' dictionary-reference' : ''));
    popover.querySelector('.meaning-source').textContent = dictionaryOnly ? '本機字典 · 不需 API' : '影片語境 · 字典補充';
    context.append(element('h3',dictionaryOnly ? '字典釋義' : '影片中的用法'));
    if (result.primary_meaning) {
      context.append(element('p',result.primary_meaning.translation,'context-translation'));
      const origins={dictionary_reference:'本機字典釋義',user_selected:'你選定的本句意思',whole_caption:'影片原有的中文字幕對照',ai_context:'AI 語境解釋 · 未查詢網路',caption_match:'字幕與字典對照 · 尚待確認'};
      context.append(element('p',origins[result.primary_status] || '字典對照','meaning-origin'));
    }
    context.append(element('p',result.sentence || selected.sentence,'video-sentence'));
    if (loading && window.AISettings?.enabled()) context.append(element('p','正在判斷影片中的用法…','help'));
    if (result.sentence_translation && result.primary_status !== 'whole_caption') {
      context.append(element('p',result.translation_source || '影片原有的中文字幕','meaning-origin'));
      context.append(element('p',result.sentence_translation,'caption-translation'));
    }
    const alternatives = result.other_meanings || [];
    if (!loading && !result.primary_meaning && !alternatives.length) {
      context.append(element('p','本機字典暫無這個詞，可使用下方網路搜尋。','help'));
    }
    if (result.primary_status === 'ai_context' || result.ai_status === 'unavailable' || result.ai_status === 'uncertain') {
      if (result.note) context.append(element('p',result.note,'context-note'));
    } else if (dictionaryOnly && result.primary_meaning) {
      context.append(element('p','可直接收藏；字典釋義不代表已判斷本句語境。','context-note'));
    }
    content.append(context);
    function meaningItem(meaning) {
      const item=element('section',undefined,'alternative-meaning');
      item.append(element('p',meaning.translation || '尚無中文釋義'),element('p',meaning.pos,'pos'));
      if (meaning.example) {
        const example=element('details',undefined,'dictionary-example');
        example.append(element('summary','查看字典例句'),element('p',meaning.example,'help'));item.append(example);
      }
      item.append(button('選為這句的意思',()=>{
        selected.meanings=[meaning]; selected.meaningSource='user_selected';
        const rest=selected.allMeanings.filter(other=>JSON.stringify(other)!==JSON.stringify(meaning));
        renderWordContext({...result,primary_meaning:meaning,primary_status:'user_selected',other_meanings:rest,note:''});
        updateSave();positionPopover();
      },'choose-meaning'));
      return item;
    }
    if (alternatives.length) {
      let remaining=alternatives;
      if (!result.primary_meaning) {
        const visible=element('div',undefined,'dictionary-candidates');
        if (alternatives.length>1) visible.append(element('p','有多種字典意思，可選擇符合這句的用法。','help'));
        alternatives.slice(0,2).forEach(meaning=>visible.append(meaningItem(meaning)));
        content.append(visible);remaining=alternatives.slice(2);
      }
      if (remaining.length) {
        const details=element('details',undefined,'other-meanings');
        details.append(element('summary',`其他字典用法（${remaining.length}）`));
        remaining.forEach(meaning=>details.append(meaningItem(meaning)));
        details.addEventListener('toggle',positionPopover);content.append(details);
      }
    }
  }
  function token(word, matching, line) {
    const node = button(word, () => showWord(node, word, matching, line), 'word-token');
    node.setAttribute('aria-expanded', 'false'); node.setAttribute('aria-controls', 'word-popover');
    return node;
  }
  function renderSentence(text, line) {
    // Match longest dictionary words first. Lemmas absent from the sentence are shown separately.
    const contextual = records.filter(record => Number.isFinite(record.timestamp_start) &&
      (source ? Math.abs(record.timestamp_start - line.start) < 0.002 :
        record.timestamp_start <= line.end + 0.2 && (record.timestamp_end ?? record.timestamp_start) >= line.start - 0.2));
    const words = [...new Set(contextual.map(record => String(record.原文 || '').trim()).filter(Boolean))].sort((a,b) => b.length-a.length);
    const shown = new Set();
    for (const part of line.text.split(/(\s+)/)) {
      let offset = 0;
      while (offset < part.length) {
        const word = words.find(word => part.startsWith(word, offset));
        if (word) { text.append(token(word, contextual.filter(r => r.原文 === word), line)); shown.add(word); offset += word.length; }
        else {
          let end = offset + 1;
          while (end < part.length && !words.some(word => part.startsWith(word, end))) end++;
          const fragment = part.slice(offset, end);
          text.append(/\S/.test(fragment) ? token(fragment, [], line) : document.createTextNode(fragment)); offset = end;
        }
      }
    }
    return {contextual, words, shown};
  }
  function renderTimeline() {
    close(); active = null; captionIndex = 0; browsingCaptions = false;
    renderCaption();
    const list = $('subtitle-list'); list.replaceChildren();
    $('subtitle-count').textContent = `${subtitles.length} 段`;
    if (!subtitles.length) { list.append(element('p', '尚無字幕。請先選擇影片並開始辨識。', 'empty-state')); return; }
    subtitles.forEach((line, index) => {
      const row = element('article', undefined, 'subtitle-row'); row.dataset.index = index;
      const jump = button(`${time(line.start)} — ${time(line.end)}`, () => seek(line.start), 'time-link');
      row.append(jump);
      const text = element('div', undefined, 'subtitle-text');
      const {contextual, words, shown} = renderSentence(text, line);
      row.append(text);
      const extra = words.filter(word => !shown.has(word));
      if (extra.length) {
        const related = element('div', undefined, 'subtitle-words'); related.append(element('span', '相關詞形：', 'help'));
        extra.forEach(word => related.append(token(word, contextual.filter(r => r.原文 === word), line))); row.append(related);
      }
      list.append(row);
    });
    sync();
  }
  function renderCaption(previousIndex = captionIndex, playing = false) {
    const caption = $('current-subtitle');
    if (anchor && caption.contains(anchor)) close();
    caption.replaceChildren();
    const line = subtitles[captionIndex];
    const previous = subtitles[captionIndex - 1], next = subtitles[captionIndex + 1];
    $('previous-subtitle').textContent = previous?.text || (line ? '已經是第一句' : '還沒有字幕');
    $('next-subtitle').textContent = next?.text || (line ? '已經是最後一句' : '還沒有字幕');
    $('previous-subtitle').disabled = $('caption-prev-btn').disabled = !previous;
    $('next-subtitle').disabled = $('caption-next-btn').disabled = !next;
    $('caption-seek').disabled = !line;
    $('caption-follow-btn').hidden = !browsingCaptions;
    if (!line) {
      caption.textContent = '字幕準備好後，會在這裡跟隨影片顯示。';
      $('caption-position-label').textContent = '目前';
      $('caption-seek').textContent = '播放這一句';
      return;
    }
    $('caption-position-label').textContent = `${browsingCaptions ? '瀏覽' : playing ? '目前' : '附近字幕'} · ${captionIndex + 1} / ${subtitles.length}`;
    $('caption-seek').textContent = `${time(line.start)} ↗ 播放這一句`;
    renderSentence(caption, line);
    if (previousIndex !== captionIndex && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
      captionAnimation?.cancel();
      captionAnimation = $('caption-reader').animate([
        {transform:`translateY(${captionIndex > previousIndex ? 12 : -12}px)`, opacity:0.65},
        {transform:'translateY(0)', opacity:1},
      ], {duration:180, easing:'ease-out'});
    }
  }
  function browseCaption(direction) {
    if (!subtitles.length) return false;
    const next = Math.max(0, Math.min(subtitles.length - 1, captionIndex + direction));
    if (next === captionIndex) return false;
    close();
    const previous = captionIndex; captionIndex = next; browsingCaptions = true;
    renderCaption(previous);
    return true;
  }
  function sync() {
    const indices = [];
    const current = currentMode === 'youtube' ? window.YouTubePlayback.currentTime() : video.currentTime;
    subtitles.forEach((line,index) => { if (current >= line.start && current < Math.max(line.end, line.start + 0.8)) indices.push(index); });
    let nearest = 0;
    subtitles.forEach((line,index) => { if (line.start <= current) nearest = index; });
    const key = indices.join(',') + '|' + nearest;
    if (active === key) return; active = key;
    document.querySelectorAll('.subtitle-row').forEach(row => row.classList.toggle('active', indices.includes(Number(row.dataset.index))));
    if (!browsingCaptions) {
      const previousIndex = captionIndex;
      if (indices.length) captionIndex = indices[0];
      else {
        captionIndex = 0;
        subtitles.forEach((line,index) => { if (line.start <= current) captionIndex = index; });
      }
      renderCaption(previousIndex, indices.includes(captionIndex));
    }
    if ($('follow-subtitles').checked && indices.length) {
      const list = $('subtitle-list'), row = list.querySelector(`[data-index="${indices[0]}"]`);
      if (row) {
        const item = row.getBoundingClientRect(), box = list.getBoundingClientRect();
        if (item.top < box.top || item.bottom > box.bottom) list.scrollTop += item.top - box.top - 12;
      }
    }
  }
  async function showResults(body, id) {
    const ticket = ++revision;
    jobId = id; records = body.records || [];
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(id)}/subtitles`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      if (ticket !== revision) return;
      subtitles = result.subtitles || []; renderTimeline();
      if (source) $('results-note').textContent = '可用時參考影片中文字幕；其他用法來自本機韓中字典。';
    } catch (error) {
      if (ticket !== revision) return;
      subtitles = []; renderTimeline();
      $('results-note').textContent = `無法載入字幕：${error.message}。單字結果仍可在進階設定內查看。`;
    }
  }
  $('timeline-toggle').addEventListener('click', () => {
    close();
    const expanded = $('timeline-toggle').getAttribute('aria-expanded') === 'true';
    $('timeline-toggle').setAttribute('aria-expanded', String(!expanded));
    $('timeline-toggle').textContent = expanded ? '展開字幕時間軸' : '收起字幕時間軸';
    $('timeline-panel').hidden = expanded;
    $('learning-workspace').classList.toggle('timeline-collapsed', expanded);
  });
  $('caption-prev-btn').addEventListener('click', () => browseCaption(-1));
  $('caption-next-btn').addEventListener('click', () => browseCaption(1));
  $('previous-subtitle').addEventListener('click', () => browseCaption(-1));
  $('next-subtitle').addEventListener('click', () => browseCaption(1));
  $('caption-seek').addEventListener('click', () => {
    if (!subtitles[captionIndex]) return;
    seek(subtitles[captionIndex].start);
    browsingCaptions = false; $('caption-follow-btn').hidden = true; active = null;
  });
  $('caption-follow-btn').addEventListener('click', () => {
    close(); browsingCaptions = false; active = null; sync();
  });
  let wheelDelta = 0, lastWheelStep = 0;
  $('caption-reader').addEventListener('wheel', event => {
    if (event.ctrlKey || Math.abs(event.deltaX) > Math.abs(event.deltaY) || !subtitles.length || !popover.hidden) return;
    const direction = Math.sign(event.deltaY);
    if (!direction || (direction < 0 && captionIndex === 0) || (direction > 0 && captionIndex === subtitles.length - 1)) return;
    event.preventDefault();
    const now = performance.now();
    if (now - lastWheelStep < 200) return;
    if (Math.sign(wheelDelta) !== direction) wheelDelta = 0;
    wheelDelta += event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 300 : 1);
    if (Math.abs(wheelDelta) >= 35) { browseCaption(direction); wheelDelta = 0; lastWheelStep = now; }
  }, {passive:false});
  document.addEventListener('ai-settings-changed', () => { contextCache.clear(); close(); });
  $('video-file').addEventListener('change', () => {
    const file = $('video-file').files[0]; if (!file) return;
    serverVideoJob=null; serverVideoName='';
    selectedFile = file; $('file-picker-text').textContent = '更換影片';
    $('selected-file-meta').textContent = `${file.size >= 1048576 ? (file.size / 1048576).toFixed(1) + ' MB' : Math.max(1,Math.round(file.size / 1024)) + ' KB'} · ${file.name.split('.').pop().toUpperCase()}`;
    video.pause(); if (videoUrl) URL.revokeObjectURL(videoUrl);
    source = null; window.YouTubePlayback.hide();
    videoUrl = URL.createObjectURL(file); video.src = videoUrl; video.hidden = false; video.load();
    $('video-placeholder').hidden = true; $('selected-file').textContent = file.name;
    $('player-notice').textContent = '影片已選擇。按播放預覽，或按「開始辨識」產生字幕。';
  });
  video.addEventListener('error', () => { $('player-notice').textContent = '瀏覽器無法播放這種編碼，請改用可播放的 MP4。後端能辨識的格式不一定能在瀏覽器播放。'; });
  video.addEventListener('timeupdate', sync);
  document.addEventListener('youtube-timeupdate', sync);
  $('rewind-btn').addEventListener('click', () => {
    if (currentMode === 'youtube' && source) {
      window.YouTubePlayback.seek(Math.max(0, window.YouTubePlayback.currentTime() - 5)); return;
    }
    if (video.readyState < 1) { $('player-notice').textContent = '請先選擇影片。'; return; }
    video.currentTime = Math.max(0, video.currentTime - 5);
  });
  $('popover-close').addEventListener('click', () => { close(); anchor?.focus(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') { close(); anchor?.focus(); } });
  document.addEventListener('click', event => { if (!popover.contains(event.target) && !event.target.closest('.word-token')) close(); });
  document.addEventListener('scroll', close, true); window.addEventListener('resize', close);
  $('save-word-btn').addEventListener('click', () => {
    if (!selected || library.some(item => item.key === selected.key)) return;
    library.push({...selected}); persist(); renderLibrary(); updateSave();
  });
  $('library-export-btn').addEventListener('click', () => {
    const cell = value => '"' + String(value ?? '').replaceAll('"','""') + '"';
    const rows = [['原文','中文','詞性','例句','影片','時間秒']];
    library.forEach(item => (item.meanings.length ? item.meanings : [{}]).forEach(meaning => rows.push([item.word,meaning.translation,meaning.pos,meaning.example,item.fileName,item.start])));
    const blob = new Blob(['\uFEFF' + rows.map(row => row.map(cell).join(',')).join('\r\n')], {type:'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob); const link = document.createElement('a');
    link.href = url; link.download = '我的韓文單字庫.csv'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  renderLibrary();
  function setMode(mode) {
    currentMode = mode;
    $('youtube-form').hidden = mode !== 'youtube'; $('local-upload').hidden = mode !== 'local';
    $('repair-video-btn').hidden=mode!=='local';
    $('youtube-mode-btn').setAttribute('aria-pressed', String(mode === 'youtube'));
    $('local-mode-btn').setAttribute('aria-pressed', String(mode === 'local'));
    close();
    if (mode === 'local') {
      window.YouTubePlayback.hide(); video.hidden = !(selectedFile || serverVideoJob); $('video-placeholder').hidden = !!(selectedFile || serverVideoJob);
    } else {
      video.pause(); video.hidden = true;
      if (source) window.YouTubePlayback.load(source.youtube_id).catch(error => { $('player-notice').textContent = error.message; });
      else { $('youtube-player-wrap').hidden = true; $('video-placeholder').hidden = false; }
    }
  }
  function setSource(value) {
    if (!value || (source?.youtube_id === value.youtube_id && resultFile === `youtube:${value.youtube_id}`)) return;
    source = value; resultFile = `youtube:${value.youtube_id}`;
    $('youtube-url').value = value.url;
    setMode('youtube');
    $('player-notice').textContent = `${value.is_generated ? '韓文自動字幕' : '韓文人工字幕'} · ${value.subtitle_count} 段${value.warning ? ' · ' + value.warning : ''}`;
    $('current-subtitle').textContent = '按播放開始，字幕會隨著影片進度顯示。';
  }
  function restoreLocalVideo(id, name, url, compatible=false) {
    if (selectedFile && (!compatible || identity(selectedFile)!==resultFile)) return;
    if (compatible) url += '?compatible=1';
    if (serverVideoJob === id && video.getAttribute('src') === url) return;
    const position=video.currentTime;
    serverVideoJob=id; serverVideoName=name || '已上傳影片';
    source=null; resultFile=resultFile || `local:${id}`;
    video.src=url; video.load();setMode('local');
    video.addEventListener('loadedmetadata',()=>{if(position<video.duration)video.currentTime=position;},{once:true});
    $('selected-file').textContent=serverVideoName;
    $('selected-file-meta').textContent='已上傳 · 可直接播放';
    $('file-picker-text').textContent='更換影片';
    $('player-notice').textContent=compatible ? '已載入相容播放版本；原影片與字幕時間不變。' : '已恢復這次工作上傳的影片，不用重新選檔。';
  }
  $('repair-video-btn').addEventListener('click',async()=>{
    if (!jobId) { $('player-notice').textContent='請先上傳影片建立工作，才能產生相容播放版本。';return; }
    const button=$('repair-video-btn');button.disabled=true;
    $('player-notice').textContent='正在本機轉換相容播放版本，原影片不會修改。請稍候…';
    try {
      const response=await fetch(`/api/jobs/${encodeURIComponent(jobId)}/prepare-video`,{method:'POST'});
      const result=await response.json();
      if (!response.ok) throw new Error(result.detail || '轉換未成功');
      const position=video.currentTime;
      video.src=result.video_url;video.hidden=false;video.load();
      video.addEventListener('loadedmetadata',()=>{if(position<video.duration)video.currentTime=position;},{once:true});
      $('player-notice').textContent='已切換相容版本，請按播放確認畫面。字幕仍對應原影片時間。';
    } catch(error) { $('player-notice').textContent=error.message; }
    finally {button.disabled=false;}
  });
  $('youtube-mode-btn').addEventListener('click', () => setMode('youtube'));
  $('local-mode-btn').addEventListener('click', () => setMode('local'));
  return {
    showResults,
    attachJob(id) { jobId=id; },
    setSource,
    restoreLocalVideo,
    setBusy(value) {
      window.LocalMedia?.setBusy(value);
      $('youtube-load-btn').disabled = value; $('youtube-url').disabled = value;
      $('youtube-mode-btn').disabled = value; $('local-mode-btn').disabled = value;
    },
    beginJob() {
      ++revision; jobId=null; source = null; resultFile = identity(selectedFile); close(); subtitles = []; records = []; renderTimeline();
      $('current-subtitle').textContent = '影片辨識中，請稍候。';
    },
    restoreFile(value) { resultFile = value || null; },
    fileIdentity: () => resultFile,
  };
})();

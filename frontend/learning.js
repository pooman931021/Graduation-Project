/* Learning workspace: actual OCR sentences + channel dictionary records. */
window.Learning = (() => {
  const $ = id => document.getElementById(id);
  const video = $('learning-video');
  const popover = $('word-popover');
  const storageKey = 'korean-learning-library-v1';
  let videoUrl = null, selectedFile = null, resultFile = null, jobId = null;
  let subtitles = [], records = [], selected = null, anchor = null, active = '';
  let revision = 0;
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
    if (!selectedFile || (fileIdentity && identity(selectedFile) !== fileIdentity)) {
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
      item.meanings.forEach(meaning => card.append(element('p', `${meaning.pos || ''} ${meaning.translation || '尚無中文釋義'}`.trim())));
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
    $('save-word-btn').disabled = !!saved;
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
    close(); anchor = node; node.setAttribute('aria-expanded', 'true');
    const meanings = [...new Map(matches.map(record => [JSON.stringify([record.中文, record.詞性, record.例句]), {
      translation: record.中文 || '', pos: record.詞性 || '', example: record.例句 || ''
    }])).values()];
    selected = { key: JSON.stringify([jobId, word, line.start]), word, meanings,
      start: line.start, jobId, fileIdentity: resultFile, fileName: selectedFile?.name || '', sentence: line.text };
    $('popover-word').textContent = word;
    const content = $('popover-meanings'); content.replaceChildren();
    if (!meanings.length) content.append(element('p', '這個詞尚無對應釋義。可先收藏，之後補充翻譯。'));
    meanings.forEach(meaning => {
      const section = element('section');
      section.append(element('p', meaning.pos, 'pos'), element('p', meaning.translation || '尚無中文釋義'));
      if (meaning.example) section.append(element('p', `字典例句：${meaning.example}`, 'help'));
      content.append(section);
    });
    updateSave(); popover.hidden = false; positionPopover();
  }
  function token(word, matching, line) {
    const node = button(word, () => showWord(node, word, matching, line), 'word-token');
    node.setAttribute('aria-expanded', 'false'); node.setAttribute('aria-controls', 'word-popover');
    return node;
  }
  function renderTimeline() {
    close(); active = '';
    const list = $('subtitle-list'); list.replaceChildren();
    $('subtitle-count').textContent = `${subtitles.length} 段`;
    if (!subtitles.length) { list.append(element('p', '尚無字幕。請先選擇影片並開始辨識。', 'empty-state')); return; }
    subtitles.forEach((line, index) => {
      const row = element('article', undefined, 'subtitle-row'); row.dataset.index = index;
      const jump = button(`${time(line.start)} — ${time(line.end)}`, () => seek(line.start), 'time-link');
      row.append(jump);
      const text = element('div', undefined, 'subtitle-text');
      // Match longest dictionary words first. Lemmas absent from the sentence are shown separately.
      const contextual = records.filter(record => Number.isFinite(record.timestamp_start) &&
        record.timestamp_start <= line.end + 0.2 && (record.timestamp_end ?? record.timestamp_start) >= line.start - 0.2);
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
  function sync() {
    const indices = [];
    const current = video.currentTime;
    subtitles.forEach((line,index) => { if (current >= line.start && current < Math.max(line.end, line.start + 0.8)) indices.push(index); });
    const key = indices.join(',');
    if (active === key) return; active = key;
    document.querySelectorAll('.subtitle-row').forEach(row => row.classList.toggle('active', indices.includes(Number(row.dataset.index))));
    $('current-subtitle').textContent = indices.map(index => subtitles[index].text).join(' ／ ') || '目前沒有辨識字幕。';
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
    } catch (error) {
      if (ticket !== revision) return;
      subtitles = []; renderTimeline();
      $('results-note').textContent = `無法載入字幕：${error.message}。單字結果仍可在進階設定內查看。`;
    }
  }
  $('video-file').addEventListener('change', () => {
    const file = $('video-file').files[0]; if (!file) return;
    selectedFile = file; video.pause(); if (videoUrl) URL.revokeObjectURL(videoUrl);
    videoUrl = URL.createObjectURL(file); video.src = videoUrl; video.hidden = false; video.load();
    $('video-placeholder').hidden = true; $('selected-file').textContent = file.name;
    $('player-notice').textContent = '影片已選擇。按播放預覽，或按「開始辨識」產生字幕。';
  });
  video.addEventListener('error', () => { $('player-notice').textContent = '瀏覽器無法播放這種編碼，請改用可播放的 MP4。後端能辨識的格式不一定能在瀏覽器播放。'; });
  video.addEventListener('timeupdate', sync);
  $('rewind-btn').addEventListener('click', () => {
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
  return {
    showResults,
    beginJob() {
      ++revision; resultFile = identity(selectedFile); close(); subtitles = []; records = []; renderTimeline();
      $('current-subtitle').textContent = '影片辨識中，請稍候。';
    },
    restoreFile(value) { resultFile = value || null; },
    fileIdentity: () => resultFile,
  };
})();

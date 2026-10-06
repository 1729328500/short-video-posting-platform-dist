/* 短视频发布工具 · 前端（v0.1：应用骨架 + 素材库） */
(function () {
  'use strict';

  var STATE = { videos: [], search: '' };
  var EXTS = ['mp4', 'mov', 'm4v', 'webm', 'mkv', 'avi'];
  var PLATS = [
    ['douyin', '抖音'], ['channels', '视频号'], ['kuaishou', '快手'], ['xhs', '小红书'],
    ['weibo', '微博'], ['toutiao', '头条号'], ['bilibili', '哔哩哔哩'], ['xigua', '西瓜视频']
  ];

  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var toastTimer = null;
  function toast(msg) {
    var t = $('#toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('show'); }, 2600);
  }

  // ★ 统一错误处理（修审查报告 2.7）：原来几十处调用都是裸 await fetch，
  //   后台没起来时全是 unhandled rejection，界面「点了没反应」。
  //   现在统一兜住：网络失败 → 返回 {ok:false}；401 → 弹登录框。
  var LOGIN_SHOWN = false;
  function needLogin() {
    var m = document.getElementById('lockMask');
    if (m && !m.classList.contains('show')) {
      m.classList.add('show');
      if (!LOGIN_SHOWN) { LOGIN_SHOWN = true; toast('需要登录（服务端已开启访问口令）'); }
    }
  }
  async function _req(url, opts) {
    var r;
    try {
      r = await fetch(url, opts);
    } catch (e) {
      return { ok: false, error: '连不上后台服务（服务可能没在运行）' };
    }
    if (r.status === 401) { needLogin(); return { ok: false, error: '未登录', need_login: true }; }
    try {
      return await r.json();
    } catch (e) {
      return { ok: false, error: '服务返回了非 JSON（HTTP ' + r.status + '）' };
    }
  }
  function apiGet(url) { return _req(url); }
  function apiPost(url, body) {
    return _req(url, {
      method: 'POST',
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined
    });
  }

  /* ---------- 展示格式 ---------- */
  function fmtSize(n) {
    if (n >= 1024 * 1024 * 1024) return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    if (n >= 1024) return (n / 1024).toFixed(0) + ' KB';
    return n + ' B';
  }
  function fmtDur(sec) {
    if (sec == null) return '';
    sec = Math.round(sec);
    if (sec < 60) return sec + ' 秒';
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + ' 分' + (s > 0 ? ' ' + s + ' 秒' : '');
  }
  function fmtWhen(s) { return String(s || '').slice(5, 16); }

  /* ---------- 素材库 ---------- */
  function renderVideos() {
    var grid = $('#vgrid');
    var kw = STATE.search.trim().toLowerCase();
    var list = STATE.videos.filter(function (v) {
      return !kw || v.name.toLowerCase().indexOf(kw) >= 0;
    });
    grid.innerHTML = '';
    $('#vempty').style.display = STATE.videos.length ? 'none' : '';
    list.forEach(function (v) {
      var card = document.createElement('div');
      card.className = 'vcard';
      card.dataset.id = v.id;
      var meta = [];
      if (v.duration != null) meta.push(fmtDur(v.duration));
      if (v.width && v.height) meta.push(v.width + '×' + v.height);
      meta.push(fmtSize(v.size));
      meta.push(fmtWhen(v.added_at));
      var thumb = v.cover
        ? '<img class="vthumbimg" src="/api/videos/cover/' + v.id + '?f=' + encodeURIComponent(v.cover) + '" alt="">'
        : '▶';
      card.innerHTML =
        '<div class="vthumb">' + thumb + '</div>' +
        '<div class="vinfo">' +
        '<div class="vname" title="' + esc(v.name) + '">' + esc(v.name) + '</div>' +
        '<div class="vmeta">' + meta.join(' · ') + '</div>' +
        '</div>' +
        '<div class="vacts">' +
        '<button class="btn ghost small act-cover">封面</button>' +
        '<button class="btn ghost small act-precheck">预检</button>' +
        '<button class="btn ghost small act-rename">重命名</button>' +
        '<button class="btn ghost small act-del">删除</button>' +
        '</div>';
      card.querySelector('.act-cover').addEventListener('click', function () { openCoverModal(v); });
      card.querySelector('.act-precheck').addEventListener('click', function () { openPrecheckModal(v); });
      card.querySelector('.act-rename').addEventListener('click', function () { startRename(card, v); });
      card.querySelector('.act-del').addEventListener('click', function () { doDelete(v); });
      grid.appendChild(card);
    });
  }

  async function loadVideos() {
    try {
      var j = await apiGet('/api/videos');
      STATE.videos = j.videos || [];
    } catch (e) {
      STATE.videos = [];
    }
    renderVideos();
  }

  function startRename(card, v) {
    if (card.querySelector('.vname-input')) return;
    var nameEl = card.querySelector('.vname');
    var input = document.createElement('input');
    input.className = 'vname-input';
    input.value = v.name;
    nameEl.replaceWith(input);
    input.focus();
    var dot = v.name.lastIndexOf('.');
    input.setSelectionRange(0, dot > 0 ? dot : v.name.length);

    async function save() {
      if (input.dataset.done) return;
      input.dataset.done = '1';
      var nv = input.value.trim();
      if (!nv || nv === v.name) { await loadVideos(); return; }
      var j = await apiPost('/api/videos/rename', { id: v.id, name: nv });
      if (!j.ok) toast('重命名失败：' + (j.error || ''));
      else toast('已重命名');
      await loadVideos();
    }
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') save();
      if (e.key === 'Escape') { input.dataset.done = '1'; loadVideos(); }
    });
    input.addEventListener('blur', save);
  }

  async function doDelete(v) {
    if (!confirm('确定删除「' + v.name + '」？文件将从本机删除。')) return;
    var j = await apiPost('/api/videos/delete', { id: v.id });
    if (!j.ok) { toast('删除失败：' + (j.error || '')); return; }
    toast('已删除');
    await loadVideos();
  }

  async function doUpload(files) {
    var list = [];
    var skipped = [];
    Array.prototype.slice.call(files).forEach(function (f) {
      var ext = (f.name.split('.').pop() || '').toLowerCase();
      if (EXTS.indexOf(ext) < 0) skipped.push(f.name);
      else list.push(f);
    });
    if (skipped.length) toast('已跳过不支持的文件：' + skipped.join('、'));
    var ok = 0, fail = 0;
    for (var i = 0; i < list.length; i++) {
      $('#dropzone').textContent = '上传中 ' + (i + 1) + '/' + list.length + '：' + list[i].name;
      try {
        var r = await fetch('/api/videos/upload?name=' + encodeURIComponent(list[i].name), { method: 'POST', body: list[i] });
        var j = await r.json();
        if (j.ok) ok++;
        else { fail++; toast('「' + list[i].name + '」上传失败：' + (j.error || '')); }
      } catch (e) {
        fail++;
        toast('「' + list[i].name + '」上传失败：' + e.message);
      }
    }
    $('#dropzone').textContent = '把视频拖到这里，或点击上传 · 支持多选';
    if (ok) toast('已上传 ' + ok + ' 个视频' + (fail ? ('，失败 ' + fail + ' 个') : ''));
    await loadVideos();
  }

  async function uploadFromLocal(files, autoSelect) {
    var list = Array.prototype.slice.call(files);
    var firstId = null, okn = 0;
    for (var i = 0; i < list.length; i++) {
      var f = list[i];
      var ext = (f.name.split('.').pop() || '').toLowerCase();
      if (EXTS.indexOf(ext) < 0) { toast('已跳过不支持的文件：' + f.name); continue; }
      try {
        var r = await fetch('/api/videos/upload?name=' + encodeURIComponent(f.name), { method: 'POST', body: f });
        var j = await r.json();
        if (j.ok) { okn++; if (!firstId) firstId = j.video.id; }
        else toast('「' + f.name + '」上传失败：' + (j.error || ''));
      } catch (e) {
        toast('「' + f.name + '」上传失败：' + e.message);
      }
    }
    if (okn) {
      toast('已上传 ' + okn + ' 个视频（已存入素材库）');
      await loadVideos();
      await loadVideoOptions();
      if (autoSelect && firstId) {
        $('#videoSel').value = String(firstId);
        renderVideoMeta();
        $('#draftState').textContent = '有未保存的修改';
      }
    }
  }

  /* ---------- 弹层 · 封面 · 预检 ---------- */
  var coverVideo = null;

  function openModal(title) {
    $('#mTitle').textContent = title;
    $('#mask').classList.add('open');
  }
  function closeModal() {
    $('#mask').classList.remove('open');
    $('#mBody').innerHTML = '';
    $('#mFoot').innerHTML = '';
    coverVideo = null;
  }
  function openCoverModal(v) {
    coverVideo = v;
    $('#mBody').innerHTML =
      '<video id="cvVideo" src="/api/videos/raw/' + v.id + '" controls preload="metadata" style="width:100%;max-height:300px;border-radius:10px;background:#000"></video>' +
      '<div class="mhint">两种方式：① 拖动视频到你想要的画面，点「截取当前帧」；② 或上传一张图片当封面。</div>';
    $('#mFoot').innerHTML = '';
    var b1 = document.createElement('button');
    b1.className = 'btn ghost small';
    b1.id = 'cvUploadBtn';
    b1.textContent = '上传图片做封面';
    b1.addEventListener('click', function () { $('#imageInput').click(); });
    var b2 = document.createElement('button');
    b2.className = 'btn primary small';
    b2.id = 'cvCaptureBtn';
    b2.textContent = '截取当前帧';
    b2.addEventListener('click', captureCover);
    $('#mFoot').appendChild(b1);
    $('#mFoot').appendChild(b2);
    openModal('设置封面 · ' + v.name);
  }
  async function captureCover() {
    var v = $('#cvVideo');
    if (!v) return;
    try {
      if (v.readyState < 2) {
        await new Promise(function (res, rej) {
          var t = setTimeout(function () { rej(new Error('视频加载超时（可能是浏览器不支持的格式）')); }, 5000);
          v.addEventListener('loadeddata', function () { clearTimeout(t); res(); }, { once: true });
          v.load();
        });
      }
    } catch (e) {
      toast('无法读取视频画面：' + e.message);
      return;
    }
    var c = document.createElement('canvas');
    c.width = v.videoWidth;
    c.height = v.videoHeight;
    if (!c.width || !c.height) { toast('视频画面为空，无法截取'); return; }
    c.getContext('2d').drawImage(v, 0, 0);
    var blob = await new Promise(function (res) { c.toBlob(res, 'image/jpeg', 0.86); });
    if (!blob) { toast('截取失败'); return; }
    await uploadCoverBlob(blob, 'image/jpeg');
  }
  async function uploadCoverBlob(blob, ctype) {
    if (!coverVideo) { toast('请先选择素材'); return; }
    var r = await fetch('/api/videos/cover?id=' + coverVideo.id, {
      method: 'POST',
      headers: { 'Content-Type': ctype || blob.type || 'image/jpeg' },
      body: blob
    });
    var j = await r.json();
    if (j.ok) {
      toast('封面已更新');
      closeModal();
      await loadVideos();
      renderVideoMeta();      // 发布台上的封面缩略图也要跟着变（它不在 loadVideos 的刷新范围里）
    } else {
      toast('封面保存失败：' + (j.error || ''));
    }
  }
  // ★★ 封面硬要求（2026-09-30 查证，看的是 vendored 上传器代码）：
  //   微博 —— `weibo_uploader.validate_upload_args` 没有 --thumbnail **直接 raise**
  //   「微博视频发布必须提供封面图」⇒ 没封面**必失败**；
  //   抖音 —— 有兜底（页面弹「请设置封面后再发布」时自动选**推荐封面**）；
  //   快手/视频号 —— `if self.thumbnail_path:` 才设，没给就跳过；
  //   头条/B站（适配器路径）——「没设封面，跳过」，平台自己抽帧。
  var COVER_REQUIRED = ['weibo'];

  function coverRequired(plats) {
    return (plats || []).some(function (k) { return COVER_REQUIRED.indexOf(k) >= 0; });
  }

  /** (b) 自动兜底：从视频**第 1 帧**抽一张当封面。复用素材页那套 canvas 截帧思路，
   *  离屏跑、不给用户看；不依赖 ffmpeg（容器里没有）。成功返回 true。 */
  async function autoCover(v) {
    if (!v) return false;
    var vd = document.createElement('video');
    vd.src = '/api/videos/raw/' + v.id;
    vd.muted = true;
    vd.preload = 'auto';
    try {
      await new Promise(function (res, rej) {
        var t = setTimeout(function () { rej(new Error('视频加载超时')); }, 8000);
        vd.addEventListener('loadeddata', function () { clearTimeout(t); res(); }, { once: true });
        vd.load();
      });
    } catch (e) {
      return false;
    }
    var c = document.createElement('canvas');
    c.width = vd.videoWidth;
    c.height = vd.videoHeight;
    if (!c.width || !c.height) return false;       // 浏览器解不了这个格式
    c.getContext('2d').drawImage(vd, 0, 0);
    var blob = await new Promise(function (res) { c.toBlob(res, 'image/jpeg', 0.86); });
    if (!blob) return false;
    try {
      var r = await fetch('/api/videos/cover?id=' + v.id, {
        method: 'POST', headers: { 'Content-Type': 'image/jpeg' }, body: blob });
      var j = await r.json();
      return !!j.ok;
    } catch (e) {
      return false;
    }
  }

  async function openPrecheckModal(v) {
    var j = await apiGet('/api/videos/precheck?id=' + v.id);
    if (!j.ok) { toast('预检失败：' + (j.error || '')); return; }
    var rows = (j.platforms || []).map(function (p) {
      var right = p.ok
        ? '<span class="pk-ok-note">' + esc(p.recommend || '可发布') + '</span><span class="badge ok">通过</span>'
        : '<span class="pk-issues">' + esc(p.issues.join('；')) + '</span><span class="badge warn">注意</span>';
      return '<div class="pkrow"><b>' + esc(p.name) + '</b>' + right + '</div>';
    }).join('');
    var bad = (j.platforms || []).filter(function (p) { return !p.ok; }).length;
    var vv = j.video || {};
    var info = [
      '格式 ' + ((vv.stored || '').split('.').pop() || '—'),
      vv.duration != null ? ('时长 ' + fmtDur(vv.duration)) : '时长 —',
      (vv.width && vv.height) ? (vv.width + '×' + vv.height) : '',
      fmtSize(vv.size)
    ].filter(Boolean).join(' · ');
    $('#mBody').innerHTML =
      '<div class="mhint">素材：' + esc(vv.name || '') + '（' + info + '）</div>' +
      '<div class="mhint">按 8 个平台的上传限制预检（参考规则，可在 platform_rules.json 调整）：' +
      (bad ? '<b style="color:var(--warn)">' + bad + ' 个平台需注意</b>' : '全部通过') + '</div>' +
      rows;
    $('#mFoot').innerHTML = '';
    var fb = document.createElement('button');
    fb.className = 'btn ghost small';
    fb.textContent = '关闭';
    fb.addEventListener('click', closeModal);
    $('#mFoot').appendChild(fb);
    openModal('发布前预检 · ' + v.name);
  }

  /* ---------- 发布台 · 文案编辑 ---------- */
  var PLAT8 = ['douyin', 'channels', 'kuaishou', 'xhs', 'weibo', 'toutiao', 'bilibili', 'xigua'];
  var TAB_KEYS = ['common'].concat(PLAT8);
  var TAB_NAME = {
    common: '通用', douyin: '抖音', channels: '视频号', kuaishou: '快手', xhs: '小红书',
    weibo: '微博', toutiao: '头条号', bilibili: '哔哩哔哩', xigua: '西瓜视频'
  };
  var DRAFT = { video_id: null, active: 'common', copy: {} };
  var draftLoaded = false;

  function cc(k) {
    if (!DRAFT.copy[k] || typeof DRAFT.copy[k] !== 'object') DRAFT.copy[k] = { title: '', body: '', topics: '' };
    var c = DRAFT.copy[k];
    c.title = String(c.title || '');
    c.body = String(c.body || '');
    c.topics = String(c.topics || '');
    return c;
  }
  function flushFields() {
    var c = cc(DRAFT.active);
    c.title = $('#fTitle').value;
    c.body = $('#fBody').value;
    c.topics = $('#fTopics').value;
  }
  function loadFields() {
    var c = cc(DRAFT.active);
    $('#fTitle').value = c.title;
    $('#fBody').value = c.body;
    $('#fTopics').value = c.topics;
  }
  function renderCopyTabs() {
    var box = $('#copyTabs');
    box.innerHTML = '';
    TAB_KEYS.forEach(function (k) {
      var t = document.createElement('div');
      t.className = 'tab' + (k === DRAFT.active ? ' active' : '');
      t.dataset.k = k;
      t.textContent = TAB_NAME[k];
      if (k !== 'common') {
        var c = DRAFT.copy[k];
        if (c && (c.title || c.body || c.topics)) t.classList.add('has');
      }
      t.addEventListener('click', function () {
        if (k === DRAFT.active) return;
        flushFields();
        DRAFT.active = k;
        renderCopyTabs();
        loadFields();
      });
      box.appendChild(t);
    });
  }
  function refreshMarks() {
    $all('.tab').forEach(function (t) {
      var k = t.dataset.k;
      if (k === 'common') return;
      var c = DRAFT.copy[k];
      t.classList.toggle('has', !!(c && (c.title || c.body || c.topics)));
    });
  }
  function topicsFor(k, s) {
    // ★ `#` 也要当分隔符（2026-09-28 实测踩过）：原来只按空白/逗号/顿号切，
    //   而下面又给每个词**再补一遍** # —— 输入里本来就有 # 时就会叠加成双井号：
    //     '#旧房翻新# #墙面翻新#'  --微博-->  '##旧房翻新## ##墙面翻新##'
    //   这正是「一键生成各平台适配版」走基础版时的输入形态（AI 给微博的就是 #词#），
    //   所以极易撞上。后端 sau_bridge.parse_topics 是同一处病，已一并修。
    var arr = String(s || '').split(/[\s,，、#]+/).filter(Boolean);
    if (!arr.length) return '';
    if (k === 'weibo') return arr.map(function (t) { return '#' + t + '#'; }).join(' ');
    return arr.map(function (t) { return '#' + t; }).join(' ');
  }
  function ruleGenAll() {
    var src = cc('common');
    PLAT8.forEach(function (k) {
      var c = cc(k);
      c.title = src.title;
      c.body = src.body;
      c.topics = topicsFor(k, src.topics);
    });
  }
  async function genAll() {
    flushFields();
    var src = cc('common');
    if (!src.title && !src.body) { toast('先在「通用」里写标题或正文'); return; }
    var btn = $('#genAllBtn');
    btn.disabled = true;
    btn.textContent = '⏳ AI 生成中…';
    try {
      var j = await apiPost('/api/ai/generate', { title: src.title, body: src.body, topics: src.topics });
      if (j.ok && j.copies) {
        PLAT8.forEach(function (k) {
          var c = cc(k);
          var r = j.copies[k] || {};
          c.title = r.title || src.title;
          c.body = r.body || src.body;
          c.topics = r.topics || topicsFor(k, src.topics);
        });
        toast('✨ 已生成 8 平台 AI 适配版（点各平台 Tab 可逐平台微调）');
      } else {
        ruleGenAll();
        toast('AI 未成功（' + (j.error || '') + '），已用基础版');
      }
    } catch (e) {
      ruleGenAll();
      toast('AI 生成失败，已用基础版');
    }
    btn.disabled = false;
    btn.textContent = '⚡ 一键生成各平台适配版';
    refreshMarks();
    loadFields();
    $('#draftState').textContent = '有未保存的修改';
  }
  async function loadVideoOptions() {
    try {
      var j = await apiGet('/api/videos');
      STATE.videos = j.videos || [];
    } catch (e) { /* 保持原列表 */ }
    var sel = $('#videoSel');
    sel.innerHTML = '<option value="">（未选视频）</option>';
    STATE.videos.forEach(function (v) {
      var o = document.createElement('option');
      o.value = String(v.id);
      o.textContent = v.name;
      sel.appendChild(o);
    });
    if (DRAFT.video_id != null && STATE.videos.some(function (v) { return v.id === DRAFT.video_id; })) {
      sel.value = String(DRAFT.video_id);
    }
    renderVideoMeta();
  }
  function renderVideoMeta() {
    var v = STATE.videos.filter(function (x) { return String(x.id) === $('#videoSel').value; })[0];
    $('#videoMeta').textContent = v ? (fmtDur(v.duration) + (v.duration != null ? ' · ' : '') + fmtSize(v.size)) : '';
    DRAFT.video_id = v ? v.id : null;
    renderPubCover(v);
  }
  // 发布台的封面控件（2026-09-28 用户要求）：
  // 封面**归属这条视频**，发布时平台有封面位就填进去、没有就跳过 ——
  // 上传入口原来只在素材库，用户站在发布台上没法设，得绕回去找。
  // 这里复用素材库那个弹层（截帧 / 上传图片），逻辑一份。
  function renderPubCover(v) {
    var thumb = $('#pubCoverThumb'), btn = $('#pubCoverBtn'), hint = $('#pubCoverHint');
    if (!v) {
      thumb.style.display = 'none';
      btn.disabled = true;
      hint.textContent = '先从上面选一条视频';
      return;
    }
    btn.disabled = false;
    if (v.cover) {
      thumb.src = '/api/videos/cover/' + v.id + '?f=' + encodeURIComponent(v.cover);
      thumb.style.display = '';
      hint.textContent = '发布时填进平台的封面位（微博必须有封面；勾了微博会自动用这张）';
    } else {
      thumb.style.display = 'none';
      hint.textContent = '还没设封面 —— 可截视频画面或用图片；不设也行，平台会自己抽一帧（微博除外：发微博会自动抽第 1 帧兜底）';
    }
  }
  async function loadDraft() {
    try {
      var j = await apiGet('/api/draft');
      if (j.draft && j.draft.data) {
        var d = j.draft.data;
        DRAFT = {
          video_id: d.video_id != null ? d.video_id : null,
          active: TAB_KEYS.indexOf(d.active) >= 0 ? d.active : 'common',
          copy: (d.copy && typeof d.copy === 'object') ? d.copy : {}
        };
        $('#draftState').textContent = '上次保存：' + fmtWhen(j.draft.updated_at);
      }
    } catch (e) { /* 忽略 */ }
  }
  async function saveDraft() {
    flushFields();
    var j = await apiPost('/api/draft/save', { data: DRAFT });
    if (j.ok) {
      toast('草稿已保存');
      $('#draftState').textContent = '已保存：' + fmtWhen(j.draft.updated_at);
      refreshMarks();
    } else {
      toast('保存失败：' + (j.error || ''));
    }
  }
  async function initPublish() {
    if (!draftLoaded) {
      await loadDraft();
      draftLoaded = true;
    }
    await loadVideoOptions();
    renderCopyTabs();
    loadFields();
    renderPubPlats();
    resumeBatch();
  }

  /* ---------- 发布执行（S6） ---------- */
  var pubTimer = null;
  var pubBatchCur = 0;
  var PLAT_ORDER = ['douyin', 'channels', 'kuaishou', 'xhs', 'weibo', 'toutiao', 'bilibili', 'xigua'];

  function getCopyFor(k) {
    var c = DRAFT.copy[k];
    if (c && (c.title || c.body || c.topics)) return c;
    return cc('common');
  }

  async function renderPubPlats() {
    var box = $('#pubPlatRow');
    if (!box) return;
    var st = {};
    try {
      var j = await apiGet('/api/accounts');
      (j.accounts || []).forEach(function (a) { st[a.key] = a; });
    } catch (e) { /* 忽略 */ }
    box.innerHTML = '';
    PLAT_ORDER.forEach(function (k) {
      var a = st[k] || { status: 'off' };
      var dot = a.status === 'on' ? 'ok' : (a.status === 'waiting' ? 'warn' : 'off');
      var lab = document.createElement('label');
      lab.style.marginRight = '10px';
      lab.innerHTML = '<input type="checkbox" class="pubplat" data-k="' + k + '"' + (a.status === 'on' ? ' checked' : '') + '> ' + (TAB_NAME[k] || k) + ' <span class="dot ' + dot + '"></span>';
      box.appendChild(lab);
    });
  }

  function startPublishFlow() {
    flushFields();
    var vid = $('#videoSel').value;
    if (!vid) { toast('先在①选择视频'); return; }
    var plats = $all('.pubplat').filter(function (c) { return c.checked; }).map(function (c) { return c.dataset.k; });
    if (!plats.length) { toast('先在③勾选要发布的平台'); return; }
    var mode = document.querySelector('input[name=pubmode]:checked').value;
    var at = $('#pubScheduleAt').value;
    if (mode === 'schedule' && !at) { toast('选一下定时时间'); return; }
    var mock = $('#pubMock').checked;
    var video = STATE.videos.filter(function (v) { return String(v.id) === vid; })[0];
    var rows = plats.map(function (k) {
      var c = getCopyFor(k);
      var own = DRAFT.copy[k] && (DRAFT.copy[k].title || DRAFT.copy[k].body || DRAFT.copy[k].topics);
      return '<div class="pkrow"><b>' + esc(TAB_NAME[k]) + '</b>' +
        '<span class="badge info">' + (own ? '平台专属版' : '通用版') + '</span>' +
        '<span class="pk-ok-note">' + esc((c.title || '（无标题）').slice(0, 24)) + '</span></div>';
    }).join('');
    $('#mBody').innerHTML =
      '<div class="mhint">视频：' + esc((video && video.name) || '') + '　方式：' +
      (mode === 'schedule' ? ('定时 ' + esc(at.replace('T', ' '))) : '立即') +
      (mock ? '　（模拟执行 · 不真实提交）' : '') + '</div>' + rows +
      '<div class="mhint">确认后按顺序逐平台执行（平台间隔随机，可在设置调整）。</div>';
    $('#mFoot').innerHTML = '';
    var b1 = document.createElement('button');
    b1.className = 'btn ghost small';
    b1.textContent = '再改改';
    b1.addEventListener('click', closeModal);
    var b2 = document.createElement('button');
    b2.className = 'btn primary small';
    b2.textContent = '确认发布';
    b2.addEventListener('click', async function () {
      var copies = { common: cc('common') };
      plats.forEach(function (k) { copies[k] = getCopyFor(k); });
      // ★ (b)+(c) 封面兜底与拦截（2026-09-30 用户定）：勾了**必须有封面**的平台
      //   （目前只有微博）而这条视频没封面 → 先自动抽第 1 帧；抽不出来就拦下并说清
      //   怎么手动设 —— 别等跑完一整轮才失败。模拟发布不掺和（它又不真发）。
      if (!mock && coverRequired(plats) && video && !video.cover) {
        toast('这条视频没设封面，正在自动抽第 1 帧（微博必须有封面）…');
        var okCover = await autoCover(video);
        if (!okCover) {
          toast('自动抽帧失败 —— 微博必须有封面：请先去素材页给这条视频「设置封面」再发');
          return;
        }
        video.cover = 'auto';      // 本地先标上，免得再点一次又抽一遍（服务端回读会覆盖）
        toast('已自动生成封面（视频第 1 帧）；想换的话去素材页重设');
      }
      var j = await apiPost('/api/publish/create', {
        video_id: parseInt(vid, 10),
        platforms: plats,
        mode: mode,
        scheduled_at: mode === 'schedule' ? at.replace('T', ' ') : '',
        mock: mock,
        copies: copies
      });
      if (!j.ok) { toast('发布失败：' + (j.error || '')); return; }
      closeModal();
      toast('已加入发布队列：批次 #' + j.batch);
      showBatch(j.batch);
    });
    $('#mFoot').appendChild(b1);
    $('#mFoot').appendChild(b2);
    openModal('发布确认');
  }

  async function showBatch(batch) {
    pubBatchCur = batch;
    $('#pubProgressCard').style.display = '';
    await pollBatchOnce();
    if (!pubTimer) pubTimer = setInterval(pollBatchOnce, 1500);
  }

  function stopBatchPoll() {
    if (pubTimer) { clearInterval(pubTimer); pubTimer = null; }
  }

  async function pollBatchOnce() {
    if (!pubBatchCur) return;
    var j;
    try {
      j = await apiGet('/api/publish/batch?id=' + pubBatchCur);
    } catch (e) {
      console.log('[pub-debug] fetch fail: ' + (e && e.message));
      return;
    }
    if (!j.ok) { console.log('[pub-debug] api not ok'); return; }
    console.log('[pub-debug] ' + new Date().toLocaleTimeString() + ' ' + (j.jobs || []).map(function (r) { return r.platform + ':' + r.status; }).join(','));
    var jobs = j.jobs || [];
    // 引擎卡在「发布间隔」里时（默认 60~180 秒），下一条任务本来只显示「排队中」，
    // 分不清是按设计在等还是卡死了 —— 所以把剩余秒数亮出来。
    // 见 server.py 的 ENGINE_WAIT 说明。
    var wait = j.engine_wait || null;
    var waitShown = false;
    $('#pubBatchHint').textContent = '批次 #' + pubBatchCur + ' · 共 ' + jobs.length + ' 条';
    var html = '';
    var active = 0, okn = 0, failn = 0, manualn = 0;
    jobs.forEach(function (r) {
      var nm = TAB_NAME[r.platform] || r.platform;
      var stat = '';
      if (r.status === 'pending') {
        // 只给「下一条要跑的那条」挂倒计时（jobs 按 id 升序，第一条 pending 就是下一个）
        if (wait && !waitShown) {
          waitShown = true;
          // 走 parseInt 而不是直接拼（OCR 2026-09-28 指出）：同函数里 r.step、r.error
          // 都过了 esc，唯独这个响应字段直插 innerHTML。服务端目前保证是 int，
          // 但「服务端一定给 int」不该是前端安全的前提。
          var remain = parseInt(wait.remaining, 10);
          stat = '<span class="pstat">排队中 · 等发布间隔 ~' + (isNaN(remain) ? 0 : remain) + 's</span>';
        } else {
          stat = '<span class="pstat">排队中</span>';
        }
      }
      else if (r.status === 'running') stat = '<span class="pstat"><span class="spin"></span>' + esc(r.step || '进行中') + '</span>';
      else if (r.status === 'success') { stat = '<span class="pstat ok">成功 ✓</span>'; okn++; }
      else if (r.status === 'fail') { stat = '<span class="pstat fail">失败</span>'; failn++; }
      else if (r.status === 'manual') { stat = '<span class="pstat manual">需人工 ⚠</span>'; manualn++; }
      else if (r.status === 'canceled') stat = '<span class="pstat">已取消</span>';
      if (r.status === 'pending' || r.status === 'running') active++;
      if (r.error) stat += '<span class="mini-note">（' + esc(r.error) + '）</span>';
      // 正在跑的那条给一个**单独的中止**（2026-09-28 用户要求）：
      // 某个平台卡住（账号被限流、验证码不来、页面不动）时只收这一条，
      // 不连累同批其它平台。整批的中止仍在下面那个「中止本批次」上。
      var retry = (r.status === 'fail' || r.status === 'manual' || r.status === 'canceled')
        ? '<button class="btn ghost small retry-job" data-id="' + r.id + '">重试</button>' : '';
      var stopone = (r.status === 'running' || r.status === 'pending')
        ? '<button class="btn ghost small abort-job" data-id="' + r.id + '">中止</button>' : '';
      html += '<div class="prow"><span class="pname">' + esc(nm) + '</span><span class="psteps">' + esc(r.step || '') + '</span>' + stat + retry + stopone + '</div>';
    });
    $('#pubProgress').innerHTML = html;
    $all('.abort-job').forEach(function (b) {
      b.addEventListener('click', async function () {
        var r = await apiPost('/api/publish/cancel',
                              { batch: pubBatchCur, id: parseInt(b.dataset.id, 10) });
        if (r.ok) { toast('已中止该平台'); pollBatchOnce(); } else { toast('中止失败'); }
      });
    });
    $all('.retry-job').forEach(function (b) {
      b.addEventListener('click', async function () {
        var r = await apiPost('/api/publish/retry', { id: parseInt(b.dataset.id, 10) });
        if (r.ok) { toast('已重新排队'); pollBatchOnce(); } else { toast('重试失败：' + (r.error || '')); }
      });
    });
    $('#pubActions').innerHTML = '';
    if (active > 0) {
      var cbtn = document.createElement('button');
      cbtn.className = 'btn ghost small';
      // 文案从「取消未执行项」改成「中止本批次」：这个接口现在**连正在跑的那个也收**
      // （原来只管 pending，正在跑的只能干等 30 分钟超时）。账号被封、验证码不来、
      // 页面卡死时要的就是这一下，所以别再用会让人以为"只管排队"的措辞。
      cbtn.textContent = '中止本批次';
      cbtn.addEventListener('click', async function () {
        var r = await apiPost('/api/publish/cancel', { batch: pubBatchCur });
        var stopping = (r && r.stopping) || [];
        toast(stopping.length ? '已中止正在执行的任务，剩余项已取消' : '已取消未执行项');
        pollBatchOnce();
      });
      $('#pubActions').appendChild(cbtn);
    } else {
      var note = document.createElement('span');
      note.className = 'mini-note';
      note.textContent = '本批次完成：成功 ' + okn + ' · 失败 ' + failn + ' · 待人工 ' + manualn;
      $('#pubActions').appendChild(note);
      stopBatchPoll();
    }
  }

  async function resumeBatch() {
    try {
      var j = await apiGet('/api/publish/batches');
      var bs = j.batches || [];
      for (var i = 0; i < bs.length; i++) {
        var act = bs[i].jobs.some(function (r) { return r.status === 'pending' || r.status === 'running'; });
        if (act) { showBatch(bs[i].batch); return; }
      }
    } catch (e) { /* 忽略 */ }
  }

  /* ---------- 账号 ---------- */
  var AC_LOGO = { douyin: '🎵', channels: '💬', kuaishou: '⚡', xhs: '📕', weibo: '🌐', toutiao: '📰', bilibili: '📺', xigua: '🍉' };
  var acTimer = null;

  function acBadge(st) {
    if (st === 'on') return '<span class="badge ok">已登录</span>';
    if (st === 'waiting') return '<span class="badge warn">等待扫码</span>';
    if (st === 'error') return '<span class="badge bad">出错</span>';
    return '<span class="badge info">未登录</span>';
  }

  async function loadAccounts() {
    var j = await apiGet('/api/accounts');
    var box = $('#acGrid');
    box.innerHTML = '';
    var waiting = false;
    var verifyRunning = Object.keys(j.verify || {}).some(function (k) {
      return (j.verify[k] || {}).state === 'running';
    });
    (j.accounts || []).forEach(function (a) {
      if (a.status === 'waiting') waiting = true;
      var card = document.createElement('div');
      card.className = 'acard';
      card.dataset.key = a.key;
      var shared = !!a.shared_with;   // 与别的平台共用登录态（如西瓜=抖音）
      var vs = (j.verify || {})[a.key] || {};
      var verifying = (vs.state === 'running');
      var confirmBtn = (a.status === 'waiting' && !shared)
        ? '<button class="btn primary small act-confirm">我已登录完成</button>' : '';
      // 叫「登录」而不是「扫码登录」：**不是所有平台都有二维码登录**，
      // 有的只有账号密码（头条号一类），有的走微信授权。统一叫登录，
      // 进去之后能扫码就扫码、不能扫就去 noVNC 里用别的方式登。
      var loginLabel = shared ? ('登录' + (a.shared_with_name || '共用平台')) : '登录';
      card.innerHTML =
        '<div class="alogo">' + (AC_LOGO[a.key] || '📌') + '</div>' +
        '<div class="ainfo"><div class="aname">' + esc(a.name) +
        (shared ? ' <span class="mini-note">（与' + esc(a.shared_with_name || '') + '共用后台）</span>' : '') +
        '</div>' +
        '<div class="ast">' + acBadge(a.status) + '<span>' + esc(a.note || '') + '</span>' +
        (a.verified_at ? '<span class="mini-note">最后核验 ' + esc(fmtWhen(a.verified_at)) + '</span>' : '') +
        '</div>' +
        // ★ 核验失败要看得见（2026-09-30）：原来失败时界面一片安静，跟成功长得一样 ——
        //   而"完成"当时只反映子进程返回码，全失败也报完成。
        (vs.state === 'error'
          ? '<div class="ast"><span class="badge bad">核验失败</span><span>'
            + esc((vs.note || '').replace(/^核验失败[:：]?/, '') || '看服务日志') + '</span></div>'
          : '') + '</div>' +
        '<div class="aact">' +
        '<button class="btn ghost small act-verify"' + (verifying ? ' disabled' : '') + '>' +
        (verifying ? '核验中…' : '核验') + '</button>' +
        '<button class="btn ghost small act-login">' + esc(loginLabel) + '</button>' +
        confirmBtn +
        '<button class="btn ghost small act-close">关闭窗口</button>' +
        '</div>';
      card.querySelector('.act-verify').addEventListener('click', function () { acVerify(a.key); });
      card.querySelector('.act-login').addEventListener('click', function () { acLogin(a.key); });
      card.querySelector('.act-close').addEventListener('click', function () { acClose(a.key); });
      var cb = card.querySelector('.act-confirm');
      if (cb) cb.addEventListener('click', function () { acConfirm(a.key); });
      box.appendChild(card);
    });
    renderLamps(j.accounts || []);
    if ((waiting || verifyRunning) && !acTimer) {
      acTimer = setInterval(loadAccounts, 2500);
    } else if (!waiting && !verifyRunning && acTimer) {
      clearInterval(acTimer);
      acTimer = null;
    }
  }

  // ── 可交互扫码登录（全在页面里完成，容器/服务器形态也适用）──
  var QR = { key: null, timer: null, lastState: '', busy: false };

  function stopQrPoll() {
    if (QR.timer) { clearInterval(QR.timer); QR.timer = null; }
    QR.lastState = '';
  }

  async function acLogin(key) {
    if (!LOGIN_MODE) {
      try { LOGIN_MODE = (await apiGet('/api/health')).login_mode || 'qr'; }
      catch (e) { toast('服务暂时不可用，请稍后重试'); return; }
    }
    if (LOGIN_MODE === 'local') {
      var local = await apiPost('/api/accounts/login', { key: key });
      if (!local.ok) { toast('打开失败：' + (local.error || '')); return; }
      toast(local.note || '请在弹出的浏览器窗口中登录，成功后窗口会自动关闭');
      setTimeout(loadAccounts, 900);
      if (!acTimer) acTimer = setInterval(loadAccounts, 2500);
      return;
    }
    // 已接入可交互扫码的平台走网页流程；其余回落到「本机弹登录窗」的老路
    var j = await apiPost('/api/accounts/qr/start', { key: key });
    if (!j.ok) {
      if (j.fallback || (j.error || '').indexOf('还没接入') >= 0) {
        toast('该平台用本机登录窗；请在弹出的窗口里扫码');
        var j2 = await apiPost('/api/accounts/login', { key: key });
        if (!j2.ok) { toast('打开失败：' + (j2.error || '')); return; }
        if (j2.shared_with) toast(j2.note || '');
        setTimeout(loadAccounts, 900);
        if (!acTimer) acTimer = setInterval(loadAccounts, 2500);
        return;
      }
      toast('启动失败：' + (j.error || ''));
      return;
    }
    QR.key = key;
    QR.lastState = '';
    openModal('登录 · ' + (TAB_NAME[key] || key));
    $('#mFoot').innerHTML = '';
    var cancel = document.createElement('button');
    cancel.className = 'btn ghost small';
    cancel.textContent = '取消';
    cancel.addEventListener('click', async function () {
      stopQrPoll();
      QR.key = null;
      await apiPost('/api/accounts/qr/cancel', { key: key });
      closeModal();
      loadAccounts();
    });
    $('#mFoot').appendChild(cancel);
    // 「自己操作浏览器」：noVNC 打开的就是登录流程正在用的那台浏览器。
    // ★ 放在页脚而不是绑到某个状态上 —— 不管卡在哪一步（菜单点不动、收不到码、
    //   弹了刷脸），都能直接切过去手动做完，程序照样负责收登录态。
    if (NOVNC.port) {
      var vnc = document.createElement('a');
      vnc.className = 'btn ghost small';
      vnc.href = NOVNC.url();
      vnc.target = '_blank';
      vnc.rel = 'noopener';
      vnc.style.textDecoration = 'none';
      vnc.textContent = '打不开？直接操作浏览器';
      vnc.title = '在网页里直接操作容器内的浏览器，可过短信/刷脸等二次验证';
      $('#mFoot').appendChild(vnc);
    }
    qrPoll();
    stopQrPoll();
    QR.timer = setInterval(qrPoll, 2000);
  }

  async function qrPoll() {
    if (!QR.key) return;
    var j = await apiGet('/api/accounts/qr/status?key=' + encodeURIComponent(QR.key));
    if (!j.ok) return;
    renderQrState(j.status || {}, !!j.alive);
  }

  function renderQrState(st, alive) {
    var s = st.state || '';
    var key = QR.key;
    // 只重画 QR 图和状态文本；验证码输入框一旦画出来就不动，否则会打断用户输入
    var body = $('#mBody');
    if (s === 'verify_choice') {
      if (QR.lastState !== 'verify_choice') {
        body.innerHTML = '<div class="mhint">' + esc(st.note || '') + '</div><div id="qrOptions" class="setrow"></div>';
        (st.options || []).forEach(function (o) {
          var b = document.createElement('button');
          b.className = 'btn ghost small';
          b.textContent = o;
          if (o.indexOf('刷脸') >= 0) { b.disabled = true; b.title = '无人环境做不了刷脸，请选短信'; }
          b.addEventListener('click', function () {
            apiPost('/api/accounts/qr/cmd', { key: key, cmd: 'choose:' + o });
          });
          $('#qrOptions').appendChild(b);
        });
      }
    } else if (s === 'sms_input') {
      if (QR.lastState !== 'sms_input') {
        body.innerHTML = '<div class="mhint">' + esc(st.note || '请查看手机短信并输入验证码') + '</div>' +
          '<div class="setrow"><input id="qrCode" class="search" style="width:200px" placeholder="短信验证码">' +
          '<button id="qrSubmit" class="btn primary small">提交</button></div>';
        var sub = $('#qrSubmit');
        function submitCode() {
          var v = ($('#qrCode').value || '').trim();
          if (!v) { toast('请先输入验证码'); return; }
          apiPost('/api/accounts/qr/cmd', { key: key, cmd: 'code:' + v });
          $('#qrSubmit').disabled = true;
          $('#qrSubmit').textContent = '已提交…';
          setTimeout(function () { var b = $('#qrSubmit'); if (b) { b.disabled = false; b.textContent = '提交'; } }, 4000);
        }
        sub.addEventListener('click', submitCode);
        $('#qrCode').addEventListener('keydown', function (e) { if (e.key === 'Enter') submitCode(); });
      } else {
        var h = body.querySelector('.mhint');
        if (h) h.textContent = st.note || '';
      }
    } else if (s === 'done') {
      stopQrPoll();
      QR.key = null;
      body.innerHTML = '<div class="mhint"><b>登录成功</b>' +
        (st.account_name ? ('（' + esc(st.account_name) + '）') : '') + '，登录态已保存到本机。</div>';
      setTimeout(function () { closeModal(); loadAccounts(); }, 1500);
    } else if (s === 'error' || s === 'closed' || !alive) {
      stopQrPoll();
      QR.key = null;
      body.innerHTML = '<div class="mhint">' + esc(st.note || '登录已结束') + '</div>';
    } else if (s === 'qr') {
      if (QR.lastState !== 'qr') {
        // ★ has_qr=false：这个平台的二维码没能自动提取出来 —— 可能是画在 canvas /
        //   iframe 里，也可能它压根不是扫码登录。这时别放一张碎图，直接引导去
        //   noVNC 人工登录（能不能提取到二维码，跟能不能登录是两回事）。
        QR.qrHint = st.note || '';
        if (st.has_qr === false) {
          body.innerHTML = '<div class="mhint">' + esc(QR.qrHint) + '</div>' +
            (NOVNC.port
              ? '<div class="mhint">点左下角 <b>【打不开？直接操作浏览器】</b>，' +
                '在弹出的浏览器里完成登录就行；登完这里会自动变成「登录成功」。</div>'
              : '') +
            '<div class="mhint" id="qrNote"></div>' +
            '<div class="mhint" id="qrAlive"></div>';
        } else {
          body.innerHTML = '<div class="mhint">' + esc(QR.qrHint) +
            '（手机 App 右上角 + → 扫一扫）</div>' +
            '<div style="text-align:center;padding:8px 0"><img id="qrImg" alt="二维码" ' +
            'style="width:230px;height:230px;background:#fff;border-radius:10px;padding:8px"></div>' +
            (NOVNC.port
              ? '<div class="mhint">扫不了、或者这个平台不用二维码登录？' +
                '点左下角 <b>【直接操作浏览器】</b>，在里面想怎么登就怎么登。</div>'
              : '') +
            '<div class="mhint" id="qrNote"></div>' +
            '<div class="mhint" id="qrAlive"></div>';
        }
      }
      // ★ 心跳（2026-09-30 实测事故）：原来这一屏是死的，用户看不出后台还在不在盯，
      //   于是反复点「登录」—— 而每点一次都会把**正在盯着的那个进程**杀掉重开。
      //   这里把 worker 的轮次/已等秒数亮出来，让"它在干活"看得见。
      var al = $('#qrAlive');
      if (al) {
        al.textContent = st.rounds
          ? ('正在盯着登录页：第 ' + st.rounds + ' 轮 · 已等 ' + (st.elapsed || 0) +
             ' 秒 —— 登完会自动识别并关窗，不用重复点「登录」')
          : '';
      }
      var img = $('#qrImg');
      if (img) img.src = '/api/accounts/qr/qrcode?key=' + encodeURIComponent(key) + '&t=' + (st.qr_at || Date.now());
      var n = $('#qrNote');
      if (n) {
        // 渲染时正文已经显示了当时的 note（QR.qrHint）。只有它**后来变了**
        // （比如「二维码已刷新」）才需要在下面补一句 —— 否则会出现两行一模一样的字，
        // 看着像卡住了（实测被提过一次）。
        n.textContent = (st.note && st.note !== QR.qrHint) ? st.note : '';
      }
    } else {
      if (QR.lastState !== s) {
        body.innerHTML = '<div class="mhint">' + esc(st.note || '准备中…') + '</div>';
      }
    }
    QR.lastState = s;
  }

  // ★ 核验登录态（2026-09-29）：状态文件是"上次操作的记录"，不是事实 ——
  //   这里真开一次浏览器核一遍，并按实写回状态（后端跑 tools/probe_accounts.py --verify）
  async function acVerify(key) {
    toast(key ? '正在核验该平台（会真开一次浏览器，约 10~20 秒）…'
              : '正在核验全部平台（每个约 10~20 秒，可能要一两分钟）…');
    var j = await apiPost('/api/accounts/verify', key ? { key: key } : {});
    if (!j.ok) { toast(j.error || '核验没能启动'); return; }
    loadAccounts();
  }

  async function acClose(key) {
    var j = await apiPost('/api/accounts/close', { key: key });
    if (j.ok) { toast('已关闭登录窗'); loadAccounts(); }
  }

  // 兜底：自动探测没认出来时，用户自己确认
  async function acConfirm(key) {
    toast('正在确认并关闭登录窗…');
    var j = await apiPost('/api/accounts/confirm', { key: key });
    if (j.ok) {
      toast('已标记为登录；登录窗已关闭，浏览器已让给发布使用');
      loadAccounts();
    } else {
      toast('确认失败：' + (j.error || ''));
    }
  }

  /* ---------- 发布记录（S7） ---------- */
  var REC_STATUS_NAME = { pending: '排队中', running: '进行中', success: '成功', fail: '失败', manual: '需人工', canceled: '已取消' };

  async function loadRecords() {
    var j = await apiGet('/api/records');
    if (!j.ok) return;
    var kw = ($('#recKw').value || '').trim().toLowerCase();
    var fp = $('#recPlat').value;
    var fs2 = $('#recStatus').value;
    var rows = (j.records || []).filter(function (r) {
      if (fp && r.platform !== fp) return false;
      if (fs2 && r.status !== fs2) return false;
      if (kw && (r.title || '').toLowerCase().indexOf(kw) < 0) return false;
      return true;
    });
    var st = j.stats || {};
    $('#recStats').textContent = '共 ' + st.total + ' 条 · 成功 ' + st.success + ' · 失败 ' + st.fail + ' · 成功率 ' + st.rate + '%';
    var tbody = $('#recBody');
    tbody.innerHTML = '';
    rows.slice(0, 300).forEach(function (r) {
      var tr = document.createElement('tr');
      var badge = { success: 'ok', fail: 'bad', manual: 'warn', canceled: 'info', pending: 'info', running: 'info' }[r.status] || 'info';
      // ★ 模拟执行的任务要一眼看出（2026-09-30 事故）：它 status 就是 success，
      //   光看徽章和真发没区别 —— 必须额外打「模拟」标。
      var mockBadge = r.mock ? '<span class="badge warn">模拟</span> ' : '';
      var retry = (r.status === 'fail' || r.status === 'manual' || r.status === 'canceled')
        ? '<span class="retry rec-retry" data-id="' + r.id + '">重试</span>' : '';
      var shotLink = r.has_shot ? '<span class="retry rec-shot" data-id="' + r.id + '">截图</span>' : '';
      tr.innerHTML = '<td>' + esc((r.created_at || '').slice(5, 16)) + '</td>' +
        '<td>' + esc(TAB_NAME[r.platform] || r.platform) + '</td>' +
        '<td>' + esc((r.title || '').slice(0, 22)) + '</td>' +
        '<td>' + mockBadge + '<span class="badge ' + badge + '">' + (REC_STATUS_NAME[r.status] || r.status) + '</span></td>' +
        '<td>' + esc((r.error || r.step || '').slice(0, 28)) + '</td>' +
        '<td>' + retry + shotLink + '</td>';
      tbody.appendChild(tr);
    });
    $all('.rec-retry').forEach(function (el) {
      el.addEventListener('click', async function () {
        var r = await apiPost('/api/publish/retry', { id: parseInt(el.dataset.id, 10) });
        toast(r.ok ? '已重新排队' : ('重试失败：' + (r.error || '')));
        loadRecords();
      });
    });
    $all('.rec-shot').forEach(function (el) {
      el.addEventListener('click', function () {
        window.open('/api/publish/shot/' + el.dataset.id, '_blank');
      });
    });
  }

  /* ---------- 数据（S8） ---------- */
  function fmtNum(n) {
    n = n || 0;
    if (n >= 10000) return (n / 10000).toFixed(1) + '万';
    return String(n);
  }

  var dataTimer = null;

  async function loadData() {
    var j = await apiGet('/api/metrics');
    if (!j.ok) return;
    var supported = j.supported || [];
    var grid = $('#dataGrid');
    grid.innerHTML = '';
    var map = {};
    (j.platforms || []).forEach(function (p) { map[p.platform] = p; });
    PLAT_ORDER.forEach(function (k) {
      var p = map[k];
      var card = document.createElement('div');
      card.className = 'gcard';
      if (p) {
        // 真实抓取：作品合计 + 账号级（粉丝/总获赞）
        var acct = '';
        if (p.followers != null || p.total_likes != null) {
          acct = '<div class="gmeta" style="margin-top:6px">粉丝 ' + fmtNum(p.followers || 0) +
            ' · <b style="color:var(--text)">总获赞 ' + fmtNum(p.total_likes || 0) + '</b></div>';
        }
        var wk = (p.works_count != null) ? (' · ' + p.works_count + ' 条作品') : '';
        card.innerHTML = '<div class="gname">' + esc(TAB_NAME[k] || k) + '</div>' +
          '<div class="gmeta">作品播放合计（' + esc(p.day) + wk + '）</div>' +
          '<div class="stat">' + fmtNum(p.views) + '</div>' +
          '<div class="gmeta" style="margin-top:6px">点赞 ' + fmtNum(p.likes) + ' · 评论 ' + fmtNum(p.comments) + '</div>' +
          acct;
      } else if (supported.indexOf(k) < 0) {
        card.innerHTML = '<div class="gname">' + esc(TAB_NAME[k] || k) + '</div>' +
          '<div class="gmeta">作品播放合计</div><div class="stat">—</div>' +
          '<div class="gmeta" style="margin-top:6px">未接入真实抓取</div>';
      } else {
        card.innerHTML = '<div class="gname">' + esc(TAB_NAME[k] || k) + '</div>' +
          '<div class="gmeta">作品播放合计</div><div class="stat">—</div>' +
          '<div class="gmeta" style="margin-top:6px">暂无数据（点【立即抓取】）</div>';
      }
      grid.appendChild(card);
    });
    var t = j.total || {};
    $('#dataTotal').innerHTML = '总播放 <b style="color:var(--text)">' + fmtNum(t.views || 0) + '</b> · 总点赞 <b style="color:var(--text)">' + fmtNum(t.likes || 0) + '</b> · 总评论 <b style="color:var(--text)">' + fmtNum(t.comments || 0) + '</b>';
    $('#dataLast').textContent = j.last_fetch ? ('最近抓取：' + j.last_fetch.slice(5, 16)) : '尚未抓取';
    var f = j.fetch || {};
    $('#dataHint').textContent = f.running ? ('（' + (f.note || '抓取中…') + '）')
      : '（真实数据 · 来自各平台创作后台；未接入的平台不显示编造数字）';

    // 抓取进行中 → 轮询到结束
    if (f.running && !dataTimer) {
      dataTimer = setInterval(loadData, 2000);
    } else if (!f.running && dataTimer) {
      clearInterval(dataTimer);
      dataTimer = null;
    }
  }

  /* ---------- 设置 ---------- */
  async function loadSettings() {
    var j = await apiGet('/api/config');
    var c = (j && j.config) || {};
    var iv = c['发布间隔'] || [60, 180];
    $('#setPass').value = '';
    $('#passState').textContent = c['口令已设'] ? '已启用（加盐哈希）' : '未设置';
    $('#setIntMin').value = iv[0];
    $('#setIntMax').value = iv[1];
    $('#setDaily').value = c['单账号日更上限'] != null ? c['单账号日更上限'] : 2;
    $('#setReport').value = c['播报时间'] || '20:00';
    $('#setRange').value = c['数据统计范围'] != null ? c['数据统计范围'] : 1;
    $('#setNotifyLocal').checked = c['通知_本机'] !== false;
    $('#setNotifyWx').checked = c['通知_企微'] !== false;
    $('#setNotifyFs').checked = c['通知_飞书'] !== false;
    try {
      var d = await apiGet('/api/data-info');
      if (d.ok) {
        $('#dataInfoRow').textContent = '数据目录：' + d.dir + ' · 素材 ' + d.videos + ' 个 · 已用 ' + fmtSize(d.size);
      }
    } catch (e) { /* 忽略 */ }
    $('#verRow').textContent = '短视频发布台 v' + (APP_VER || '?') + ' · 全本地存储 · 数据不上传第三方';
    try {
      var ai = await apiGet('/api/ai/status');
      if (ai && ai.ok) {
        $('#aiState').textContent = ai.mock ? '本地模拟（调试）' : (ai.configured ? ('已接通 · ' + ai.model) : '未配置');
        $('#aiEnabled').checked = !!ai.enabled;
        // 地址/模型可以回显；密钥永远不回显（服务端根本不发），所以输入框恒空，
        // 于是「留空＝不改动」的语义才成立。
        $('#aiBase').value = ai.api_base || '';
        $('#aiModel').value = ai.model || '';
        $('#aiKey').value = '';
        $('#aiKey').placeholder = ai.configured ? '已配置（留空＝不改动）' : 'sk-…';
      } else {
        $('#aiState').textContent = '读取失败';
      }
    } catch (e) {
      $('#aiState').textContent = '读取失败';
    }
  }

  async function saveConfig(patch, okMsg) {
    var j = await apiPost('/api/config/save', { data: patch });
    if (j.ok) {
      toast(okMsg || '已保存');
      loadSettings();
    } else {
      toast('保存失败：' + (j.error || ''));
    }
  }

  function saveTempo() {
    var a = parseInt($('#setIntMin').value, 10) || 60;
    var b = parseInt($('#setIntMax').value, 10) || 180;
    saveConfig({ '发布间隔': [a, b] }, '已保存');
  }

  async function refreshLogs() {
    var j = await apiGet('/api/logs/tail?lines=200');
    $('#logBox').textContent = (j.ok ? (j.text || '（空）') : '读取失败');
  }

  /* ---------- 界面框架 ---------- */
  var LAMPS = null;
  function renderLamps(accounts) {
    var box = $('#lamps');
    if (!box) return;
    if (!LAMPS) {                 // 骨架只建一次，之后只改灯的颜色
      box.innerHTML = '';
      LAMPS = {};
      PLATS.forEach(function (p) {
        var st = document.createElement('span');
        st.className = 'st';
        var dot = document.createElement('span');
        dot.className = 'dot off';
        st.appendChild(dot);
        st.appendChild(document.createTextNode(p[1]));
        box.appendChild(st);
        LAMPS[p[0]] = { el: st, dot: dot, name: p[1] };
      });
    }
    if (!accounts) return;        // 没拿到数据时保持现状，别谎报未登录
    accounts.forEach(function (a) {
      var L = LAMPS[a.key];
      if (!L) return;
      var cls = a.status === 'on' ? 'ok' : (a.status === 'waiting' ? 'warn' : (a.status === 'error' ? 'bad' : 'off'));
      L.dot.className = 'dot ' + cls;
      L.el.title = L.name + '：' + (a.status === 'on' ? ('已登录' + (a.note ? '（' + a.note + '）' : ''))
        : (a.status === 'waiting' ? (a.note || '等待扫码')
          : (a.status === 'error' ? ('出错：' + (a.note || '')) : '未登录')));
    });
  }

  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    $all('#themeBox .topt').forEach(function (el) {
      el.classList.toggle('active', el.dataset.t === t);
    });
    localStorage.setItem('vp_theme', t);
  }

  function showPage(name) {
    $all('.nav-item').forEach(function (a) { a.classList.toggle('active', a.dataset.page === name); });
    $all('.page').forEach(function (s) { s.classList.toggle('on', s.id === 'page-' + name); });
    if (name === 'publish') initPublish();
    if (name === 'accounts') { loadAccounts(); }
    else if (acTimer) { clearInterval(acTimer); acTimer = null; }
    if (name === 'settings') loadSettings();
    if (name === 'records') loadRecords();
    if (name === 'data') loadData();
  }

  var APP_VER = '';   // 从 /api/health 读，避免界面上出现两个版本号
  var LOGIN_MODE = '';

  // noVNC：把容器里那台浏览器搬到网页上，让人能直接操作。
  // ★ 为什么留这个出口：平台的二次验证（短信/刷脸）弹窗结构说变就变，脚本追不上
  //   —— vendored 的 SAU 里那个按钮类名在现网已经失效了，它自己的做法就是
  //   「请在弹出的浏览器中手动输入」。有这条路，任何弹窗都能人工过；
  //   程序该自动的部分（检测登录成功、导出并灌入登录态）一点没少。
  // 端口由服务端下发，为 0 表示这次部署没开这条路，按钮就不显示。
  var NOVNC = {
    port: 0,
    url: function () {
      return location.protocol + '//' + location.hostname + ':' + NOVNC.port +
             '/vnc.html?autoconnect=1&resize=scale';
    }
  };

  document.addEventListener('DOMContentLoaded', function () {
    (async function () {
      try {
        var h = await apiGet('/api/health');
        LOGIN_MODE = h.login_mode || 'qr';
        // ★ 2026-10-06（安全审查 R2）：config.json 曾损坏 → 已备份并重置为默认值。
        //   必须让用户看见，否则他只会觉得"设置改了等于没改"（用户的设置确实丢了）。
        if (h['配置损坏']) {
          var cc = document.getElementById('cfgBrokenCard');
          var cn = document.getElementById('cfgBrokenNote');
          if (cc && cn) {
            cn.textContent = '原文件已备份为 ' + (h['配置备份'] || 'config.json.broken-*') +
              '，当前按默认值运行。请逐项检查下面的设置并重新保存。';
            cc.style.display = '';
          }
        }
        if (LOGIN_MODE === 'local') {
          $('#accountHint').textContent = '点【登录】后会打开本机浏览器窗口；扫码、短信验证、账号密码都在该窗口完成。登录成功并保存登录态后，窗口自动关闭、状态自动刷新；西瓜视频与抖音共用登录态。';
          $('#accountMode').textContent = '本机浏览器登录 · 登录态保存在本机';
        }
        if (h.mode === 'desktop') {
          $('#storageHint').textContent = '本机存储 · 视频 / 登录态 / 记录都在所选数据目录';
          $('#helpText').textContent = '首次使用：在素材库上传视频，再填写文案、逐个平台登录后发布。登录在本机浏览器中完成。数据保存在安装时选择的目录，更新只替换程序。把文件复制进素材目录不会自动加入素材库，请通过上传入口导入。启动窗口关闭后服务结束。';
          var labels = { 'no-source': '更新源不可达或未配置，使用现有版本', 'updated': '已更新',
            'same-version': '已是最新版本', 'up-to-date': '当前版本较新',
            'update-failed': '更新失败，使用现有版本', 'rolled-back': '新版启动失败，已恢复旧版' };
          var u = h.update || {};
          $('#verLine').textContent = (labels[u.result] || '启动检查完成') + (u.checked_at ? ' · 检查时间 ' + u.checked_at : '');
          $('#verLine').title = u.message || '';
        }
        if (h && h.version) {
          APP_VER = h.version;
          var chip = $('#appVer');
          if (chip) chip.textContent = 'v' + h.version;
        }
        if (h && h.novnc_port) NOVNC.port = h.novnc_port;
        if (h && h.display === false) {
          // 虚拟显示挂了 = 登录/发布/抓取全用不了，**但接口一切正常** —— 必须显式提示，
          // 否则用户只会看到「点了没反应」。实测踩过一次，找了好久。
          var c2 = $('#appVer');
          if (c2) {
            c2.textContent = 'v' + (h.version || '?') + ' · 浏览器不可用';
            c2.style.background = '#c0392b';
            c2.style.color = '#fff';
            c2.title = '服务器上的虚拟显示（Xvfb）没起来 —— 登录/发布/抓取都会失败，' +
                       '请管理员重启容器';
          }
          toast('⚠ 服务器浏览器不可用（虚拟显示未启动）：登录、发布、抓取都会失败');
        }
      } catch (e) { /* 读不到就保持占位，不瞎写版本号 */ }
    })();
    renderLamps();
    applyTheme(localStorage.getItem('vp_theme') || 'light');
    var name = (location.hash || '#library').replace('#', '') || 'library';
    showPage(name);
    loadVideos();

    // 发布台
    ['fTitle', 'fBody', 'fTopics'].forEach(function (id) {
      $('#' + id).addEventListener('input', function () {
        flushFields();
        refreshMarks();
        $('#draftState').textContent = '有未保存的修改';
      });
    });
    $('#saveDraftBtn').addEventListener('click', saveDraft);
    $('#genAllBtn').addEventListener('click', genAll);
    $('#videoSel').addEventListener('change', function () {
      renderVideoMeta();
      $('#draftState').textContent = '有未保存的修改';
    });
    // 发布台上的封面入口：复用素材库那个弹层（截帧 / 上传图片）
    $('#pubCoverBtn').addEventListener('click', function () {
      var v = STATE.videos.filter(function (x) {
        return String(x.id) === $('#videoSel').value; })[0];
      if (!v) { toast('先从上面选一条视频'); return; }
      openCoverModal(v);
    });
    $('#pubPickBtn').addEventListener('click', function () { $('#pubFileInput').click(); });
    $('#pubFileInput').addEventListener('change', function (e) {
      var fs = Array.prototype.slice.call(e.target.files);
      e.target.value = '';
      if (fs.length) uploadFromLocal(fs, true);
    });
    $('#pubStartBtn').addEventListener('click', startPublishFlow);
    $all('input[name=pubmode]').forEach(function (r) {
      r.addEventListener('change', function () {
        $('#pubScheduleAt').style.display = (document.querySelector('input[name=pubmode]:checked').value === 'schedule') ? '' : 'none';
      });
    });
    $('#acRefreshBtn').addEventListener('click', loadAccounts);
    $('#acVerifyAllBtn').addEventListener('click', function () { acVerify(''); });

    // 设置页
    $('#savePassBtn').addEventListener('click', function () {
      var v = $('#setPass').value.trim();
      saveConfig({ '口令': v }, v ? '口令已启用（下次打开需输入）' : '口令已清除');
      $('#setPass').value = '';
    });
    $('#setIntMin').addEventListener('change', saveTempo);
    $('#setIntMax').addEventListener('change', saveTempo);
    $('#setDaily').addEventListener('change', function () {
      saveConfig({ '单账号日更上限': parseInt($('#setDaily').value, 10) || 2 }, '已保存');
    });
    $('#setReport').addEventListener('change', function () {
      saveConfig({ '播报时间': $('#setReport').value || '20:00' }, '已保存');
    });
    $('#setRange').addEventListener('change', function () {
      var d = parseInt($('#setRange').value, 10);
      if (!d || d < 1) { d = 1; }
      if (d > 30) { d = 30; }
      $('#setRange').value = d;
      saveConfig({ '数据统计范围': d }, '已保存（下次抓取生效）');
    });
    ['setNotifyLocal', 'setNotifyWx', 'setNotifyFs'].forEach(function (id) {
      $('#' + id).addEventListener('change', function () {
        saveConfig({
          '通知_本机': $('#setNotifyLocal').checked,
          '通知_企微': $('#setNotifyWx').checked,
          '通知_飞书': $('#setNotifyFs').checked
        }, '已保存');
      });
    });
    $('#openFolderBtn').addEventListener('click', async function () {
      var j = await apiPost('/api/open-folder', {});
      toast(j.ok ? '已打开数据文件夹' : ('打开失败：' + (j.error || '')));
    });
    $('#hardenBtn').addEventListener('click', async function () {
      var j = await apiPost('/api/security/harden', {});
      toast(j.ok ? ('已加固：' + (j.detail || '')) : ('加固失败：' + (j.detail || j.error || '')));
    });
    $('#logRefreshBtn').addEventListener('click', refreshLogs);
    $('#aiEnabled').addEventListener('change', async function () {
      var j = await apiPost('/api/ai/save', { enabled: $('#aiEnabled').checked });
      toast(j.ok ? ($('#aiEnabled').checked ? 'AI 生成已开启' : '已切换为基础版生成') : '设置失败');
      if (!j.ok) loadSettings();
    });
    // 保存 AI 地址/模型/密钥。密钥留空＝不改动（服务端从不回传，输入框恒空）。
    $('#aiSaveBtn').addEventListener('click', async function () {
      var j = await apiPost('/api/ai/save', {
        api_base: $('#aiBase').value,
        model: $('#aiModel').value,
        api_key: $('#aiKey').value
      });
      if (!j.ok) { toast('保存失败'); return; }
      if (!j.configured) {
        // 地址/模型存下来了，但还没有密钥 → 「一键生成」仍会退回基础版，得说清楚
        toast('已保存，但还没填 API 密钥 → 仍走基础版');
      } else {
        toast('已保存：' + (j.model || '') + ' · 现在点「一键生成」会走 AI');
      }
      loadSettings();
    });

    // 记录页 / 数据页
    $('#recRefreshBtn').addEventListener('click', loadRecords);
    $('#recKw').addEventListener('input', loadRecords);
    $('#recPlat').addEventListener('change', loadRecords);
    $('#recStatus').addEventListener('change', loadRecords);
    $('#recExportBtn').addEventListener('click', function () {
      var a = document.createElement('a');
      a.href = '/api/publish/export';
      a.download = 'publish-records.csv';
      document.body.appendChild(a);
      a.click();
      a.remove();
    });
    $('#dataFetchBtn').addEventListener('click', async function () {
      var j = await apiPost('/api/metrics/fetch', {});
      toast(j.ok ? (j.note || '已开始抓取') : '抓取启动失败');
      loadData();   // 后台在跑，loadData 会自己轮询到结束
    });
    $('#dataReportBtn').addEventListener('click', async function () {
      // ★ 文案修正如实：这里**是真的发**，不是「测试模式仅记录」。
      //   旧版文案会让用户以为点了不会真发，于是随手点 → 真推到企微/飞书。
      if (!confirm('这会**立即把数据播报真实推送到**已绑定的企微 / 飞书。确定发吗？')) return;
      var j = await apiPost('/api/report/run-now', {});
      toast(j.ok ? '播报已推送（企微/飞书）' : '播报失败');
    });

    // 访问鉴权（服务端会话，Cookie 承载；不再是前端遮罩）
    (async function () {
      var j = await apiGet('/api/auth/status');
      // ★ 2026-10-06（安全审查 R2）：口令文件损坏时服务会一律 503。
      //   这里必须把原因说清楚，否则用户只会看到登录框反复拒绝、不知道该删哪个文件。
      if (j && j['配置损坏']) {
        var n = document.getElementById('authBrokenNote');
        if (n) {
          n.textContent = '口令文件 auth.json 已损坏，服务已停止放行。请在数据目录删除 auth.json 后重新设置访问口令。';
          n.style.display = '';
        }
        $('#lockMask').classList.add('show');
        return;
      }
      if (j && j.ok && j['口令已设'] && !j.logged_in) {
        $('#lockMask').classList.add('show');
      }
    })();
    $('#lockBtn').addEventListener('click', async function () {
      var r;
      try {
        r = await fetch('/api/auth/login', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pass: $('#lockInput').value })
        });
      } catch (e) {
        toast('连不上后台服务'); return;
      }
      var j = {};
      try { j = await r.json(); } catch (e) { /* 忽略 */ }
      if (r.ok && j.ok) {
        location.reload();   // 带上 Cookie 重新加载，各处数据才是已登录后取的
      } else {
        toast(j.error || '口令不对');
      }
    });
    $('#lockInput').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') $('#lockBtn').click();
    });

    // 弹层
    $('#mClose').addEventListener('click', closeModal);
    $('#mask').addEventListener('click', function (e) { if (e.target === this) closeModal(); });
    $('#imageInput').addEventListener('change', function (e) {
      var f = e.target.files[0];
      e.target.value = '';
      if (f) uploadCoverBlob(f, f.type || 'image/jpeg');
    });

    $all('#themeBox .topt').forEach(function (el) {
      el.addEventListener('click', function () { applyTheme(el.dataset.t); });
    });
    $all('.nav-item').forEach(function (a) {
      a.addEventListener('click', function () { location.hash = '#' + a.dataset.page; });
    });
    window.addEventListener('hashchange', function () {
      showPage((location.hash || '#library').replace('#', '') || 'library');
    });

    $('#uploadBtn').addEventListener('click', function () { $('#fileInput').click(); });
    $('#fileInput').addEventListener('change', function (e) {
      var fs = Array.prototype.slice.call(e.target.files);
      e.target.value = '';
      if (fs.length) doUpload(fs);
    });

    var dz = $('#dropzone');
    dz.addEventListener('click', function () { $('#fileInput').click(); });
    ['dragenter', 'dragover'].forEach(function (ev) {
      dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add('drag'); });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove('drag'); });
    });
    dz.addEventListener('drop', function (e) {
      var fs = Array.prototype.slice.call(e.dataTransfer.files);
      if (fs.length) doUpload(fs);
    });

    $('#searchBox').addEventListener('input', function (e) {
      STATE.search = e.target.value;
      renderVideos();
    });
  });
})();

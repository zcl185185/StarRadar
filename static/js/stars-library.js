(function () {
  'use strict';

  var TOKEN_KEY = 'starradar:gh_token';
  var META_KEY = 'starradar:stars_library_meta';
  var LAYOUT_KEY = 'starradar:stars_library_layout';
  var DEFAULT_LAYOUT = { columns: ['repo', 'description', 'language', 'stars', 'updated', 'created', 'tags', 'actions', 'owner'], visible: { repo: true, description: true, language: true, stars: true, updated: true, created: true, tags: true, actions: true, owner: false }, widths: { repo: 180, description: 250, language: 82, stars: 68, updated: 95, created: 95, tags: 150, actions: 76, owner: 130 }, ownerAvatar: true };
  var COLUMN_LABELS = { repo: '仓库', description: '项目简介', language: '语言', stars: 'Star 数', updated: '更新时间', created: '创建时间', tags: '标签', actions: '', owner: '所有者' };
  var state = { items: [], meta: {}, selected: '', checked: {}, layout: null, page: 1, hasNext: false, sort: 'starred_at', direction: 'desc', query: '', language: '', tag: '', favorites: false, untagged: false, archived: false, surface: 'stars' };
  var $ = function (selector) { return document.querySelector(selector); };
  var escapeHtml = function (value) { return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]; }); };
  var localGet = function (key, fallback) { try { var value = localStorage.getItem(key); return value ? JSON.parse(value) : fallback; } catch (e) { return fallback; } };
  var localSet = function (key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) {} };
  var dateText = function (value) { if (!value) return '—'; var date = new Date(value); return isNaN(date.getTime()) ? '—' : date.toLocaleDateString('zh-CN').replaceAll('/', '-'); };
  var countText = function (value) { var count = Number(value || 0); return count >= 1000 ? (count / 1000).toFixed(count >= 10000 ? 0 : 1) + 'k' : String(count); };
  var status = function (message) { $('#syncStatus').textContent = message; };
  var metaFor = function (name) { var item = state.meta[name] || {}; return { tags: Array.isArray(item.tags) ? item.tags : [], note: String(item.note || ''), favorite: !!item.favorite }; };
  var selectedItem = function () { return state.items.filter(function (item) { return item.full_name === state.selected; })[0] || null; };
  var checkedNames = function () { return Object.keys(state.checked).filter(function (name) { return state.checked[name]; }); };

  function loadLayout() {
    var stored = localGet(LAYOUT_KEY, {}); var layout = JSON.parse(JSON.stringify(DEFAULT_LAYOUT));
    if (stored && Array.isArray(stored.columns)) layout.columns = stored.columns.filter(function (key) { return Object.prototype.hasOwnProperty.call(COLUMN_LABELS, key); });
    Object.keys(layout.visible).forEach(function (key) { if (stored.visible && typeof stored.visible[key] === 'boolean') layout.visible[key] = stored.visible[key]; });
    Object.keys(layout.widths).forEach(function (key) { if (stored.widths && Number(stored.widths[key]) >= 55) layout.widths[key] = Math.min(420, Number(stored.widths[key])); });
    if (stored && typeof stored.ownerAvatar === 'boolean') layout.ownerAvatar = stored.ownerAvatar;
    if (layout.columns.indexOf('repo') === -1) layout.columns.unshift('repo');
    if (layout.columns.indexOf('owner') === -1) layout.columns.push('owner');
    return layout;
  }

  function saveLayout() { localSet(LAYOUT_KEY, state.layout); }
  function visibleColumns() { return state.layout.columns.filter(function (key) { return state.layout.visible[key]; }); }

  function metadataLoad() {
    return fetch('/api/starred-meta', { cache: 'no-store' }).then(function (response) { return response.json(); }).then(function (body) {
      if (!body.ok) throw new Error(body.error || 'metadata unavailable');
      state.meta = body.items || {}; localSet(META_KEY, state.meta);
    }).catch(function () { state.meta = localGet(META_KEY, {}); });
  }

  function metadataSave(name, next) {
    state.meta[name] = next; localSet(META_KEY, state.meta);
    return fetch('/api/starred-meta', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ full_name: name, tags: next.tags, note: next.note, favorite: next.favorite }) })
      .then(function (response) { return response.json(); }).then(function (body) { if (!body.ok) throw new Error(body.error || '保存失败'); return body.item; })
      .then(function (item) { state.meta[name] = item; localSet(META_KEY, state.meta); })
      .catch(function () { status('已只保存到当前浏览器'); });
  }

  function normalize(raw) {
    var repo = raw && raw.repo && typeof raw.repo === 'object' ? raw.repo : raw || {};
    return { full_name: repo.full_name || '', html_url: repo.html_url || '', description: repo.description || '暂无项目描述。', language: repo.language || '未标注', topics: Array.isArray(repo.topics) ? repo.topics : [], stars: repo.stars != null ? repo.stars : repo.stargazers_count, owner: (repo.owner && repo.owner.login) || String(repo.full_name || '').split('/')[0], owner_avatar: (repo.owner && repo.owner.avatar_url) || '', starred_at: raw.starred_at || repo.starred_at || '', pushed_at: repo.updated_at || repo.pushed_at || '', created_at: repo.created_at || '', archived: !!repo.archived };
  }

  function directStars(token, page) {
    var url = 'https://api.github.com/user/starred?per_page=100&page=' + (page || state.page) + '&sort=created&direction=desc';
    return fetch(url, { headers: { Authorization: 'token ' + token, Accept: 'application/vnd.github.star+json', 'X-GitHub-Api-Version': '2022-11-28' } }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) throw new Error((body && body.message) || 'GitHub 星标读取失败');
        return { items: body, hasNext: /rel="next"/.test(response.headers.get('Link') || '') };
      });
    });
  }

  function backendStars(page) {
    return fetch('/api/github/starred?page=' + (page || state.page) + '&per_page=100', { cache: 'no-store' }).then(function (response) { return response.json().then(function (body) { if (!response.ok || !body.ok) throw new Error(body.error || '请先在首页登录 GitHub'); return { items: body.items || [], hasNext: !!body.has_next }; }); });
  }

  function allStars(token) {
    var items = [], page = 1;
    function next() {
      var request = token ? directStars(token, page) : backendStars(page);
      return request.then(function (result) {
        items = items.concat(result.items || []);
        if (result.hasNext && page < 50) { page += 1; return next(); }
        return { items: items, hasNext: false };
      });
    }
    return next();
  }

  function loadStars() {
    var button = $('#syncStars'); button.disabled = true; status('正在同步 GitHub 星标…');
    var token = localStorage.getItem(TOKEN_KEY) || '';
    Promise.all([allStars(token), metadataLoad()]).then(function (results) {
      state.items = results[0].items.map(normalize).filter(function (item) { return item.full_name; });
      state.checked = {};
      state.hasNext = results[0].hasNext;
      if (state.selected && !selectedItem()) state.selected = '';
      status('已同步 · ' + new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }));
      render();
      maybeRefreshRecommendations();
    }).catch(function (error) {
      state.items = []; state.hasNext = false; status(error.message || '同步失败'); render();
    }).finally(function () { button.disabled = false; });
  }

  function filtered() {
    var query = state.query.trim().toLowerCase();
    var items = state.items.filter(function (item) {
      var meta = metaFor(item.full_name);
      var text = [item.full_name, item.description, item.language, item.topics.join(' '), meta.tags.join(' '), meta.note].join(' ').toLowerCase();
      return (!query || text.indexOf(query) !== -1) && (!state.language || item.language === state.language) && (!state.tag || meta.tags.indexOf(state.tag) !== -1) && (!state.favorites || meta.favorite) && (!state.untagged || !meta.tags.length) && (!state.archived || item.archived);
    });
    var key = state.sort;
    items.sort(function (a, b) {
      var first = key === 'name' ? a.full_name.toLowerCase() : key === 'stars' ? Number(a.stars || 0) : new Date(a[key] || 0).getTime();
      var second = key === 'name' ? b.full_name.toLowerCase() : key === 'stars' ? Number(b.stars || 0) : new Date(b[key] || 0).getTime();
      var result = typeof first === 'string' ? first.localeCompare(second) : first - second;
      return state.direction === 'asc' ? result : -result;
    });
    return items;
  }

  function renderFilters() {
    var langs = {}, tags = {};
    state.items.forEach(function (item) { langs[item.language] = (langs[item.language] || 0) + 1; metaFor(item.full_name).tags.forEach(function (tag) { tags[tag] = (tags[tag] || 0) + 1; }); });
    $('#languageTotal').textContent = Object.keys(langs).length;
    $('#tagTotal').textContent = Object.keys(tags).length;
    $('#languageFilters').innerHTML = Object.keys(langs).sort(function (a, b) { return langs[b] - langs[a]; }).map(function (language) { return '<button class="filter-option' + (state.language === language ? ' active' : '') + '" data-language="' + escapeHtml(language) + '"><b>' + escapeHtml(language) + '</b><em>' + langs[language] + '</em></button>'; }).join('') || '<span class="note-muted">同步后显示语言</span>';
    var tagQuery = $('#tagSearch') ? String($('#tagSearch').value || '').trim().toLowerCase() : '';
    $('#tagFilters').innerHTML = Object.keys(tags).filter(function (tag) { return !tagQuery || tag.toLowerCase().indexOf(tagQuery) !== -1; }).sort(function (a, b) { return tags[b] - tags[a]; }).map(function (tag) { return '<button class="filter-option' + (state.tag === tag ? ' active' : '') + '" data-tag="' + escapeHtml(tag) + '"><b>' + escapeHtml(tag) + '</b><em>' + tags[tag] + '</em></button>'; }).join('') || '<span class="note-muted">给项目添加标签后显示</span>';
    Array.prototype.forEach.call(document.querySelectorAll('[data-language]'), function (button) { button.addEventListener('click', function () { state.language = state.language === button.dataset.language ? '' : button.dataset.language; render(); }); });
    Array.prototype.forEach.call(document.querySelectorAll('[data-tag]'), function (button) { button.addEventListener('click', function () { state.tag = state.tag === button.dataset.tag ? '' : button.dataset.tag; render(); }); });
  }

  function renderTableHeader() {
    var columns = visibleColumns();
    var wrap = $('#tableWrap');
    var displayWidths = columns.map(function (key) { return Math.round(state.layout.widths[key]); });
    // 列宽保留用户设定的最小值；屏幕有剩余空间时分配给简介和标签列，避免右侧出现大片空白。
    var gapTotal = columns.length * 12, chrome = 16 + 24 + gapTotal;
    var total = displayWidths.reduce(function (sum, width) { return sum + width; }, 0) + chrome;
    var extra = Math.max(0, (wrap ? wrap.clientWidth : 0) - total);
    if (extra > 0) {
      var descriptionIndex = columns.indexOf('description');
      var tagsIndex = columns.indexOf('tags');
      var primary = descriptionIndex >= 0 ? descriptionIndex : (columns.indexOf('repo') >= 0 ? columns.indexOf('repo') : 0);
      var primaryExtra = tagsIndex >= 0 && tagsIndex !== primary ? Math.round(extra * 0.72) : extra;
      displayWidths[primary] += primaryExtra;
      if (tagsIndex >= 0 && tagsIndex !== primary) displayWidths[tagsIndex] += extra - primaryExtra;
    }
    wrap.style.setProperty('--library-columns', ['16px'].concat(displayWidths.map(function (width) { return width + 'px'; })).join(' '));
    $('#tableHead').innerHTML = '<span><input id="selectAllRows" type="checkbox" aria-label="选择当前筛选的所有项目" title="全选或取消选择当前结果"></span>' + columns.map(function (key) { return '<span class="layout-head-cell" draggable="true" data-column="' + key + '">' + COLUMN_LABELS[key] + '<i class="column-resizer" data-resize="' + key + '"></i></span>'; }).join('');
    $('#selectAllRows').addEventListener('change', function () { var checked = this.checked; filtered().forEach(function (item) { state.checked[item.full_name] = checked; }); render(); });
    var dragging = '';
    Array.prototype.forEach.call(document.querySelectorAll('.layout-head-cell'), function (cell) {
      cell.addEventListener('dragstart', function () { dragging = cell.dataset.column; cell.classList.add('dragging'); });
      cell.addEventListener('dragend', function () { dragging = ''; cell.classList.remove('dragging'); Array.prototype.forEach.call(document.querySelectorAll('.layout-head-cell'), function (other) { other.classList.remove('drop-target'); }); });
      cell.addEventListener('dragover', function (event) { if (dragging && dragging !== cell.dataset.column) { event.preventDefault(); cell.classList.add('drop-target'); } });
      cell.addEventListener('dragleave', function () { cell.classList.remove('drop-target'); });
      cell.addEventListener('drop', function (event) { event.preventDefault(); var target = cell.dataset.column; if (!dragging || dragging === target) return; var order = state.layout.columns; var from = order.indexOf(dragging); var to = order.indexOf(target); order.splice(from, 1); order.splice(to, 0, dragging); saveLayout(); render(); });
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-resize]'), function (handle) {
      handle.addEventListener('pointerdown', function (event) {
        event.preventDefault(); event.stopPropagation(); var key = handle.dataset.resize; var startX = event.clientX; var startWidth = state.layout.widths[key];
        function move(moveEvent) { state.layout.widths[key] = Math.max(55, Math.min(420, startWidth + moveEvent.clientX - startX)); renderTableHeader(); }
        function end() { document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', end); saveLayout(); renderRows(filtered()); }
        document.addEventListener('pointermove', move); document.addEventListener('pointerup', end);
      });
    });
  }

  function tableCell(item, key, meta) {
    if (key === 'repo') return '<span class="repo-cell">' + (item.owner_avatar ? '<img src="' + escapeHtml(item.owner_avatar) + '" alt="">' : '') + '<span class="repo-name">' + escapeHtml(item.full_name) + '</span></span>';
    if (key === 'description') return '<span class="repo-description">' + escapeHtml(item.description) + '</span>';
    if (key === 'language') return '<span class="row-lang">' + escapeHtml(item.language) + '</span>';
    if (key === 'stars') return '<span class="row-stars">' + countText(item.stars) + '</span>';
    if (key === 'updated') return '<span class="row-updated">' + dateText(item.pushed_at) + '</span>';
    if (key === 'created') return '<span class="row-updated">' + dateText(item.created_at) + '</span>';
    if (key === 'actions') return '<span class="row-actions"><button type="button" data-row-favorite="' + escapeHtml(item.full_name) + '" aria-label="' + (meta.favorite ? '取消收藏' : '收藏') + '" title="' + (meta.favorite ? '取消收藏' : '收藏') + '">' + (meta.favorite ? '★' : '☆') + '</button><button type="button" data-row-note="' + escapeHtml(item.full_name) + '" aria-label="打开笔记" title="打开仓库详情和笔记">▱</button></span>';
    // 头像只在“仓库”列展示；所有者列保持纯文本，避免同一行出现两个头像。
    if (key === 'owner') return '<span class="owner-cell"><span>' + escapeHtml(item.owner || '—') + '</span></span>';
    var tags = meta.tags.slice(0, 3).map(function (tag, index) { return '<span class="row-tag' + (index === 0 && meta.favorite ? ' strong' : '') + '">' + escapeHtml(tag) + '</span>'; }).join('');
    if (meta.tags.length > 3) tags += '<span class="row-tag-count">+' + (meta.tags.length - 3) + '</span>';
    return '<span class="tag-cluster">' + (tags || '<span class="row-tag-count">—</span>') + '</span>';
  }

  function renderRows(items) {
    renderTableHeader();
    if (!state.items.length) { $('#starsRows').innerHTML = '<div class="empty-list"><span>☆</span><b>还没有可展示的星标</b><p>请先回到航标 Beacon 首页登录 GitHub，然后点击“同步”。</p><a href="index.html">返回航标 Beacon 首页</a></div>'; return; }
    if (!items.length) { $('#starsRows').innerHTML = '<div class="empty-list"><span>⌕</span><b>没有符合条件的项目</b><p>换一个关键词，或清除当前筛选试试。</p></div>'; return; }
    $('#starsRows').innerHTML = items.map(function (item) {
      var meta = metaFor(item.full_name);
      return '<article class="star-row' + (state.selected === item.full_name ? ' selected' : '') + '" role="row" data-repo="' + escapeHtml(item.full_name) + '"><input class="row-select" type="checkbox" aria-label="选择 ' + escapeHtml(item.full_name) + '" title="选择此仓库进行批量操作"' + (state.checked[item.full_name] ? ' checked' : '') + '>' + visibleColumns().map(function (key) { return tableCell(item, key, meta); }).join('') + '</article>';
    }).join('');
    Array.prototype.forEach.call(document.querySelectorAll('.star-row'), function (row) {
      row.addEventListener('click', function () { state.selected = row.dataset.repo; renderRows(filtered()); renderDetail(); });
      var checkbox = row.querySelector('.row-select');
      checkbox.addEventListener('click', function (event) { event.stopPropagation(); });
      checkbox.addEventListener('change', function () { state.checked[row.dataset.repo] = checkbox.checked; renderBatchTools(); renderSelectAll(filtered()); });
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-row-favorite]'), function (button) {
      button.addEventListener('click', function (event) { event.stopPropagation(); var name = button.dataset.rowFavorite; var meta = metaFor(name); meta.favorite = !meta.favorite; metadataSave(name, meta).then(render); });
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-row-note]'), function (button) {
      button.addEventListener('click', function (event) { event.stopPropagation(); state.selected = button.dataset.rowNote; renderRows(filtered()); renderDetail(); });
    });
  }

  function renderDetail() {
    var item = selectedItem();
    var body = $('.library-body');
    var pane = $('#detailPane');
    if (body) body.classList.toggle('has-detail', !!item);
    if (pane) pane.classList.toggle('is-open', !!item);
    $('#detailEmpty').hidden = !!item; $('#detailContent').hidden = !item;
    if (!item) return;
    var meta = metaFor(item.full_name);
    var topics = item.topics.length ? item.topics.map(function (topic) { return '<span>' + escapeHtml(topic) + '</span>'; }).join('') : '<span>暂无 Topics</span>';
    var tags = meta.tags.map(function (tag) { return '<button data-remove-tag="' + escapeHtml(tag) + '">' + escapeHtml(tag) + ' ×</button>'; }).join('');
    $('#detailContent').className = 'detail-content';
    $('#detailContent').innerHTML = '<div class="detail-topline"><span>' + escapeHtml(item.language) + '</span><button class="detail-favorite' + (meta.favorite ? ' on' : '') + '" id="favoriteButton" title="' + (meta.favorite ? '取消收藏这个仓库' : '收藏这个仓库') + '">★ ' + (meta.favorite ? '已收藏' : '收藏') + '</button><button class="detail-close" id="detailClose" aria-label="关闭详情" title="关闭详情">×</button></div><a class="detail-title" href="' + escapeHtml(item.html_url) + '" target="_blank" rel="noopener">' + escapeHtml(item.full_name) + ' ↗</a><p class="detail-desc">' + escapeHtml(item.description) + '</p><a class="radar-analysis-link" href="index.html?libraryRepo=' + encodeURIComponent(item.full_name) + '" title="返回航标分析这个仓库">✦ 交给航标研究</a><a class="radar-analysis-link" href="learning.html?name=' + encodeURIComponent(item.full_name) + '&url=' + encodeURIComponent(item.html_url) + '&stack=' + encodeURIComponent(item.language) + '&summary=' + encodeURIComponent(item.description) + '" title="将此仓库加入学习档案">▤ 加入学习档案</a><div class="detail-stats"><span><b>★ ' + countText(item.stars) + '</b><small>GitHub 星标数</small></span><span><b>' + dateText(item.starred_at) + '</b><small>加星日期</small></span><span><b>' + dateText(item.pushed_at) + '</b><small>最近更新</small></span><span><b>' + escapeHtml(item.language) + '</b><small>主要语言</small></span></div><section class="detail-section"><h2>主题 Topics</h2><div class="topic-list">' + topics + '</div></section><section class="detail-section"><h2>我的标签</h2><div class="tag-editor-list">' + tags + '</div><div class="tag-add"><input id="newTag" maxlength="32" placeholder="添加标签，按 Enter"><button id="addTag" title="添加这个本地标签">添加</button></div></section><section class="detail-section"><h2>我的笔记</h2><div class="note-editor"><textarea id="projectNote" maxlength="1000" placeholder="为什么收藏它？下一步想做什么？">' + escapeHtml(meta.note) + '</textarea><button class="note-save" id="saveNote" title="保存这条本地笔记">保存笔记</button></div></section>';
    $('#detailClose').addEventListener('click', function () { state.selected = ''; render(); });
    $('#favoriteButton').addEventListener('click', function () { meta.favorite = !meta.favorite; metadataSave(item.full_name, meta).then(render); });
    function addTag() { var input = $('#newTag'); var tag = input.value.trim(); if (!tag || meta.tags.indexOf(tag) !== -1) return; meta.tags.push(tag); metadataSave(item.full_name, meta).then(render); }
    $('#addTag').addEventListener('click', addTag); $('#newTag').addEventListener('keydown', function (event) { if (event.key === 'Enter') { event.preventDefault(); addTag(); } });
    Array.prototype.forEach.call(document.querySelectorAll('[data-remove-tag]'), function (button) { button.addEventListener('click', function () { meta.tags = meta.tags.filter(function (tag) { return tag !== button.dataset.removeTag; }); metadataSave(item.full_name, meta).then(render); }); });
    $('#saveNote').addEventListener('click', function () { meta.note = $('#projectNote').value.trim(); metadataSave(item.full_name, meta).then(function () { status('笔记已保存'); render(); }); });
  }

  function renderActiveFilters() {
    var filters = []; if (state.query) filters.push('搜索：' + state.query); if (state.language) filters.push(state.language); if (state.tag) filters.push('#' + state.tag); if (state.favorites) filters.push('仅收藏'); if (state.untagged) filters.push('未标注'); if (state.archived) filters.push('已归档');
    $('#activeFilters').hidden = !filters.length; $('#activeFilters').innerHTML = filters.map(function (filter) { return '<span class="filter-chip">' + escapeHtml(filter) + '</span>'; }).join('') + '<button class="filter-reset" id="resetActive">清除全部筛选</button>';
    if ($('#resetActive')) $('#resetActive').addEventListener('click', clearFilters);
  }

  function render() {
    if (state.surface === 'recommendations') { renderRecommendationChrome(); return; }
    $('.library-body').classList.remove('recommendations-mode');
    $('.library-body').style.gridTemplateColumns = '';
    $('#recommendationView').hidden = true; if ($('#issuesView')) $('#issuesView').hidden = true; if ($('#followingView')) $('#followingView').hidden = true; $('#tableWrap').hidden = false;
    var visible = filtered();
    $('#libraryCount').textContent = state.items.length ? visible.length : '—';
    if ($('#libraryTotal')) $('#libraryTotal').textContent = state.items.length ? state.items.length : '—';
    if ($('#surfaceStarsCount')) $('#surfaceStarsCount').textContent = state.items.length ? state.items.length : '—';
    $('#filterFavorites').checked = state.favorites; $('#filterUntagged').checked = state.untagged; if ($('#filterArchived')) $('#filterArchived').checked = state.archived;
    $('#sortSelect').value = state.sort; $('#sortDirection').textContent = state.direction === 'desc' ? '↓' : '↑';
    renderActiveFilters(); renderFilters(); renderRows(visible); renderDetail(); renderBatchTools(); renderSelectAll(visible); renderOverview(visible);
    $('#pager').hidden = !state.items.length || !state.hasNext; $('#pageLabel').textContent = '第 ' + state.page + ' 页'; $('#prevPage').disabled = state.page <= 1; $('#nextPage').disabled = !state.hasNext;
  }

  function renderBatchTools() {
    var count = checkedNames().length;
    $('#batchTools').hidden = !count;
    $('#batchCount').textContent = '已选 ' + count + ' 项';
  }

  function renderSelectAll(items) {
    var box = $('#selectAllRows');
    if (!box) return;
    var names = items.map(function (item) { return item.full_name; });
    box.checked = names.length > 0 && names.every(function (name) { return !!state.checked[name]; });
    box.indeterminate = names.some(function (name) { return !!state.checked[name]; }) && !box.checked;
  }

  function renderOverview(visible) {
    var panel = $('#libraryOverview');
    if (!panel) return;
    // 数据较多时保持列表纯净；少量仓库时用真实统计填充视口下方的留白。
    if (!state.items.length || state.items.length > 12) { panel.hidden = true; panel.innerHTML = ''; return; }
    var languages = {}, tags = {}, favorites = 0;
    state.items.forEach(function (item) {
      languages[item.language] = (languages[item.language] || 0) + 1;
      var meta = metaFor(item.full_name); if (meta.favorite) favorites += 1;
      meta.tags.forEach(function (tag) { tags[tag] = (tags[tag] || 0) + 1; });
    });
    var topLanguages = Object.keys(languages).sort(function (a, b) { return languages[b] - languages[a]; }).slice(0, 3).map(function (name) { return escapeHtml(name) + ' ' + languages[name]; }).join(' · ');
    var topTags = Object.keys(tags).sort(function (a, b) { return tags[b] - tags[a]; }).slice(0, 3).map(function (name) { return '#' + escapeHtml(name); }).join(' ');
    panel.hidden = false;
    panel.innerHTML = '<div class="overview-heading"><div><span>LIBRARY SNAPSHOT</span><h2>你的星标库概览</h2></div><small>' + visible.length + ' 个当前结果</small></div><div class="overview-metrics"><div><b>' + state.items.length + '</b><span>全部仓库</span></div><div><b>' + Object.keys(languages).length + '</b><span>主要语言</span></div><div><b>' + Object.keys(tags).length + '</b><span>本地标签</span></div><div><b>' + favorites + '</b><span>收藏项目</span></div></div><div class="overview-foot"><span><b>语言</b> ' + (topLanguages || '尚未同步') + '</span><span><b>常用标签</b> ' + (topTags || '添加标签后显示') + '</span><span class="overview-hint">点击仓库可打开详情 · 使用 Cubby 整理整个库</span></div>';
  }

  function saveMany(names, change, completeMessage) {
    if (!names.length) return;
    status('正在整理 ' + names.length + ' 个项目…');
    Promise.all(names.map(function (name) { return metadataSave(name, change(metaFor(name), name)); })).then(function () { status(completeMessage); render(); });
  }

  function autoTagVisible() {
    var names = state.items.filter(function (item) { return item.topics && item.topics.length; }).map(function (item) { return item.full_name; });
    saveMany(names, function (meta, name) {
      var item = state.items.filter(function (candidate) { return candidate.full_name === name; })[0];
      (item.topics || []).slice(0, 6).forEach(function (topic) { if (meta.tags.indexOf(topic) === -1 && meta.tags.length < 12) meta.tags.push(topic); });
      return meta;
    }, '已根据 Topics 整理 ' + names.length + ' 个项目');
  }

  function githubRequest(path, options) {
    var token = localStorage.getItem(TOKEN_KEY) || '';
    if (!token) return Promise.reject(new Error('请先在 StarRadar 首页登录 GitHub'));
    var request = options || {}; request.headers = Object.assign({ Authorization: 'token ' + token, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' }, request.headers || {});
    return fetch('https://api.github.com' + path, request)
      .then(function (response) { if (response.status === 204) return null; return response.json().then(function (body) { if (!response.ok) throw new Error((body && body.message) || 'GitHub 请求失败'); return body; }); });
  }

  function openDrawer(kicker, title, html) {
    $('#drawerKicker').textContent = kicker;
    $('#drawerTitle').textContent = title;
    $('#drawerBody').innerHTML = html;
    $('#workspaceDrawer').hidden = false;
  }

  function closeDrawer() { $('#workspaceDrawer').hidden = true; }

  function setSurfaceTab(surface) {
    Array.prototype.forEach.call(document.querySelectorAll('.surface-tab'), function (tab) {
      var active = tab.dataset.surface === surface;
      tab.classList.toggle('active', active);
      if (active) tab.setAttribute('aria-current', 'page'); else tab.removeAttribute('aria-current');
    });
  }

  function renderWatch() {
    setSurfaceTab('watch');
    openDrawer('动态通知 · GitHub', '动态通知', '<div class="surface-empty"><span>◌</span><h3>动态通知尚未接入</h3><p>当前版本已经完成工作台布局，但还没有可靠的通知同步接口。为避免展示伪造数据，这里暂不生成空的动态列表。</p><a class="drawer-action" href="https://github.com/notifications" target="_blank" rel="noopener">打开 GitHub 通知 ↗</a><button class="drawer-link" id="watchBack">返回星标</button></div>');
    if ($('#watchBack')) $('#watchBack').addEventListener('click', function () { setSurfaceTab('stars'); closeDrawer(); });
  }

  function topSignals() {
    var topics = {}, languages = {};
    state.items.forEach(function (item) {
      languages[item.language] = (languages[item.language] || 0) + 1;
      item.topics.forEach(function (topic) { topics[topic] = (topics[topic] || 0) + 1; });
    });
    return { topic: Object.keys(topics).sort(function (a, b) { return topics[b] - topics[a]; })[0] || '', language: Object.keys(languages).sort(function (a, b) { return languages[b] - languages[a]; })[0] || '' };
  }

  function recommendationWords(value) {
    return String(value || '').toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]+/g, ' ').split(/\s+/).filter(function (word) { return word.length >= 3 && !/^(the|and|for|with|from|this|that|github|project|tool|app|using)$/.test(word); }).slice(0, 6);
  }

  function recommendationSeeds() {
    var languageCount = {}, ownerCount = {}, selected = [], candidates = state.items.slice().sort(function (a, b) { return new Date(b.starred_at || 0).getTime() - new Date(a.starred_at || 0).getTime(); });
    candidates.forEach(function (item) {
      if (selected.length >= 12) return;
      var language = item.language || '', owner = item.owner || '';
      if ((language && (languageCount[language] || 0) >= 3) || (owner && (ownerCount[owner] || 0) >= 3)) return;
      selected.push(item); if (language) languageCount[language] = (languageCount[language] || 0) + 1; if (owner) ownerCount[owner] = (ownerCount[owner] || 0) + 1;
    });
    candidates.forEach(function (item) { if (selected.length < 12 && selected.indexOf(item) < 0) selected.push(item); });
    return selected;
  }

  function recommendationPlan(seeds) {
    var queries = [], seen = {};
    function add(query) { if (query && !seen[query] && queries.length < 6) { seen[query] = true; queries.push(query); } }
    var topicCount = {}, languageCount = {}, ownerCount = {}, keywords = [];
    seeds.forEach(function (seed) { (seed.topics || []).slice(0, 4).forEach(function (topic) { topicCount[topic] = (topicCount[topic] || 0) + 1; }); if (seed.language && seed.language !== '未标注') languageCount[seed.language] = (languageCount[seed.language] || 0) + 1; if (seed.owner) ownerCount[seed.owner] = (ownerCount[seed.owner] || 0) + 1; recommendationWords(seed.full_name.replace('/', ' ')).forEach(function (word) { if (keywords.indexOf(word) < 0) keywords.push(word); }); recommendationWords(seed.description).slice(0, 2).forEach(function (word) { if (keywords.indexOf(word) < 0) keywords.push(word); }); });
    Object.keys(topicCount).sort(function (a, b) { return topicCount[b] - topicCount[a]; }).slice(0, 2).forEach(function (topic) { add('topic:' + encodeURIComponent(topic) + '+stars:>=10+archived:false+fork:false'); });
    Object.keys(languageCount).sort(function (a, b) { return languageCount[b] - languageCount[a]; }).slice(0, 2).forEach(function (language) { add('language:' + encodeURIComponent(language) + '+stars:>=25+archived:false+fork:false'); });
    Object.keys(ownerCount).sort(function (a, b) { return ownerCount[b] - ownerCount[a]; }).slice(0, 1).forEach(function (owner) { add('user:' + encodeURIComponent(owner) + '+stars:>=5+archived:false+fork:false'); });
    keywords.slice(0, 2).forEach(function (word) { add(encodeURIComponent(word) + '+in:name,description+stars:>=10+archived:false+fork:false'); });
    return queries;
  }

  function scoreRecommendation(candidate, seeds) {
    var candidateTopics = (candidate.topics || []).map(function (topic) { return String(topic).toLowerCase(); }), candidateName = String(candidate.full_name || '').toLowerCase(), candidateDescription = String(candidate.description || '').toLowerCase(), best = { score: 0, reasons: [], seed: '' };
    seeds.forEach(function (seed) {
      var common = (seed.topics || []).filter(function (topic) { return candidateTopics.indexOf(String(topic).toLowerCase()) >= 0; }), words = recommendationWords(seed.full_name.replace('/', ' ')).concat(recommendationWords(seed.description));
      var keywordMatches = words.filter(function (word) { return candidateName.indexOf(word.toLowerCase()) >= 0 || candidateDescription.indexOf(word.toLowerCase()) >= 0; }).filter(function (word, index, all) { return all.indexOf(word) === index; });
      var base = 0, reasons = [];
      if (common.length) { base = 80 + 12 * Math.min(common.length, 3); reasons.push('共同主题：' + common.slice(0, 3).join('、')); }
      if (seed.language && seed.language === candidate.language) { base = Math.max(base, 50); reasons.push('相同语言：' + seed.language); }
      if (seed.owner && seed.owner === ((candidate.owner && candidate.owner.login) || candidate.full_name.split('/')[0])) { base = Math.max(base, 38); reasons.push('同一作者/组织'); }
      if (keywordMatches.length) { base = Math.max(base, 30 + 2 * Math.min(keywordMatches.length, 3)); reasons.push('名称或简介相关'); }
      var stars = Number(candidate.stargazers_count || 0), hot = Math.min(24, 6 * Math.log10(stars + 1)), pushed = new Date(candidate.pushed_at || candidate.updated_at || 0).getTime(), days = pushed ? Math.max(0, (Date.now() - pushed) / 86400000) : 9999, fresh = Math.max(0, 18 - 3 * Math.log2(days + 1)), score = base + hot + fresh;
      if (score > best.score) best = { score: score, reasons: reasons, seed: seed.full_name };
    });
    return best;
  }

  var RECOMMEND_CACHE_PREFIX = 'starradar:recommendations:v1:';
  var recommendationTimer = null;
  function recommendationAccount() { var user = localGet('starradar:gh_user', null); return user && user.login ? String(user.login).toLowerCase() : 'local'; }
  function recommendationCacheKey() { return RECOMMEND_CACHE_PREFIX + recommendationAccount(); }
  function recommendationCacheLoad() { return localGet(recommendationCacheKey(), null); }
  function recommendationCacheSave(snapshot) { localSet(recommendationCacheKey(), snapshot); }
  function recommendationStatusText(snapshot) { if (!snapshot) return '尚未生成推荐。'; if (snapshot.status === 'cooldown') return 'GitHub Search 暂时需要冷却，稍后可重试。'; if (snapshot.status === 'stale') return '上次刷新失败，正在显示最近一次成功的推荐。'; return '已缓存 ' + (snapshot.items || []).length + ' 条推荐 · ' + dateText(snapshot.generated_at); }
  function renderRecommendationCards(items) {
    var results = $('#drawerResults'); if (!results) return;
    results.innerHTML = (items || []).map(function (item) { var reason = item._recommendation || {}, review = item._review || {}, research = reason.sharedResearch || item.topics || []; return '<article class="recommendation-card"><a href="' + escapeHtml(item.html_url) + '" target="_blank" rel="noopener">' + escapeHtml(item.full_name) + ' ↗</a><p class="recommendation-summary">' + escapeHtml(item.summary_zh || item.description || '暂无中文简介。') + '</p><p class="recommendation-novelty">' + escapeHtml(item.novelty || '新颖点：已根据公开主题和技术栈直接提炼。') + '</p><div class="recommendation-research"><b>研究方向：</b>' + escapeHtml(research.slice(0, 5).join('、') || '以项目简介为准') + '</div><small>★ ' + countText(item.stargazers_count) + ' · ' + escapeHtml(item.language || '未标注') + ' · ' + (item.topics || []).slice(0, 3).map(escapeHtml).join(' · ') + '</small><div class="recommendation-reason"><b>为什么推荐：</b>' + escapeHtml((reason.reasons || []).join('；') || '与你关注的技术方向相关') + (reason.seed ? '（参考：' + escapeHtml(reason.seed) + '）' : '') + '</div><div class="recommendation-review"><select data-review-status="' + escapeHtml(item.full_name) + '" title="记录查看状态"><option value=""' + (!review.status ? ' selected' : '') + '>记录状态</option><option value="seen"' + (review.status === 'seen' ? ' selected' : '') + '>已看过</option><option value="worth_research"' + (review.status === 'worth_research' ? ' selected' : '') + '>值得研究</option><option value="dismissed"' + (review.status === 'dismissed' ? ' selected' : '') + '>暂不考虑</option></select><input data-review-note="' + escapeHtml(item.full_name) + '" value="' + escapeHtml(review.note || '') + '" placeholder="写一条笔记"><button data-review-save="' + escapeHtml(item.full_name) + '" title="保存查看状态和笔记">保存</button></div><button class="recommendation-star" data-recommendation-star="' + escapeHtml(item.full_name) + '" title="在 GitHub 上 Star 并加入我的星标">☆ 加入我的星标</button></article>'; }).join('') || '<div class="recommendation-empty"><span>⌁</span><p>暂时没有可推荐的仓库。</p></div>';
    Array.prototype.forEach.call(document.querySelectorAll('[data-review-save]'), function (button) { button.addEventListener('click', function () { var name = button.dataset.reviewSave, select = document.querySelector('[data-review-status="' + CSS.escape(name) + '"]'), input = document.querySelector('[data-review-note="' + CSS.escape(name) + '"]'); fetch('/api/recommendations/reviews', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ full_name: name, status: select ? select.value : 'seen', note: input ? input.value : '' }) }).then(function () { button.textContent = '已保存'; setTimeout(function () { button.textContent = '保存'; }, 1200); }); }); });
    Array.prototype.forEach.call(document.querySelectorAll('[data-recommendation-star]'), function (starButton) { starButton.addEventListener('click', function () { var name = starButton.dataset.recommendationStar, parts = name.split('/'); starButton.disabled = true; githubRequest('/user/starred/' + parts.map(function (part) { return encodeURIComponent(part); }).join('/'), { method: 'PUT' }).then(function () { return githubRequest('/repos/' + parts.map(function (part) { return encodeURIComponent(part); }).join('/')); }).then(function (repo) { state.items.unshift(normalize({ repo: repo, starred_at: new Date().toISOString() })); return fetch('/api/recommendations?full_name=' + encodeURIComponent(name), { method: 'DELETE' }).catch(function () {}).then(function () { var snapshot = recommendationCacheLoad() || { items: [] }; snapshot.items = (snapshot.items || []).filter(function (item) { return item.full_name !== name; }); recommendationCacheSave(snapshot); var card = starButton.closest('.recommendation-card'); if (card) card.remove(); if ($('#drawerStatus')) $('#drawerStatus').textContent = '已加入星标，推荐列表已更新。'; }); }).catch(function (error) { starButton.disabled = false; starButton.textContent = error.message || '加入失败'; }); }); });
  }
  function refreshRecommendations(force) {
    var token = localStorage.getItem(TOKEN_KEY) || '', snapshot = recommendationCacheLoad(), now = Date.now();
    /* 推荐由本地 Python 服务读取 gh_token.json；浏览器无需重复持有 Token。 */
    if (snapshot && snapshot.nextAllowedAt && snapshot.nextAllowedAt > now) { if ($('#drawerStatus')) $('#drawerStatus').textContent = recommendationStatusText({ status: 'cooldown', items: snapshot.items || [] }); return Promise.resolve(snapshot); }
    if (!state.items.length) return Promise.resolve(snapshot);
    if ($('#drawerStatus')) $('#drawerStatus').textContent = '正在根据你的星标生成推荐…';
    return fetch('/api/recommendations/refresh' + (force ? '?force=1' : ''), { method: 'POST', cache: 'no-store' }).then(function (response) { return response.json().then(function (body) { if (!response.ok || !body.ok) throw new Error(body.error || '推荐刷新失败'); return body.snapshot; }); }).then(function (next) { recommendationCacheSave(next); renderRecommendationCards(next.items || []); if ($('#drawerStatus')) $('#drawerStatus').textContent = recommendationStatusText(next); return next; }).catch(function (error) { if ($('#drawerStatus')) $('#drawerStatus').textContent = error.message || '推荐刷新失败'; return snapshot; });
    /* 浏览器端旧逻辑保留在历史版本中，推荐请求统一由 Python 后台完成。 */
    /*
    var seeds = recommendationSeeds(), queries = recommendationPlan(seeds); if (!seeds.length) return Promise.resolve(snapshot);
    if ($('#drawerStatus')) $('#drawerStatus').textContent = '正在根据 ' + seeds.length + ' 个 Star 仓库搜索候选…';
    var starred = {}; state.items.forEach(function (item) { starred[item.full_name] = true; });
    var byName = {}, completed = 0;
    function fetchNext(index) { if (index >= queries.length) return Promise.resolve(); return githubRequest('/search/repositories?q=' + queries[index] + '&sort=stars&order=desc&per_page=100&page=1').then(function (body) { (body.items || []).forEach(function (item) { if (item.full_name && !byName[item.full_name]) byName[item.full_name] = item; }); completed += 1; if ($('#drawerStatus')) $('#drawerStatus').textContent = '已完成 ' + completed + '/' + queries.length + ' 条搜索…'; return fetchNext(index + 1); }); }
    return fetchNext(0).then(function () { var result = Object.keys(byName).map(function (name) { var item = byName[name]; if (starred[name] || item.archived || item.fork) return null; var relevance = scoreRecommendation(item, seeds); if (!relevance.score) return null; item._recommendation = relevance; return item; }).filter(Boolean).sort(function (a, b) { return b._recommendation.score - a._recommendation.score || Number(b.stargazers_count || 0) - Number(a.stargazers_count || 0) || a.full_name.localeCompare(b.full_name); }).slice(0, 20); var next = { account: recommendationAccount(), status: 'fresh', generated_at: new Date().toISOString(), attempted_at: new Date().toISOString(), seeds: seeds.length, queries: queries.length, items: result, nextAllowedAt: 0 }; recommendationCacheSave(next); renderRecommendationCards(result); if ($('#drawerStatus')) $('#drawerStatus').textContent = recommendationStatusText(next); return next; }).catch(function (error) { var failed = snapshot || { account: recommendationAccount(), items: [] }; failed.status = 'stale'; failed.error = error.message || '推荐刷新失败'; failed.attempted_at = new Date().toISOString(); failed.nextAllowedAt = Date.now() + 15 * 60 * 1000; recommendationCacheSave(failed); if ($('#drawerStatus')) $('#drawerStatus').textContent = failed.items && failed.items.length ? '刷新失败，保留上次成功的推荐：' + failed.error : failed.error; return failed; }); */
  }
  function recommendationNextEight() { var next = new Date(); next.setHours(8, 0, 0, 0); if (next.getTime() <= Date.now()) next.setDate(next.getDate() + 1); return next.getTime(); }
  function scheduleRecommendationRefresh() { if (recommendationTimer) clearTimeout(recommendationTimer); recommendationTimer = setTimeout(function () { var snapshot = recommendationCacheLoad(), today = new Date().toISOString().slice(0, 10); if (state.items.length && (!snapshot || (snapshot.generated_at || '').slice(0, 10) < today)) refreshRecommendations(false); scheduleRecommendationRefresh(); }, Math.max(1000, recommendationNextEight() - Date.now())); }
  function maybeRefreshRecommendations() { if (!state.items.length) return; /* 本地日期边界由 Python 后台调度器负责，页面只读取快照。 */ fetch('/api/recommendations', { cache: 'no-store' }).then(function (response) { return response.json(); }).then(function (body) { if (body && body.ok && body.snapshot) { recommendationCacheSave(body.snapshot); } }).catch(function () {}); }
  function renderForYou() {
    state.surface = 'recommendations'; state.selected = ''; setSurfaceTab('recommendations'); closeDrawer(); renderRecommendationChrome();
    var snapshot = recommendationCacheLoad(); if (snapshot && snapshot.items) { renderRecommendationCards(snapshot.items); $('#drawerStatus').textContent = recommendationStatusText(snapshot); }
    fetch('/api/recommendations', { cache: 'no-store' }).then(function (response) { return response.json(); }).then(function (body) { if (!body || !body.ok) return; var remote = body.snapshot || {}; var reviews = body.reviews || {}; (remote.items || []).forEach(function (item) { item._review = reviews[item.full_name] || {}; }); recommendationCacheSave(remote); renderRecommendationCards(remote.items || []); if ($('#drawerStatus')) $('#drawerStatus').textContent = recommendationStatusText(remote); }).catch(function () {});
    var loadButton = $('#loadForYou'); if (!loadButton || loadButton.dataset.bound) return; loadButton.dataset.bound = '1'; loadButton.addEventListener('click', function () { var button = this; button.disabled = true; button.textContent = '正在生成…'; if ($('#drawerStatus')) $('#drawerStatus').textContent = '正在搜索并整理 20 条推荐，请稍候…'; refreshRecommendations(true).then(function (snapshot) { if (!snapshot || snapshot.status === 'stale') return; if ($('#drawerStatus')) $('#drawerStatus').textContent = '推荐已更新，共 ' + (snapshot.items || []).length + ' 条。'; }).finally(function () { button.disabled = false; button.textContent = '换一批推荐'; }); });
    if (!snapshot || !snapshot.items || !snapshot.items.length) refreshRecommendations(false);
  }

  function renderRecommendationChrome() {
    var view = $('#recommendationView'); if (!view) return;
    $('.library-body').classList.add('recommendations-mode');
    $('#tableWrap').hidden = true; $('#libraryOverview').hidden = true; $('#pager').hidden = true; if ($('#issuesView')) $('#issuesView').hidden = true; if ($('#followingView')) $('#followingView').hidden = true;
    if (!view.innerHTML) view.innerHTML = '<div class="recommendation-header"><div><p class="recommendation-kicker">个性化发现</p><h1>为你推荐</h1><p>根据你的星标偏好，发现尚未收藏的公开仓库。推荐依据会显示在每张卡片上，方便你判断是否值得加入。</p></div><button class="drawer-action" id="loadForYou" title="重新搜索一批推荐仓库">换一批推荐</button></div><div class="recommendation-status" id="drawerStatus">点击“换一批推荐”开始搜索。</div><div class="recommendation-results" id="drawerResults"></div>';
    view.hidden = false;
  }

  function renderFollowing() {
    setSurfaceTab('following'); closeDrawer(); var body = $('.library-body'); body.classList.add('recommendations-mode'); body.style.gridTemplateColumns = 'minmax(0, 1fr)'; $('#tableWrap').hidden = true; $('#libraryOverview').hidden = true; $('#pager').hidden = true; $('#recommendationView').hidden = true; $('#issuesView').hidden = true;
    var view = $('#followingView'); if (!view) return; view.hidden = false;
    var cacheKey = 'starradar:following:v1:' + recommendationAccount(), cache = localGet(cacheKey, null);
    view.innerHTML = '<div class="following-shell"><aside class="following-nav"><p class="recommendation-kicker">社交动态</p><h1>关注动态</h1><p class="following-desc">查看你关注的人最近公开关注了哪些项目。</p><nav><button class="following-tab active" data-following-tab="feed">动态</button><button class="following-tab" data-following-tab="projects">项目</button><button class="following-tab" data-following-tab="following">关注的人</button><button class="following-tab" data-following-tab="me">我的动态</button></nav></aside><section class="following-main"><div class="following-toolbar"><input id="followingSearch" type="search" placeholder="搜索人员或仓库…"><select id="followingPeriod"><option value="30">最近 30 天</option><option value="7">最近 7 天</option><option value="90">最近 90 天</option></select><button class="drawer-action" id="loadFollowing" title="刷新关注动态">刷新</button></div><div class="recommendation-status" id="followingStatus">' + (cache ? '正在显示上次保存的动态。' : '点击“刷新”读取关注动态。') + '</div><div id="followingResults" class="following-results"></div></section></div>';
    var navLabels = { feed: '关注者的动态', projects: '关注者参与的项目', following: '我关注的人', me: '我的 Star 动态' }; Object.keys(navLabels).forEach(function (key) { var nav = view.querySelector('[data-following-tab="' + key + '"]'); if (nav) nav.textContent = navLabels[key]; });
    var activeTab = 'feed', data = cache || { feed: [], projects: [], following: [], me: [] };
    function renderTab() {
      var query = ($('#followingSearch').value || '').toLowerCase(), period = Number($('#followingPeriod').value || 30), cutoff = Date.now() - period * 86400000, rows = data[activeTab] || [];
      $('#followingResults').className = 'following-results' + (activeTab === 'following' ? ' following-person-grid' : '');
      rows = rows.filter(function (row) { return (!row.ts || new Date(row.ts).getTime() >= cutoff) && (!query || JSON.stringify(row).toLowerCase().indexOf(query) >= 0); });
      if (activeTab === 'following') $('#followingResults').innerHTML = rows.map(function (p) { return '<article class="following-person"><img src="' + escapeHtml(p.avatar_url || '') + '"><div><a href="https://github.com/' + encodeURIComponent(p.login) + '" target="_blank">' + escapeHtml(p.login) + '</a><p>' + escapeHtml(p.bio || '暂无个人简介。') + '</p><button class="following-repos-button" data-person-repos="' + escapeHtml(p.login) + '">查看当前公开仓库</button></div></article>'; }).join('') || '<div class="surface-empty"><h3>暂无关注用户</h3></div>';
      else $('#followingResults').innerHTML = rows.map(function (row) { return '<article class="following-activity"><img src="' + escapeHtml(row.actor_avatar || '') + '"><div class="following-activity-body"><a href="' + escapeHtml(row.html_url || '#') + '" target="_blank"><b>' + escapeHtml(row.full_name || '') + '</b> ↗</a><p>' + escapeHtml(row.description || '暂无项目简介。') + '</p><small>' + escapeHtml(row.actor || '你') + ' · ' + escapeHtml(row.action || '公开活动') + ' · ★ ' + countText(row.stars) + ' · ' + escapeHtml(row.relative || '') + '</small></div></article>'; }).join('') || '<div class="surface-empty"><h3>暂无动态</h3><p>调整时间范围或点击刷新获取最新内容。</p></div>';
    }
    Array.prototype.forEach.call(document.querySelectorAll('[data-following-tab]'), function (tab) { tab.addEventListener('click', function () { activeTab = tab.dataset.followingTab; document.querySelectorAll('[data-following-tab]').forEach(function (x) { x.classList.toggle('active', x === tab); }); renderTab(); }); });
    $('#followingSearch').addEventListener('input', renderTab); $('#followingPeriod').addEventListener('change', renderTab);
    $('#followingResults').addEventListener('click', function (event) { var button = event.target.closest('[data-person-repos]'); if (!button) return; var login = button.dataset.personRepos; button.disabled = true; $('#followingStatus').textContent = '正在读取 ' + login + ' 的公开仓库…'; githubRequest('/users/' + encodeURIComponent(login) + '/repos?type=all&sort=updated&direction=desc&per_page=50').then(function (repos) { var visibleRepos = (repos || []).filter(function (repo) { return !repo.private; }); var html = '<button class="following-back-button" id="followingBack">← 返回关注的人</button><h2 class="following-repos-title">' + escapeHtml(login) + ' 的当前公开仓库</h2><div class="following-repo-grid">' + visibleRepos.map(function (repo) { return '<article class="following-repo-card"><a href="' + escapeHtml(repo.html_url || '#') + '" target="_blank"><b>' + escapeHtml(repo.full_name || '') + '</b> ↗</a><p>' + escapeHtml(repo.description || '暂无项目简介。') + '</p><small>' + escapeHtml(repo.language || '未标注') + ' · ★ ' + countText(repo.stargazers_count) + '<br>最近更新 ' + dateText(repo.updated_at) + '</small></article>'; }).join('') + '</div>' || '<div class="surface-empty"><p>该用户目前没有可显示的公开仓库。</p></div>'; $('#followingResults').className = 'following-results following-repo-grid-wrap'; $('#followingResults').innerHTML = html; $('#followingStatus').textContent = '已读取 ' + visibleRepos.length + ' 个公开仓库。'; $('#followingBack').addEventListener('click', renderTab); }).catch(function (error) { $('#followingStatus').textContent = error.message || '无法读取该用户的公开仓库。'; }).finally(function () { button.disabled = false; }); });
    if (window.MutationObserver) { var observer = new MutationObserver(function () { var nodes = Array.prototype.slice.call(document.querySelectorAll('.following-repo-card p, .following-activity-body p')); if (!nodes.length || nodes.some(function (node) { return node.dataset.translating; })) return; var items = nodes.map(function (node) { return { description: node.textContent }; }); if (!items.some(function (item) { return /[A-Za-z]{3}/.test(item.description); })) return; nodes.forEach(function (node) { node.dataset.translating = '1'; }); fetch('/api/translate-descriptions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items: items }) }).then(function (response) { return response.json(); }).then(function (body) { (body.items || []).forEach(function (item, index) { if (nodes[index] && item.description) nodes[index].textContent = item.description; }); if (body.translated === false && $('#followingStatus')) $('#followingStatus').textContent = '简介暂未翻译，已显示 GitHub 原文；请检查模型配置或网络。'; }).catch(function () { if ($('#followingStatus')) $('#followingStatus').textContent = '简介翻译请求失败，已显示 GitHub 原文。'; }); }); observer.observe($('#followingResults'), { childList: true, subtree: true }); }
    $('#loadFollowing').addEventListener('click', function () { var button = this; button.disabled = true; $('#followingStatus').textContent = '正在读取关注列表和公开 Star 动态…'; githubRequest('/user/following?per_page=50').then(function (people) { if (!people.length) throw new Error('未发现可读取的关注账号'); data.following = people; return Promise.all(people.slice(0, 20).map(function (person) { return githubRequest('/users/' + encodeURIComponent(person.login) + '/events/public?per_page=30').then(function (events) { return { person: person, events: events || [] }; }); })); }).then(function (groups) { var feed = [], seen = {}; groups.forEach(function (group) { group.events.forEach(function (event) { if (event.type !== 'StarEvent') return; var repo = event.repo || {}, payload = event.payload || {}, key = group.person.login + '/' + repo.name; if (seen[key]) return; seen[key] = true; feed.push({ full_name: repo.name, html_url: 'https://github.com/' + repo.name, description: payload.repository ? payload.repository.description : '', stars: payload.repository ? payload.repository.stargazers_count : 0, actor: group.person.login, actor_avatar: group.person.avatar_url, action: '最近 Star', relative: dateText(event.created_at) + ' 更新' }); }); }); data.feed = feed; var map = {}; feed.forEach(function (row) { if (!map[row.full_name]) map[row.full_name] = Object.assign({}, row, { actors: [] }); if (map[row.full_name].actors.indexOf(row.actor) < 0) map[row.full_name].actors.push(row.actor); }); data.projects = Object.keys(map).map(function (name) { var row = map[name]; row.action = row.actors.length + ' 位关注者参与'; return row; }); data.me = []; localSet(cacheKey, data); $('#followingStatus').textContent = '已更新：' + feed.length + ' 条公开 Star 动态。'; renderTab(); }).catch(function (error) { $('#followingStatus').textContent = error.message || '暂时无法读取关注动态；请确认 Token 包含 read:user。'; }).finally(function () { button.disabled = false; }); });
    if (data.me && !data.me.length) data.me = state.items.slice(0, 30).map(function (repo) { return { full_name: repo.full_name, html_url: repo.html_url, description: repo.description, stars: repo.stars, actor: '我', action: '我的 Star', relative: dateText(repo.starred_at) + ' 更新' }; });
    renderTab();
  }

  function renderIssues() {
    setSurfaceTab('issues'); closeDrawer(); var body = $('.library-body'); body.classList.add('recommendations-mode'); body.style.gridTemplateColumns = 'minmax(0, 1fr)'; $('#tableWrap').hidden = true; $('#libraryOverview').hidden = true; $('#pager').hidden = true; $('#recommendationView').hidden = true; if ($('#followingView')) $('#followingView').hidden = true;
    var view = $('#issuesView'); if (!view) return; view.hidden = false;
    var item = selectedItem() || state.items[0];
    if (!item) { view.innerHTML = '<div class="surface-empty"><h3>暂无星标仓库</h3><p>先同步 GitHub 星标，再查看其中的问题。</p></div>'; return; }
    view.innerHTML = '<div class="issues-layout"><aside class="issue-repo-list"><p class="recommendation-kicker">我的星标仓库</p><h2>选择仓库</h2>' + (state.items || []).map(function (repo) { return '<button class="issue-repo-option ' + (repo.full_name === item.full_name ? 'active' : '') + '" data-issue-repo="' + escapeHtml(repo.full_name) + '">' + escapeHtml(repo.full_name) + '</button>'; }).join('') + '</aside><section class="issue-main"><div class="recommendation-header"><div><p class="recommendation-kicker">问题浏览</p><h1>' + escapeHtml(item.full_name) + ' 的问题</h1><p>只显示 GitHub Issue，不包含合并请求；内容直接展示在右侧。</p></div><div class="issue-toolbar"><select id="issueState" aria-label="问题状态" title="选择要查看的问题状态"><option value="open">未关闭</option><option value="closed">已关闭</option><option value="all">全部</option></select><button class="drawer-action" id="loadIssues" title="从 GitHub 读取这个仓库的问题">读取问题</button></div></div><div class="recommendation-status" id="issueStatus">点击“读取问题”查看最新内容。</div><div class="recommendation-results" id="issueResults"></div></section></div>';
    Array.prototype.forEach.call(document.querySelectorAll('[data-issue-repo]'), function (button) { button.addEventListener('click', function () { state.selected = button.dataset.issueRepo; renderIssues(); }); });
    var path = item.full_name.split('/').map(function (part) { return encodeURIComponent(part); }).join('/');
    $('#loadIssues').addEventListener('click', function () {
      var button = this, stateValue = $('#issueState').value;
      button.disabled = true; $('#issueStatus').textContent = '正在读取 ' + item.full_name + ' 的 Issue…'; $('#issueResults').innerHTML = '';
      githubRequest('/repos/' + path + '/issues?state=' + stateValue + '&sort=updated&direction=desc&per_page=30').then(function (issues) {
        var result = (issues || []).filter(function (issue) { return !issue.pull_request; });
        $('#issueStatus').textContent = result.length ? '显示最近更新的 ' + result.length + ' 个 Issue。' : '没有找到符合条件的 Issue。';
        $('#issueResults').innerHTML = result.map(function (issue) {
          var labels = (issue.labels || []).slice(0, 4).map(function (label) { return '<span class="issue-label">' + escapeHtml(label.name || '') + '</span>'; }).join('');
          var body = String(issue.body || '').replace(/\s+/g, ' ').trim();
          if (body.length > 180) body = body.slice(0, 180) + '…';
          return '<article class="issue-card"><a href="' + escapeHtml(issue.html_url || '#') + '" target="_blank" rel="noopener"><span class="issue-number">#' + issue.number + '</span> ' + escapeHtml(issue.title || '无标题') + ' ↗</a><p>' + escapeHtml(body || '暂无问题描述。') + '</p><div class="issue-meta"><span>' + escapeHtml((issue.user && issue.user.login) || '未知作者') + '</span><span>' + (issue.comments || 0) + ' 条评论</span><span>' + dateText(issue.updated_at) + ' 更新</span></div><div class="issue-labels">' + labels + '</div></article>';
        }).join('');
      }).catch(function (error) { $('#issueStatus').textContent = error.message || '暂时无法读取 Issue'; }).finally(function () { button.disabled = false; });
    });
  }

  function cubbyContext(names) {
    var list = (names && names.length ? state.items.filter(function (item) { return names.indexOf(item.full_name) !== -1; }) : state.items).slice(0, 40);
    return list.map(function (item) { var meta = metaFor(item.full_name); return { repo: item.full_name, description: item.description, language: item.language, topics: item.topics.slice(0, 6), tags: meta.tags, note: meta.note, stars: Number(item.stars || 0) }; });
  }

  var CUBBY_JOB_KEY = 'starradar:cubby-organize-job:v2';
  var cubbyJob = null;
  var sourceFingerprint = function (item) { return JSON.stringify([item.full_name, item.description, item.language, item.topics, item.stars, item.pushed_at]); };
  var metaFingerprint = function (meta) { return JSON.stringify([meta.tags || [], meta.note || '', !!meta.favorite]); };
  function loadCubbyJob() { var job = localGet(CUBBY_JOB_KEY, null); if (job && job.status === 'analyzing') { job.status = 'paused'; job.error = '页面曾在分析中断开；已保留进度，可从下一批继续。'; localSet(CUBBY_JOB_KEY, job); } if (job && job.status === 'applying') { job.status = 'review'; job.error = '上一次应用过程被中断；请先核对建议与现有标签后，再次确认应用。'; localSet(CUBBY_JOB_KEY, job); } if (job && job.chat_busy) { job.chat_busy = false; job.chat = (job.chat || []).concat([{ role: 'assistant', content: '上一条对话在页面刷新时中断了，可以重新发送。' }]).slice(-12); localSet(CUBBY_JOB_KEY, job); } return job; }
  function saveCubbyJob() { localSet(CUBBY_JOB_KEY, cubbyJob); }
  function stageIndex(status) { return ({ scope: 0, analyzing: 1, paused: 1, review: 2, applying: 3, receipt: 4 })[status] == null ? 0 : ({ scope: 0, analyzing: 1, paused: 1, review: 2, applying: 3, receipt: 4 })[status]; }
  function renderCubbyStages() {
    var labels = ['范围', '分析', '审核', '应用', '结果']; var active = stageIndex(cubbyJob ? cubbyJob.status : 'scope');
    $('#cubbyOrganizeStage').innerHTML = labels.map(function (label, index) { return '<span class="cubby-stage ' + (index < active ? 'done' : index === active ? 'active' : '') + '"><i>' + (index < active ? '✓' : index + 1) + '</i>' + label + '</span>'; }).join('');
    var total = cubbyJob && cubbyJob.scope ? cubbyJob.scope.length : 0, selected = cubbyJob && cubbyJob.proposals ? cubbyJob.proposals.filter(function (item) { return item.selected; }).length : 0;
    $('#cubbyJobBadge').textContent = cubbyJob && cubbyJob.status === 'review' ? '待审核 · 已选 ' + selected + ' 项' : cubbyJob && cubbyJob.status === 'analyzing' ? '分析 ' + Math.min(cubbyJob.next || 0, total) + '/' + total : cubbyJob && cubbyJob.status === 'receipt' ? '整理完成' : '待分析 · ' + total + ' 个项目';
  }
  function closeCubbyOrganize() { $('#cubbyOrganize').hidden = true; }
  function openCubbyOrganize() { cubbyJob = loadCubbyJob(); $('#cubbyOrganize').hidden = false; renderCubbyOrganize(); }
  function frozenScope(items) {
    return (items || state.items).map(function (item) { var meta = metaFor(item.full_name); return { repo: item.full_name, description: item.description, language: item.language, topics: item.topics.slice(0, 8), stars: Number(item.stars || 0), pushed_at: item.pushed_at, existing_tags: meta.tags.slice(0, 12), source_fingerprint: sourceFingerprint(item), meta_fingerprint: metaFingerprint(meta) }; });
  }
  function cubbyStarsPage(page) {
    var token = localStorage.getItem(TOKEN_KEY) || '';
    if (token) {
      return fetch('https://api.github.com/user/starred?per_page=100&page=' + page + '&sort=created&direction=desc', { headers: { Authorization: 'token ' + token, Accept: 'application/vnd.github.star+json', 'X-GitHub-Api-Version': '2022-11-28' } }).then(function (response) { return response.json().then(function (items) { if (!response.ok) throw new Error((items && items.message) || 'GitHub 星标读取失败'); return { items: items.map(normalize), hasNext: /rel="next"/.test(response.headers.get('Link') || '') }; }); });
    }
    return fetch('/api/github/starred?page=' + page + '&per_page=100', { cache: 'no-store' }).then(function (response) { return response.json().then(function (body) { if (!response.ok || !body.ok) throw new Error(body.error || '请先在首页登录 GitHub'); return { items: (body.items || []).map(normalize), hasNext: !!body.has_next }; }); });
  }
  function loadAllCubbyStars() {
    var items = [];
    function next(page) { return cubbyStarsPage(page).then(function (result) { items = items.concat(result.items); return result.hasNext && page < 50 ? next(page + 1) : items; }); }
    return Promise.all([next(1), metadataLoad()]).then(function (results) { return results[0]; });
  }
  function newCubbyJob() {
    cubbyJob = { id: 'cubby-' + Date.now().toString(36), status: 'scope', created_at: new Date().toISOString(), scope: frozenScope(), next: 0, proposals: [], receipt: null };
    saveCubbyJob(); renderCubbyOrganize();
  }
  function scopeItem(repo) { return (cubbyJob.scope || []).filter(function (item) { return item.repo === repo; })[0]; }
  function safeSuggestions(text, batch) {
    var parsed = window.LLM.parseJSON(text) || {}; var allowed = {}; batch.forEach(function (item) { allowed[item.repo] = item; });
    return (Array.isArray(parsed.suggestions) ? parsed.suggestions : []).map(function (entry) {
      var item = allowed[entry && entry.repo]; if (!item || !Array.isArray(entry.tags)) return null;
      var tags = entry.tags.map(function (tag) { return String(tag || '').trim().slice(0, 32); }).filter(function (tag, index, values) { return tag && values.indexOf(tag) === index && item.existing_tags.indexOf(tag) === -1; }).slice(0, 3);
      var evidence = String(entry.evidence || '').trim().slice(0, 220); return tags.length && evidence ? { repo: item.repo, tags: tags, evidence: evidence, source_fingerprint: item.source_fingerprint, meta_fingerprint: item.meta_fingerprint, selected: true } : null;
    }).filter(Boolean);
  }
  function analyzeCubbyBatch() {
    if (!cubbyJob || cubbyJob.status !== 'analyzing') return;
    if (!window.LLM || !window.LLM.isConfigured()) { cubbyJob.status = 'paused'; cubbyJob.error = '请先在首页「我的雷达」配置 LLM。'; saveCubbyJob(); renderCubbyOrganize(); return; }
    var batch = cubbyJob.scope.slice(cubbyJob.next, cubbyJob.next + 12);
    if (!batch.length) { cubbyJob.status = 'review'; cubbyJob.reviewPage = 1; cubbyJob.error = ''; saveCubbyJob(); renderCubbyOrganize(); return; }
    renderCubbyOrganize();
    var prompt = '你是谨慎的开源项目图书馆整理员。根据给定的仓库描述、语言、Topics 和既有标签提出本地标签建议。只能引用输入中明确存在的事实，不要猜测。返回严格 JSON：{"suggestions":[{"repo":"owner/name","tags":["短中文标签"],"evidence":"证据：描述/Topics 中的具体词"}]}。每项最多 3 个新标签；没有明确依据则不要返回该项。';
    window.LLM.chat([{ role: 'system', content: '你只提出可审核的建议，绝不执行写入。' }, { role: 'user', content: prompt + '\n\n冻结范围内的本批项目：' + JSON.stringify(batch) }], { feature: 'cubby-organize', temperature: 0.15, max_tokens: 1700 })
      .then(function (text) { cubbyJob.proposals = cubbyJob.proposals.concat(safeSuggestions(text, batch)); cubbyJob.next += batch.length; cubbyJob.error = ''; saveCubbyJob(); analyzeCubbyBatch(); })
      .catch(function (error) { cubbyJob.status = 'paused'; cubbyJob.error = error.message === 'rate-limit' ? '已触及今日模型调用限制；可以稍后继续。' : ('分析暂停：' + (error.message || '请检查 LLM 配置')); saveCubbyJob(); renderCubbyOrganize(); });
  }
  function renderCubbyScope() {
    var total = cubbyJob.scope.length;
    return '<div class="cubby-task-bubble">为整个星标库中未整理的仓库添加有意义的语义标签。Cubby 会先检查仓库名称、描述、Topics 与已有标签；只有证据充分时才提出建议，并在写入前一次性返回完整审核清单。</div><p class="cubby-agent-line">正在准备分析范围。</p><div class="cubby-control-card"><span>◇</span><p><b>冻结范围</b><small>先锁定当前 Stars，再开始分析；此阶段不会写入任何数据。</small></p></div><div class="cubby-analysis-card"><h3>准备分析</h3><p>全部星标仓库</p><div class="cubby-progress"><i style="width:0%"></i></div><div class="cubby-progress-row"><span>当前可见 ' + total + ' 项 · 开始后将读取全部星标</span></div><button class="cubby-primary" id="cubbyFreeze">分析整个星标库</button><button class="cubby-secondary" id="cubbyDiscard">取消</button></div>';
  }
  function renderCubbyAnalyze() {
    var total = cubbyJob.scope.length, current = Math.min(cubbyJob.next, total), percent = total ? Math.round(current / total * 100) : 0;
    return '<div class="cubby-task-bubble">正在为整个星标库整理语义标签。Cubby 只会基于仓库名称、描述、Topics 与已有标签中的明确证据提出建议。</div><p class="cubby-agent-line">' + (cubbyJob.status === 'paused' ? '分析已暂停，检查点已保留，可以继续。' : '正在分析已冻结的星标库。') + '</p>' + (cubbyJob.status === 'paused' ? '<div class="cubby-control-card"><span>◇</span><p><b>控制此任务的页面已断开。</b><small>接管任务后可以继续管理本次分析。</small></p><button id="cubbyResume">接管任务</button></div>' : '') + '<div class="cubby-analysis-card"><h3>正在分析</h3><p>全部星标仓库</p><div class="cubby-progress"><i style="width:' + percent + '%"></i></div><div class="cubby-progress-row"><span>已分析 ' + current + ' 项 · 剩余 ' + Math.max(0, total - current) + ' 项</span></div><p class="cubby-policy">已锁定范围：' + total + ' 个项目 · 分析过程中不会修改标签。</p><button class="cubby-secondary" id="cubbyCancelAnalyze">取消分析</button></div>';
  }
  function renderCubbyReview() {
    var proposals = cubbyJob.proposals || [], selected = proposals.filter(function (proposal) { return proposal.selected; }).length, tags = proposals.filter(function (proposal) { return proposal.selected; }).reduce(function (sum, proposal) { return sum + proposal.tags.length; }, 0);
    var page = Math.max(1, Number(cubbyJob.reviewPage || 1)), perPage = 100, start = (page - 1) * perPage, view = proposals.slice(start, start + perPage), pages = Math.max(1, Math.ceil(proposals.length / perPage));
    var items = view.map(function (proposal, offset) { var index = start + offset, source = scopeItem(proposal.repo) || {}, existing = source.existing_tags || [], expanded = !!proposal.expanded; return '<article class="cubby-review-item"><input type="checkbox" data-cubby-select="' + index + '"' + (proposal.selected ? ' checked' : '') + '><div class="cubby-review-main"><b>' + escapeHtml(proposal.repo) + ' ↗</b><button data-cubby-expand="' + index + '">' + (expanded ? '收起审核详情' : '查看审核详情') + ' ›</button><div class="cubby-tags-edit"><span>已有标签</span>' + existing.map(function (tag) { return '<span>#' + escapeHtml(tag) + '</span>'; }).join('') + proposal.tags.map(function (tag) { return '<span class="cubby-new-tag">+' + escapeHtml(tag) + '</span>'; }).join('') + '<input data-cubby-tags="' + index + '" value="' + escapeHtml(proposal.tags.join('，')) + '"></div>' + (expanded ? '<p class="cubby-evidence"><b>证据：</b>' + escapeHtml(proposal.evidence) + '</p><button class="cubby-reject" data-cubby-reject="' + index + '">◯ 拒绝建议</button>' : '') + '</div></article>'; }).join('') || '<p class="drawer-intro">没有需要审核的建议。</p>';
    return '<div class="cubby-review-top"><div><p class="cubby-kicker">✦ 审核标签建议</p><h3>审核标签建议</h3><p>' + proposals.length + ' 条建议 · 已选 ' + selected + ' 条</p></div></div><div class="cubby-review-cover"><b>已覆盖全库 · 已分析 ' + cubbyJob.scope.length + ' 个仓库</b><span>编辑和拒绝都只会保留在本次审核中，只有勾选的建议才会应用。</span></div><div class="cubby-proposals">' + items + '</div><footer class="cubby-review-foot"><button class="cubby-primary" id="cubbyApply"' + (selected ? '' : ' disabled') + '>应用 ' + tags + ' 个标签到 ' + selected + ' 个仓库</button><button class="cubby-text-button" id="cubbySelectAll">全选</button><button class="cubby-text-button" id="cubbyClearAll">清空</button><span>' + (proposals.length ? (start + 1) + '–' + Math.min(start + perPage, proposals.length) + ' / ' + proposals.length : '') + '</span><button class="cubby-text-button" data-cubby-page="' + (page - 1) + '"' + (page <= 1 ? ' disabled' : '') + '>‹</button><button class="cubby-text-button" data-cubby-page="' + (page + 1) + '"' + (page >= pages ? ' disabled' : '') + '>›</button></footer>';
  }
  function cubbyChatContext() {
    var scope = cubbyJob && Array.isArray(cubbyJob.scope) && cubbyJob.scope.length ? cubbyJob.scope : frozenScope();
    return scope.slice(0, 40).map(function (item) { return { repo: item.repo, description: item.description, language: item.language, topics: (item.topics || []).slice(0, 8), tags: item.existing_tags || metaFor(item.repo).tags, stars: Number(item.stars || 0) }; });
  }
  function renderCubbyChat() {
    var messages = cubbyJob && Array.isArray(cubbyJob.chat) ? cubbyJob.chat : [];
    if (!messages.length && !(cubbyJob && cubbyJob.chat_busy)) return '';
    var body = messages.map(function (message) { return '<div class="cubby-chat-message ' + (message.role === 'user' ? 'user' : 'assistant') + '"><span>' + (message.role === 'user' ? '你' : 'Cubby') + '</span><p>' + escapeHtml(message.content || '') + '</p></div>'; }).join('');
    if (cubbyJob && cubbyJob.chat_busy) body += '<div class="cubby-chat-message assistant pending"><span>Cubby</span><p>正在查看当前星标范围…</p></div>';
    return '<section class="cubby-chat-thread" aria-live="polite"><div class="cubby-chat-thread-head"><span>对话</span><small>仅基于当前星标元数据</small></div>' + body + '</section>';
  }
  function sendCubbyQuestion() {
    var input = $('#cubbyAsk');
    if (!input || !cubbyJob || cubbyJob.chat_busy) return;
    var question = String(input.value || '').trim();
    if (!question) return;
    if (!window.LLM || !window.LLM.isConfigured()) {
      cubbyJob.chat = (cubbyJob.chat || []).concat([{ role: 'assistant', content: '请先在首页「我的雷达 → AI 个性化」配置模型。' }]);
      saveCubbyJob(); renderCubbyOrganize(); return;
    }
    input.value = '';
    var sendButton = $('#cubbyAskSend');
    input.disabled = true;
    if (sendButton) sendButton.disabled = true;
    cubbyJob.chat = (cubbyJob.chat || []).concat([{ role: 'user', content: question }]).slice(-12);
    cubbyJob.chat_busy = true; cubbyJob.chat_error = ''; saveCubbyJob(); renderCubbyOrganize();
    var history = cubbyJob.chat.slice(-8).map(function (message) { return { role: message.role, content: message.content }; });
    var system = '你是 Cubby，负责帮助用户理解和整理 GitHub 星标库。只根据提供的仓库元数据回答，不要虚构事实，不执行任何写入。回答简洁、中文为主；如果用户问标签建议，先说明依据并提醒需要在审核阶段应用。当前上下文：' + JSON.stringify(cubbyChatContext());
    window.LLM.chat([{ role: 'system', content: system }].concat(history), { feature: 'cubby-chat', temperature: 0.35, max_tokens: 900 })
      .then(function (text) { cubbyJob.chat = (cubbyJob.chat || []).concat([{ role: 'assistant', content: text }]).slice(-12); })
      .catch(function (error) { cubbyJob.chat = (cubbyJob.chat || []).concat([{ role: 'assistant', content: '暂时无法回答：' + (error.message || '请检查模型连接') }]).slice(-12); })
      .then(function () { cubbyJob.chat_busy = false; input.disabled = false; if (sendButton) sendButton.disabled = false; saveCubbyJob(); renderCubbyOrganize(); });
  }
  function renderCubbyReceipt() { var receipt = cubbyJob.receipt || { changed: 0, skipped: 0, unchanged: 0 }; return '<div class="cubby-receipt"><p class="cubby-kicker">执行回执 · 本地元数据</p><h3>本次整理已完成</h3><p>标签只写入航标 Beacon 的本地元数据，不会修改 GitHub 的 Star。应用前已比对本次冻结范围；期间发生本地编辑的项目会被跳过。</p><div class="cubby-receipt-grid"><span><b>' + receipt.changed + '</b><small>已更新项目</small></span><span><b>' + receipt.unchanged + '</b><small>没有新增标签</small></span><span><b>' + receipt.skipped + '</b><small>因数据变化而跳过</small></span></div><button class="cubby-primary" id="cubbyDone">返回星标库</button><button class="cubby-secondary" id="cubbyNewJob">新建整理任务</button></div>'; }
  function renderCubbyOrganize() {
    if (!cubbyJob) cubbyJob = { status: 'scope', scope: frozenScope(), proposals: [] };
    renderCubbyStages(); var container = $('#cubbyOrganizeContent');
    $('#cubbyScopeHint').textContent = cubbyJob.scope && cubbyJob.scope.length ? '已锁定范围：' + cubbyJob.scope.length + ' 个仓库' : '未锁定范围';
    var stage = cubbyJob.status === 'scope' ? renderCubbyScope() : (cubbyJob.status === 'analyzing' || cubbyJob.status === 'paused') ? renderCubbyAnalyze() : cubbyJob.status === 'review' ? renderCubbyReview() : renderCubbyReceipt();
    container.innerHTML = stage + renderCubbyChat();
    if ($('#cubbyFreeze')) $('#cubbyFreeze').addEventListener('click', function () { var button = this; button.disabled = true; button.textContent = '正在冻结全库…'; loadAllCubbyStars().then(function (items) { cubbyJob.scope = frozenScope(items); cubbyJob.status = 'analyzing'; cubbyJob.next = 0; cubbyJob.proposals = []; cubbyJob.error = ''; saveCubbyJob(); analyzeCubbyBatch(); }).catch(function (error) { cubbyJob.error = error.message || '无法冻结全库范围'; saveCubbyJob(); renderCubbyOrganize(); }); });
    if ($('#cubbyDiscard')) $('#cubbyDiscard').addEventListener('click', function () { localStorage.removeItem(CUBBY_JOB_KEY); cubbyJob = null; closeCubbyOrganize(); });
    if ($('#cubbyResume')) $('#cubbyResume').addEventListener('click', function () { cubbyJob.status = 'analyzing'; cubbyJob.error = ''; saveCubbyJob(); analyzeCubbyBatch(); });
    if ($('#cubbyCancelAnalyze')) $('#cubbyCancelAnalyze').addEventListener('click', function () { cubbyJob.status = 'scope'; cubbyJob.next = 0; cubbyJob.proposals = []; saveCubbyJob(); renderCubbyOrganize(); });
    Array.prototype.forEach.call(document.querySelectorAll('[data-cubby-select]'), function (input) { input.addEventListener('change', function () { cubbyJob.proposals[Number(input.dataset.cubbySelect)].selected = input.checked; saveCubbyJob(); renderCubbyOrganize(); }); });
    Array.prototype.forEach.call(document.querySelectorAll('[data-cubby-tags]'), function (input) { input.addEventListener('change', function () { var tags = input.value.split(/[，,]/).map(function (tag) { return tag.trim().slice(0, 32); }).filter(function (tag, index, all) { return tag && all.indexOf(tag) === index; }).slice(0, 3); cubbyJob.proposals[Number(input.dataset.cubbyTags)].tags = tags; cubbyJob.proposals[Number(input.dataset.cubbyTags)].selected = tags.length > 0; saveCubbyJob(); renderCubbyOrganize(); }); });
    Array.prototype.forEach.call(document.querySelectorAll('[data-cubby-expand]'), function (button) { button.addEventListener('click', function () { var proposal = cubbyJob.proposals[Number(button.dataset.cubbyExpand)]; proposal.expanded = !proposal.expanded; saveCubbyJob(); renderCubbyOrganize(); }); });
    Array.prototype.forEach.call(document.querySelectorAll('[data-cubby-reject]'), function (button) { button.addEventListener('click', function () { var proposal = cubbyJob.proposals[Number(button.dataset.cubbyReject)]; proposal.selected = false; proposal.rejected = true; saveCubbyJob(); renderCubbyOrganize(); }); });
    Array.prototype.forEach.call(document.querySelectorAll('[data-cubby-page]'), function (button) { button.addEventListener('click', function () { if (button.disabled) return; cubbyJob.reviewPage = Number(button.dataset.cubbyPage); saveCubbyJob(); renderCubbyOrganize(); }); });
    if ($('#cubbySelectAll')) $('#cubbySelectAll').addEventListener('click', function () { cubbyJob.proposals.forEach(function (proposal) { proposal.selected = true; }); saveCubbyJob(); renderCubbyOrganize(); });
    if ($('#cubbyClearAll')) $('#cubbyClearAll').addEventListener('click', function () { cubbyJob.proposals.forEach(function (proposal) { proposal.selected = false; }); saveCubbyJob(); renderCubbyOrganize(); });
    if ($('#cubbyRestart')) $('#cubbyRestart').addEventListener('click', function () { cubbyJob.status = 'scope'; cubbyJob.next = 0; cubbyJob.proposals = []; saveCubbyJob(); renderCubbyOrganize(); });
    if ($('#cubbyApply')) $('#cubbyApply').addEventListener('click', applyCubbyOrganize);
    if ($('#cubbyDone')) $('#cubbyDone').addEventListener('click', function () { closeCubbyOrganize(); render(); });
    if ($('#cubbyNewJob')) $('#cubbyNewJob').addEventListener('click', newCubbyJob);
    if ($('#cubbyAskSend')) $('#cubbyAskSend').addEventListener('click', sendCubbyQuestion);
    if ($('#cubbyAsk')) $('#cubbyAsk').addEventListener('keydown', function (event) { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendCubbyQuestion(); } });
  }
  function applyCubbyOrganize() {
    var selected = cubbyJob.proposals.filter(function (proposal) { return proposal.selected && proposal.tags.length; }); if (!selected.length) return;
    cubbyJob.status = 'applying'; saveCubbyJob(); renderCubbyOrganize();
    loadAllCubbyStars().then(function (liveItems) {
      var receipt = { changed: 0, unchanged: 0, skipped: 0 };
      var liveByName = {}; liveItems.forEach(function (item) { liveByName[item.full_name] = item; });
      var writes = selected.map(function (proposal) {
        var source = scopeItem(proposal.repo); var live = liveByName[proposal.repo]; var meta = metaFor(proposal.repo);
        if (!source || !live || sourceFingerprint(live) !== proposal.source_fingerprint || metaFingerprint(meta) !== proposal.meta_fingerprint) { receipt.skipped++; return Promise.resolve(); }
        var next = { tags: meta.tags.slice(), note: meta.note, favorite: meta.favorite }; proposal.tags.forEach(function (tag) { if (next.tags.indexOf(tag) === -1 && next.tags.length < 12) next.tags.push(tag); });
        if (next.tags.length === meta.tags.length) { receipt.unchanged++; return Promise.resolve(); }
        return metadataSave(proposal.repo, next).then(function () { receipt.changed++; });
      });
      // 应用完成后重新读取本地元数据，并立即刷新背后的星标列表。
      // 之前这里只刷新了 Cubby 面板，关闭面板后列表仍可能显示旧标签。
      return Promise.all(writes).then(function () {
        return metadataLoad().then(function () {
          cubbyJob.status = 'receipt';
          cubbyJob.receipt = receipt;
          cubbyJob.completed_at = new Date().toISOString();
          saveCubbyJob();
          render();
          renderCubbyOrganize();
        });
      });
    }).catch(function (error) { cubbyJob.status = 'review'; cubbyJob.error = error.message || '应用失败'; saveCubbyJob(); renderCubbyOrganize(); });
  }

  function renderCubby() {
    openCubbyOrganize();
  }

  function renderTableLayout() {
    openDrawer('TABLE LAYOUT · 本机保存', '自定义表格列', '<p class="drawer-intro">直接拖拽表头可调整列顺序；拖动表头右侧细线可调宽。这里控制列显隐与所有者头像。</p><div id="layoutControls"></div><button class="drawer-action" id="resetLayout">恢复默认布局</button>');
    $('#layoutControls').innerHTML = state.layout.columns.map(function (key) { return '<label class="cubby-proposal"><input type="checkbox" data-layout-column="' + key + '"' + (state.layout.visible[key] ? ' checked' : '') + (key === 'repo' ? ' disabled' : '') + '><span><b>' + COLUMN_LABELS[key] + '</b><span>宽度 ' + Math.round(state.layout.widths[key]) + ' px</span></span></label>'; }).join('') + '<p class="drawer-intro layout-note">仓库头像仅显示在仓库列，所有者列使用纯文本，避免重复头像。</p>';
    Array.prototype.forEach.call(document.querySelectorAll('[data-layout-column]'), function (input) { input.addEventListener('change', function () { state.layout.visible[input.dataset.layoutColumn] = input.checked; saveLayout(); render(); }); });
    $('#resetLayout').addEventListener('click', function () { state.layout = JSON.parse(JSON.stringify(DEFAULT_LAYOUT)); saveLayout(); closeDrawer(); render(); });
  }

  function clearFilters() { state.query = ''; state.language = ''; state.tag = ''; state.favorites = false; state.untagged = false; state.archived = false; $('#librarySearch').value = ''; if ($('#tagSearch')) $('#tagSearch').value = ''; $('#clearSearch').hidden = true; render(); }
  $('#librarySearch').addEventListener('input', function () { state.query = this.value; $('#clearSearch').hidden = !state.query; render(); });
  $('#clearSearch').addEventListener('click', function () { $('#librarySearch').value = ''; state.query = ''; this.hidden = true; render(); });
  $('#filterFavorites').addEventListener('change', function () { state.favorites = this.checked; render(); });
  $('#filterUntagged').addEventListener('change', function () { state.untagged = this.checked; render(); });
  if ($('#filterArchived')) $('#filterArchived').addEventListener('change', function () { state.archived = this.checked; render(); });
  if ($('#tagSearch')) $('#tagSearch').addEventListener('input', function () { render(); });
  $('#clearFilters').addEventListener('click', clearFilters);
  Array.prototype.forEach.call(document.querySelectorAll('[data-collapse]'), function (button) {
    button.addEventListener('click', function () {
      var group = button.closest('.filter-group');
      if (group) group.classList.toggle('is-collapsed');
    });
  });
  $('#sortSelect').addEventListener('change', function () { state.sort = this.value; render(); });
  $('#sortDirection').addEventListener('click', function () { state.direction = state.direction === 'desc' ? 'asc' : 'desc'; render(); });
  $('#syncStars').addEventListener('click', function () { loadStars(); });
  $('#autoTag').addEventListener('click', autoTagVisible);
  $('#following').addEventListener('click', renderFollowing);
  Array.prototype.forEach.call(document.querySelectorAll('.surface-tab'), function (tab) {
    tab.addEventListener('click', function (event) {
      var surface = tab.dataset.surface;
      if (surface === 'stars') { event.preventDefault(); state.surface = 'stars'; setSurfaceTab('stars'); closeDrawer(); render(); return; }
      event.preventDefault();
      if (surface === 'watch') renderWatch();
      if (surface === 'following') renderFollowing();
      if (surface === 'issues') renderIssues();
      if (surface === 'recommendations') renderForYou();
    });
  });
  $('#cubbyFloat').addEventListener('click', renderCubby);
  $('#cubbyFloat').addEventListener('mouseenter', function () { $('#cubbyMascot').src = 'assets/better-stars/cubby-working.gif'; });
  $('#cubbyFloat').addEventListener('mouseleave', function () { $('#cubbyMascot').src = 'assets/better-stars/cubby-static.png'; });
  $('#tableLayout').addEventListener('click', renderTableLayout);
  window.addEventListener('resize', function () { if (state.layout) renderTableHeader(); });
  $('#drawerClose').addEventListener('click', closeDrawer);
  $('#cubbyOrganizeClose').addEventListener('click', closeCubbyOrganize);
  $('#cubbyOrganize').addEventListener('click', function (event) { if (event.target === this) closeCubbyOrganize(); });
  document.addEventListener('keydown', function (event) {
    if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey) {
      var target = event.target;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return;
      event.preventDefault();
      if ($('#librarySearch')) $('#librarySearch').focus();
      return;
    }
    if (event.key !== 'Escape') return;
    if (!$('#cubbyOrganize').hidden) { closeCubbyOrganize(); return; }
    if (!$('#workspaceDrawer').hidden) { setSurfaceTab('stars'); closeDrawer(); return; }
    if (state.selected) { state.selected = ''; render(); }
  });
  $('#applyBatchTag').addEventListener('click', function () { var tag = $('#batchTag').value.trim(); if (!tag) { status('先输入一个标签'); return; } saveMany(checkedNames(), function (meta) { if (meta.tags.indexOf(tag) === -1 && meta.tags.length < 12) meta.tags.push(tag); return meta; }, '已添加标签「' + tag + '」'); $('#batchTag').value = ''; });
  $('#batchTag').addEventListener('keydown', function (event) { if (event.key === 'Enter') { event.preventDefault(); $('#applyBatchTag').click(); } });
  $('#batchFavorite').addEventListener('click', function () { saveMany(checkedNames(), function (meta) { meta.favorite = true; return meta; }, '已收藏所选项目'); });
  $('#clearSelection').addEventListener('click', function () { state.checked = {}; render(); });
  $('#prevPage').addEventListener('click', function () { if (state.page > 1) { state.page--; state.selected = ''; loadStars(); } });
  $('#nextPage').addEventListener('click', function () { if (state.hasNext) { state.page++; state.selected = ''; loadStars(); } });
  Array.prototype.forEach.call(document.querySelectorAll('[data-collapse]'), function (button) { button.addEventListener('click', function () { var list = button.parentNode.querySelector('.filter-list'); var hidden = list.hidden; list.hidden = !hidden; button.querySelector('i').textContent = hidden ? '⌄' : '›'; }); });
  // Do not read a locally stored GitHub token until the same local access gate
  // used by the main StarRadar page has accepted this browser session.
  state.layout = loadLayout();
  if (window.StarRadarAccessReady && typeof window.StarRadarAccessReady.then === 'function') {
    window.StarRadarAccessReady.then(loadStars);
  } else {
    loadStars();
  }
}());

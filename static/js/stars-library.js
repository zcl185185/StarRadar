(function () {
  'use strict';

  var TOKEN_KEY = 'starradar:gh_token';
  var META_KEY = 'starradar:stars_library_meta';
  var LAYOUT_KEY = 'starradar:stars_library_layout';
  var DEFAULT_LAYOUT = { columns: ['repo', 'description', 'language', 'stars', 'updated', 'tags', 'owner'], visible: { repo: true, description: true, language: true, stars: true, updated: true, tags: true, owner: false }, widths: { repo: 180, description: 250, language: 82, stars: 68, updated: 95, tags: 150, owner: 130 }, ownerAvatar: true };
  var COLUMN_LABELS = { repo: '仓库', description: '项目简介', language: '语言', stars: 'Stars', updated: '更新时间', tags: '标签', owner: '所有者' };
  var state = { items: [], meta: {}, selected: '', checked: {}, layout: null, page: 1, hasNext: false, sort: 'starred_at', direction: 'desc', query: '', language: '', tag: '', favorites: false, untagged: false };
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
    return { full_name: repo.full_name || '', html_url: repo.html_url || '', description: repo.description || '暂无项目描述。', language: repo.language || '未标注', topics: Array.isArray(repo.topics) ? repo.topics : [], stars: repo.stars != null ? repo.stars : repo.stargazers_count, owner: (repo.owner && repo.owner.login) || String(repo.full_name || '').split('/')[0], owner_avatar: (repo.owner && repo.owner.avatar_url) || '', starred_at: raw.starred_at || repo.starred_at || '', pushed_at: repo.updated_at || repo.pushed_at || '' };
  }

  function directStars(token) {
    var url = 'https://api.github.com/user/starred?per_page=100&page=' + state.page + '&sort=created&direction=desc';
    return fetch(url, { headers: { Authorization: 'token ' + token, Accept: 'application/vnd.github.star+json', 'X-GitHub-Api-Version': '2022-11-28' } }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) throw new Error((body && body.message) || 'GitHub 星标读取失败');
        return { items: body, hasNext: /rel="next"/.test(response.headers.get('Link') || '') };
      });
    });
  }

  function backendStars() {
    return fetch('/api/github/starred?page=' + state.page + '&per_page=100', { cache: 'no-store' }).then(function (response) { return response.json().then(function (body) { if (!response.ok || !body.ok) throw new Error(body.error || '请先在首页登录 GitHub'); return { items: body.items || [], hasNext: !!body.has_next }; }); });
  }

  function loadStars() {
    var button = $('#syncStars'); button.disabled = true; status('正在同步 GitHub 星标…');
    var token = localStorage.getItem(TOKEN_KEY) || '';
    Promise.all([token ? directStars(token) : backendStars(), metadataLoad()]).then(function (results) {
      state.items = results[0].items.map(normalize).filter(function (item) { return item.full_name; });
      state.checked = {};
      state.hasNext = results[0].hasNext;
      if (state.selected && !selectedItem()) state.selected = '';
      status('已同步 · ' + new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }));
      render();
    }).catch(function (error) {
      state.items = []; state.hasNext = false; status(error.message || '同步失败'); render();
    }).finally(function () { button.disabled = false; });
  }

  function filtered() {
    var query = state.query.trim().toLowerCase();
    var items = state.items.filter(function (item) {
      var meta = metaFor(item.full_name);
      var text = [item.full_name, item.description, item.language, item.topics.join(' '), meta.tags.join(' '), meta.note].join(' ').toLowerCase();
      return (!query || text.indexOf(query) !== -1) && (!state.language || item.language === state.language) && (!state.tag || meta.tags.indexOf(state.tag) !== -1) && (!state.favorites || meta.favorite) && (!state.untagged || !meta.tags.length);
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
    $('#tagFilters').innerHTML = Object.keys(tags).sort(function (a, b) { return tags[b] - tags[a]; }).map(function (tag) { return '<button class="filter-option' + (state.tag === tag ? ' active' : '') + '" data-tag="' + escapeHtml(tag) + '"><b>' + escapeHtml(tag) + '</b><em>' + tags[tag] + '</em></button>'; }).join('') || '<span class="note-muted">给项目添加标签后显示</span>';
    Array.prototype.forEach.call(document.querySelectorAll('[data-language]'), function (button) { button.addEventListener('click', function () { state.language = state.language === button.dataset.language ? '' : button.dataset.language; render(); }); });
    Array.prototype.forEach.call(document.querySelectorAll('[data-tag]'), function (button) { button.addEventListener('click', function () { state.tag = state.tag === button.dataset.tag ? '' : button.dataset.tag; render(); }); });
  }

  function renderTableHeader() {
    var columns = visibleColumns();
    $('#tableWrap').style.setProperty('--library-columns', ['16px'].concat(columns.map(function (key) { return Math.round(state.layout.widths[key]) + 'px'; })).join(' '));
    $('#tableHead').innerHTML = '<span><input id="selectAllRows" type="checkbox" aria-label="选择当前筛选的所有项目"></span>' + columns.map(function (key) { return '<span class="layout-head-cell" draggable="true" data-column="' + key + '">' + COLUMN_LABELS[key] + '<i class="column-resizer" data-resize="' + key + '"></i></span>'; }).join('');
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
    if (key === 'repo') return '<span class="repo-name">' + escapeHtml(item.full_name) + '</span>';
    if (key === 'description') return '<span class="repo-description">' + escapeHtml(item.description) + '</span>';
    if (key === 'language') return '<span class="row-lang">' + escapeHtml(item.language) + '</span>';
    if (key === 'stars') return '<span class="row-stars">' + countText(item.stars) + '</span>';
    if (key === 'updated') return '<span class="row-updated">' + dateText(item.pushed_at) + '</span>';
    if (key === 'owner') return '<span class="owner-cell">' + (state.layout.ownerAvatar && item.owner_avatar ? '<img src="' + escapeHtml(item.owner_avatar) + '" alt="">' : '') + '<span>' + escapeHtml(item.owner || '—') + '</span></span>';
    var tags = meta.tags.slice(0, 2).map(function (tag, index) { return '<span class="row-tag' + (index === 0 && meta.favorite ? ' strong' : '') + '">' + escapeHtml(tag) + '</span>'; }).join('');
    if (meta.tags.length > 2) tags += '<span class="row-tag-count">+' + (meta.tags.length - 2) + '</span>';
    return '<span class="tag-cluster">' + (tags || '<span class="row-tag-count">—</span>') + '</span>';
  }

  function renderRows(items) {
    renderTableHeader();
    if (!state.items.length) { $('#starsRows').innerHTML = '<div class="empty-list"><span>☆</span><b>还没有可展示的星标</b><p>请先回到航标 Beacon 首页登录 GitHub，然后点击“同步”。</p><a href="index.html">返回航标 Beacon 首页</a></div>'; return; }
    if (!items.length) { $('#starsRows').innerHTML = '<div class="empty-list"><span>⌕</span><b>没有符合条件的项目</b><p>换一个关键词，或清除当前筛选试试。</p></div>'; return; }
    $('#starsRows').innerHTML = items.map(function (item) {
      var meta = metaFor(item.full_name);
      return '<article class="star-row' + (state.selected === item.full_name ? ' selected' : '') + '" role="row" data-repo="' + escapeHtml(item.full_name) + '"><input class="row-select" type="checkbox" aria-label="选择 ' + escapeHtml(item.full_name) + '"' + (state.checked[item.full_name] ? ' checked' : '') + '>' + visibleColumns().map(function (key) { return tableCell(item, key, meta); }).join('') + '</article>';
    }).join('');
    Array.prototype.forEach.call(document.querySelectorAll('.star-row'), function (row) {
      row.addEventListener('click', function () { state.selected = row.dataset.repo; renderRows(filtered()); renderDetail(); });
      var checkbox = row.querySelector('.row-select');
      checkbox.addEventListener('click', function (event) { event.stopPropagation(); });
      checkbox.addEventListener('change', function () { state.checked[row.dataset.repo] = checkbox.checked; renderBatchTools(); renderSelectAll(filtered()); });
    });
  }

  function renderDetail() {
    var item = selectedItem(); $('#detailEmpty').hidden = !!item; $('#detailContent').hidden = !item;
    if (!item) return;
    var meta = metaFor(item.full_name);
    var topics = item.topics.length ? item.topics.map(function (topic) { return '<span>' + escapeHtml(topic) + '</span>'; }).join('') : '<span>暂无 Topics</span>';
    var tags = meta.tags.map(function (tag) { return '<button data-remove-tag="' + escapeHtml(tag) + '">' + escapeHtml(tag) + ' ×</button>'; }).join('');
    $('#detailContent').className = 'detail-content';
    $('#detailContent').innerHTML = '<div class="detail-topline"><span>' + escapeHtml(item.language) + '</span><button class="detail-favorite' + (meta.favorite ? ' on' : '') + '" id="favoriteButton">★ ' + (meta.favorite ? '已收藏' : '收藏') + '</button></div><a class="detail-title" href="' + escapeHtml(item.html_url) + '" target="_blank" rel="noopener">' + escapeHtml(item.full_name) + ' ↗</a><p class="detail-desc">' + escapeHtml(item.description) + '</p><a class="radar-analysis-link" href="index.html?libraryRepo=' + encodeURIComponent(item.full_name) + '">✦ 交给航标研究</a><div class="detail-stats"><span><b>★ ' + countText(item.stars) + '</b><small>GitHub Stars</small></span><span><b>' + dateText(item.starred_at) + '</b><small>加星日期</small></span><span><b>' + dateText(item.pushed_at) + '</b><small>最近更新</small></span><span><b>' + escapeHtml(item.language) + '</b><small>主要语言</small></span></div><section class="detail-section"><h2>Topics</h2><div class="topic-list">' + topics + '</div></section><section class="detail-section"><h2>我的标签</h2><div class="tag-editor-list">' + tags + '</div><div class="tag-add"><input id="newTag" maxlength="32" placeholder="添加标签，按 Enter"><button id="addTag">添加</button></div></section><section class="detail-section"><h2>我的笔记</h2><div class="note-editor"><textarea id="projectNote" maxlength="1000" placeholder="为什么收藏它？下一步想做什么？">' + escapeHtml(meta.note) + '</textarea><button class="note-save" id="saveNote">保存笔记</button></div></section>';
    $('#favoriteButton').addEventListener('click', function () { meta.favorite = !meta.favorite; metadataSave(item.full_name, meta).then(render); });
    function addTag() { var input = $('#newTag'); var tag = input.value.trim(); if (!tag || meta.tags.indexOf(tag) !== -1) return; meta.tags.push(tag); metadataSave(item.full_name, meta).then(render); }
    $('#addTag').addEventListener('click', addTag); $('#newTag').addEventListener('keydown', function (event) { if (event.key === 'Enter') { event.preventDefault(); addTag(); } });
    Array.prototype.forEach.call(document.querySelectorAll('[data-remove-tag]'), function (button) { button.addEventListener('click', function () { meta.tags = meta.tags.filter(function (tag) { return tag !== button.dataset.removeTag; }); metadataSave(item.full_name, meta).then(render); }); });
    $('#saveNote').addEventListener('click', function () { meta.note = $('#projectNote').value.trim(); metadataSave(item.full_name, meta).then(function () { status('笔记已保存'); render(); }); });
  }

  function renderActiveFilters() {
    var filters = []; if (state.query) filters.push('搜索：' + state.query); if (state.language) filters.push(state.language); if (state.tag) filters.push('#' + state.tag); if (state.favorites) filters.push('仅收藏'); if (state.untagged) filters.push('未标注');
    $('#activeFilters').hidden = !filters.length; $('#activeFilters').innerHTML = filters.map(function (filter) { return '<span class="filter-chip">' + escapeHtml(filter) + '</span>'; }).join('') + '<button class="filter-reset" id="resetActive">清除全部筛选</button>';
    if ($('#resetActive')) $('#resetActive').addEventListener('click', clearFilters);
  }

  function render() {
    $('#libraryCount').textContent = state.items.length ? state.items.length : '—';
    $('#filterFavorites').checked = state.favorites; $('#filterUntagged').checked = state.untagged;
    $('#sortSelect').value = state.sort; $('#sortDirection').textContent = state.direction === 'desc' ? '↓' : '↑';
    renderActiveFilters(); renderFilters(); renderRows(filtered()); renderDetail(); renderBatchTools(); renderSelectAll(filtered());
    $('#pager').hidden = !state.items.length; $('#pageLabel').textContent = '第 ' + state.page + ' 页'; $('#prevPage').disabled = state.page <= 1; $('#nextPage').disabled = !state.hasNext;
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

  function githubRequest(path) {
    var token = localStorage.getItem(TOKEN_KEY) || '';
    if (!token) return Promise.reject(new Error('请先在 StarRadar 首页登录 GitHub'));
    return fetch('https://api.github.com' + path, { headers: { Authorization: 'token ' + token, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' } })
      .then(function (response) { return response.json().then(function (body) { if (!response.ok) throw new Error((body && body.message) || 'GitHub 请求失败'); return body; }); });
  }

  function openDrawer(kicker, title, html) {
    $('#drawerKicker').textContent = kicker;
    $('#drawerTitle').textContent = title;
    $('#drawerBody').innerHTML = html;
    $('#workspaceDrawer').hidden = false;
  }

  function closeDrawer() { $('#workspaceDrawer').hidden = true; }

  function topSignals() {
    var topics = {}, languages = {};
    state.items.forEach(function (item) {
      languages[item.language] = (languages[item.language] || 0) + 1;
      item.topics.forEach(function (topic) { topics[topic] = (topics[topic] || 0) + 1; });
    });
    return { topic: Object.keys(topics).sort(function (a, b) { return topics[b] - topics[a]; })[0] || '', language: Object.keys(languages).sort(function (a, b) { return languages[b] - languages[a]; })[0] || '' };
  }

  function renderForYou() {
    openDrawer('FOR YOU · 基于你的星标', '为你发现', '<p class="drawer-intro">从你的 Stars 中提取主题与语言偏好，寻找尚未收藏且近期活跃的项目。</p><button class="drawer-action" id="loadForYou">生成推荐</button><span class="drawer-status" id="drawerStatus"></span><div id="drawerResults"></div>');
    $('#loadForYou').addEventListener('click', function () {
      var button = this; button.disabled = true; $('#drawerStatus').textContent = '正在从 GitHub 寻找候选项目…';
      var signal = topSignals();
      var qualifiers = [signal.topic ? 'topic:' + encodeURIComponent(signal.topic) : '', signal.language && signal.language !== '未标注' ? 'language:' + encodeURIComponent(signal.language) : '', 'stars:>30', 'fork:false', 'archived:false'].filter(Boolean).join('+');
      githubRequest('/search/repositories?q=' + qualifiers + '&sort=updated&order=desc&per_page=24').then(function (body) {
        var starred = {}; state.items.forEach(function (item) { starred[item.full_name] = true; });
        var result = (body.items || []).filter(function (item) { return !starred[item.full_name]; }).slice(0, 8);
        $('#drawerStatus').textContent = result.length ? '根据「' + (signal.topic || signal.language || '近期收藏') + '」找到 ' + result.length + ' 个候选。' : '没有找到新的候选，换一页 Stars 后再试。';
        $('#drawerResults').innerHTML = result.map(function (item) { return '<article class="recommendation-card"><a href="' + escapeHtml(item.html_url) + '" target="_blank" rel="noopener">' + escapeHtml(item.full_name) + ' ↗</a><p>' + escapeHtml(item.description || '暂无项目描述。') + '</p><small>★ ' + countText(item.stargazers_count) + ' · ' + escapeHtml(item.language || '未标注') + ' · ' + (item.topics || []).slice(0, 3).map(escapeHtml).join(' · ') + '</small></article>'; }).join('');
      }).catch(function (error) { $('#drawerStatus').textContent = error.message || '暂时无法生成推荐'; }).finally(function () { button.disabled = false; });
    });
  }

  function renderFollowing() {
    openDrawer('FOLLOWING · 公开动态', '关注的人最近收藏', '<p class="drawer-intro">读取你关注的账号最近公开 Star；只展示公开数据，首次查询会稍慢。</p><button class="drawer-action" id="loadFollowing">读取最近动态</button><span class="drawer-status" id="drawerStatus"></span><div id="drawerResults"></div>');
    $('#loadFollowing').addEventListener('click', function () {
      var button = this; button.disabled = true; $('#drawerStatus').textContent = '正在读取关注列表…';
      githubRequest('/user/following?per_page=12').then(function (people) {
        if (!people.length) throw new Error('未发现可读取的关注账号');
        return Promise.all(people.slice(0, 8).map(function (person) {
          return githubRequest('/users/' + encodeURIComponent(person.login) + '/starred?per_page=8&sort=created&direction=desc').then(function (stars) { return { person: person, stars: stars.slice(0, 3) }; });
        }));
      }).then(function (groups) {
        var rows = [];
        groups.forEach(function (group) { group.stars.forEach(function (repo) { rows.push({ person: group.person, repo: repo }); }); });
        $('#drawerStatus').textContent = '已读取 ' + groups.length + ' 位关注者的近期公开收藏。';
        $('#drawerResults').innerHTML = rows.map(function (row) { return '<article class="following-card"><a href="' + escapeHtml(row.repo.html_url) + '" target="_blank" rel="noopener">' + escapeHtml(row.repo.full_name) + ' ↗</a><p>' + escapeHtml(row.repo.description || '暂无项目描述。') + '</p><small>' + escapeHtml(row.person.login) + ' 最近收藏 · ★ ' + countText(row.repo.stargazers_count) + '</small></article>'; }).join('') || '<p class="drawer-intro">这些账号近期没有公开的 Star 动态。</p>';
      }).catch(function (error) { $('#drawerStatus').textContent = error.message || '暂时无法读取 Following'; }).finally(function () { button.disabled = false; });
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
  function loadCubbyJob() { var job = localGet(CUBBY_JOB_KEY, null); if (job && job.status === 'analyzing') { job.status = 'paused'; job.error = '页面曾在分析中断开；已保留进度，可从下一批继续。'; localSet(CUBBY_JOB_KEY, job); } if (job && job.status === 'applying') { job.status = 'review'; job.error = '上一次应用过程被中断；请先核对建议与现有标签后，再次确认应用。'; localSet(CUBBY_JOB_KEY, job); } return job; }
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
  function renderCubbyReceipt() { var receipt = cubbyJob.receipt || { changed: 0, skipped: 0, unchanged: 0 }; return '<div class="cubby-receipt"><p class="cubby-kicker">执行回执 · 本地元数据</p><h3>本次整理已完成</h3><p>标签只写入航标 Beacon 的本地元数据，不会修改 GitHub 的 Star。应用前已比对本次冻结范围；期间发生本地编辑的项目会被跳过。</p><div class="cubby-receipt-grid"><span><b>' + receipt.changed + '</b><small>已更新项目</small></span><span><b>' + receipt.unchanged + '</b><small>没有新增标签</small></span><span><b>' + receipt.skipped + '</b><small>因数据变化而跳过</small></span></div><button class="cubby-primary" id="cubbyDone">返回星标库</button><button class="cubby-secondary" id="cubbyNewJob">新建整理任务</button></div>'; }
  function renderCubbyOrganize() {
    if (!cubbyJob) cubbyJob = { status: 'scope', scope: frozenScope(), proposals: [] };
    renderCubbyStages(); var container = $('#cubbyOrganizeContent');
    $('#cubbyScopeHint').textContent = cubbyJob.scope && cubbyJob.scope.length ? '已锁定范围：' + cubbyJob.scope.length + ' 个仓库' : '未锁定范围';
    container.innerHTML = cubbyJob.status === 'scope' ? renderCubbyScope() : (cubbyJob.status === 'analyzing' || cubbyJob.status === 'paused') ? renderCubbyAnalyze() : cubbyJob.status === 'review' ? renderCubbyReview() : renderCubbyReceipt();
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
      return Promise.all(writes).then(function () { cubbyJob.status = 'receipt'; cubbyJob.receipt = receipt; cubbyJob.completed_at = new Date().toISOString(); saveCubbyJob(); renderCubbyOrganize(); });
    }).catch(function (error) { cubbyJob.status = 'review'; cubbyJob.error = error.message || '应用失败'; saveCubbyJob(); renderCubbyOrganize(); });
  }

  function renderCubby() {
    openCubbyOrganize();
  }

  function renderTableLayout() {
    openDrawer('TABLE LAYOUT · 本机保存', '自定义表格列', '<p class="drawer-intro">直接拖拽表头可调整列顺序；拖动表头右侧细线可调宽。这里控制列显隐与所有者头像。</p><div id="layoutControls"></div><button class="drawer-action" id="resetLayout">恢复默认布局</button>');
    $('#layoutControls').innerHTML = state.layout.columns.map(function (key) { return '<label class="cubby-proposal"><input type="checkbox" data-layout-column="' + key + '"' + (state.layout.visible[key] ? ' checked' : '') + (key === 'repo' ? ' disabled' : '') + '><span><b>' + COLUMN_LABELS[key] + '</b><span>宽度 ' + Math.round(state.layout.widths[key]) + ' px</span></span></label>'; }).join('') + '<label class="cubby-proposal"><input type="checkbox" id="ownerAvatarToggle"' + (state.layout.ownerAvatar ? ' checked' : '') + '><span><b>显示所有者头像</b><span>仅在「所有者」列可见时生效</span></span></label>';
    Array.prototype.forEach.call(document.querySelectorAll('[data-layout-column]'), function (input) { input.addEventListener('change', function () { state.layout.visible[input.dataset.layoutColumn] = input.checked; saveLayout(); render(); }); });
    $('#ownerAvatarToggle').addEventListener('change', function () { state.layout.ownerAvatar = this.checked; saveLayout(); render(); });
    $('#resetLayout').addEventListener('click', function () { state.layout = JSON.parse(JSON.stringify(DEFAULT_LAYOUT)); saveLayout(); closeDrawer(); render(); });
  }

  function clearFilters() { state.query = ''; state.language = ''; state.tag = ''; state.favorites = false; state.untagged = false; $('#librarySearch').value = ''; $('#clearSearch').hidden = true; render(); }
  $('#librarySearch').addEventListener('input', function () { state.query = this.value; $('#clearSearch').hidden = !state.query; render(); });
  $('#clearSearch').addEventListener('click', function () { $('#librarySearch').value = ''; state.query = ''; this.hidden = true; render(); });
  $('#filterFavorites').addEventListener('change', function () { state.favorites = this.checked; render(); });
  $('#filterUntagged').addEventListener('change', function () { state.untagged = this.checked; render(); });
  $('#clearFilters').addEventListener('click', clearFilters);
  $('#sortSelect').addEventListener('change', function () { state.sort = this.value; render(); });
  $('#sortDirection').addEventListener('click', function () { state.direction = state.direction === 'desc' ? 'asc' : 'desc'; render(); });
  $('#syncStars').addEventListener('click', function () { loadStars(); });
  $('#autoTag').addEventListener('click', autoTagVisible);
  $('#forYou').addEventListener('click', renderForYou);
  $('#following').addEventListener('click', renderFollowing);
  $('#cubby').addEventListener('click', renderCubby);
  $('#cubbyFloat').addEventListener('click', renderCubby);
  $('#cubbyFloat').addEventListener('mouseenter', function () { $('#cubbyMascot').src = 'assets/better-stars/cubby-working.gif'; });
  $('#cubbyFloat').addEventListener('mouseleave', function () { $('#cubbyMascot').src = 'assets/better-stars/cubby-static.png'; });
  $('#tableLayout').addEventListener('click', renderTableLayout);
  $('#drawerClose').addEventListener('click', closeDrawer);
  $('#cubbyOrganizeClose').addEventListener('click', closeCubbyOrganize);
  $('#cubbyOrganize').addEventListener('click', function (event) { if (event.target === this) closeCubbyOrganize(); });
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

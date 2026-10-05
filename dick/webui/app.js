'use strict';

/* DICK Web GUI 前端：无框架，纯 DOM + fetch。所有用户数据都用 textContent 写入，避免注入。 */

const SUGGESTIONS = ['firefox', 'gimp', 'vscode', 'neovim', 'blender', 'docker', 'htop', 'wps'];
const TRANSLATION_LIMIT = 400;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const state = {
  status: null,
  sources: [],
  filter: new Set(),
  packages: [],
  installed: [],
  translations: new Map(),
  sort: 'relevance',
  translateOn: false,
  job: null,
  history: [],
  view: 'home',
  featured: null,
  featuredAt: 0,
  category: null,
  token: '',
};

/* ------------------------------------------------------------------ 工具 */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/* 访问令牌：存在 localStorage，随每个请求走 X-Dick-Token 头（图片走 ?token=）。 */
function storedToken() {
  try { return localStorage.getItem('dick.token') || ''; } catch (error) { return ''; }
}

function applyToken(value) {
  // 令牌只会是十六进制串：顺手挡掉粘贴带来的空格/中文，否则 fetch 会因请求头不是 latin-1 直接报错
  state.token = String(value || '').replace(/[^\x21-\x7e]/g, '');
  try {
    if (state.token) localStorage.setItem('dick.token', state.token);
    else localStorage.removeItem('dick.token');
  } catch (error) { /* 忽略隐私模式 */ }
  const field = $('#tokenValue');
  if (field) field.value = state.token;
}

async function api(path, options = {}) {
  const { method = 'GET', query, body } = options;
  let url = path;
  if (query) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === '') continue;
      params.set(key, Array.isArray(value) ? value.join(',') : String(value));
    }
    const text = params.toString();
    if (text) url += `?${text}`;
  }
  const init = { method, headers: {} };
  if (state.token) init.headers['X-Dick-Token'] = state.token;
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const response = await fetch(url, init);
  let payload = null;
  try { payload = await response.json(); } catch (error) { payload = null; }
  if (!response.ok) {
    if (response.status === 401 && payload && payload.code === 'token') openTokenGate();
    throw new Error((payload && payload.error) || `${response.status} ${response.statusText}`);
  }
  return payload;
}

function toast(message, kind = '') {
  const node = el('div', { class: `toast ${kind}`.trim(), text: message });
  $('#toasts').append(node);
  setTimeout(() => node.remove(), kind === 'err' ? 8000 : 3600);
}

const withToken = (url) => {
  if (!url) return '';
  if (!state.token) return url;
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(state.token)}`;
};

// 图片请求带不了请求头，令牌只能塞进查询串。
const iconUrl = (source, name) => withToken(
  `/api/icon?source=${encodeURIComponent(source)}&name=${encodeURIComponent(name)}`);

// 后端返回的 record.icon / detail.icon / candidate.icon 是拼好的地址（不含令牌），
// 精选页、详情页、重装候选框原来直接把它的值当 src，于是这些 <img> 全被 401 挡掉。
// 所有图标都从这里过一道，令牌换了也能立刻生效（地址是渲染时拼的，不存缓存）。
const iconSrc = (record) => {
  if (!record) return '';
  if (record.icon) return withToken(record.icon);
  if (record.source && record.name) return iconUrl(record.source, record.name);
  return '';
};

const selectedSources = () => (state.filter.size ? Array.from(state.filter) : undefined);

/* ------------------------------------------------------------ 令牌门与提权框 */

function openTokenGate(message) {
  // 手里已经有一个令牌却还是被拒，那就是它不对——这个时候别再说「去横幅里看」
  $('#tokenError').textContent = message
    || (state.token ? '令牌不对（或者后端换过令牌了），请重新输入。' : '');
  $('#tokenGate').hidden = false;
  const input = $('#tokenInput');
  if (document.activeElement !== input) { input.value = state.token || ''; input.focus(); }
}

function closeTokenGate() { $('#tokenGate').hidden = true; }

function submitToken(value) {
  const raw = String(value || '').trim();
  applyToken(raw);
  if (raw !== state.token) toast('令牌里的空格或非英文字符已忽略');
  closeTokenGate();
  bootstrap();
}

/* 提权：只有真正要执行（非演练）的安装/卸载/升级才会问密码。 */
function askPassword(job, message) {
  const gate = $('#passGate');
  const form = $('#passForm');
  const input = $('#passInput');
  $('#passError').textContent = message || '';
  input.value = '';
  gate.hidden = false;
  input.focus();
  form.onsubmit = (event) => {
    event.preventDefault();
    gate.hidden = true;
    form.onsubmit = null;
    startJob({ ...job.request, dry_run: false, confirm: true, password: input.value }, job.options);
    input.value = '';
  };
  $('#passCancel').onclick = () => { gate.hidden = true; form.onsubmit = null; input.value = ''; };
}

/* ------------------------------------------------------------------ 主题与译文缓存 */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('dick.theme', theme); } catch (error) { /* 忽略隐私模式 */ }
}

function restoreTheme() {
  let theme = 'light';
  try { theme = localStorage.getItem('dick.theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); }
  catch (error) { /* 忽略 */ }
  applyTheme(theme);
}

const translationKey = (text) => `${(state.ai && state.ai.target) || '中文'}\u0000${text}`;

function restoreTranslations() {
  try {
    const stored = JSON.parse(localStorage.getItem('dick.translations') || '{}');
    for (const [key, value] of Object.entries(stored)) state.translations.set(key, value);
  } catch (error) { /* 忽略 */ }
}

function saveTranslations() {
  try {
    const entries = Array.from(state.translations.entries()).slice(-TRANSLATION_LIMIT);
    localStorage.setItem('dick.translations', JSON.stringify(Object.fromEntries(entries)));
  } catch (error) { /* 忽略 */ }
}

/* ------------------------------------------------------------------ 视图切换 */

function showView(name) {
  state.view = name;
  for (const button of $$('#nav .nav-item')) button.classList.toggle('is-active', button.dataset.view === name);
  for (const section of $$('.view')) section.hidden = section.id !== `view-${name}`;
  $('#filters').hidden = name !== 'search';
  if (name === 'home') loadFeatured().catch((error) => toast(error.message, 'err'));
  if (name === 'settings') $('#tokenValue').value = state.token;
  if (name === 'browse') renderBrowse();
  if (name === 'installed') loadInstalled();
  if (name === 'settings') loadStatus().catch((error) => toast(error.message, 'err'));
  if (name === 'updates') renderHistory();
}

function renderSuggestions() {
  const box = $('#homeSuggest');
  box.replaceChildren(el('span', { text: '热门搜索：' }));
  for (const word of SUGGESTIONS) {
    box.append(el('button', { class: 'chip', text: word, onclick: () => searchFor(word) }));
  }
}

function searchFor(text) {
  $('#searchInput').value = text;
  runSearch();
}

/* ------------------------------------------------------------------ 精选首页 */

const FEATURED_TTL = 300000;

const appPkg = (record) => ({
  source: record.source, name: record.package, version: record.version,
  description: record.description, repository: record.repository,
  architecture: record.architecture, installed: record.installed,
  tagline: record.tagline, icon: record.icon, category: record.category,
});

async function loadFeatured(force = false) {
  if (state.featured && !force && Date.now() - state.featuredAt < FEATURED_TTL) return state.featured;
  const shelves = $('#homeShelves');
  if (!state.featured) {
    shelves.replaceChildren(el('div', { class: 'shelf-loading' }, [
      el('span', { class: 'spinner' }), el('span', { text: '正在解析本机索引与精选应用…' }),
    ]));
  }
  try {
    const data = await api('/api/featured', { query: { sources: selectedSources() } });
    state.featured = data;
    state.featuredAt = Date.now();
    renderHome();
    if (data.errors && data.errors.length) toast(data.errors.join('；'), 'err');
    return data;
  } catch (error) {
    const stale = String(error.message || '').includes('未知接口');
    shelves.replaceChildren(el('div', { class: 'empty' }, [
      el('p', { text: stale ? '这个 dick web 进程还是旧版本，没有 /api/featured 接口。' : `无法读取精选应用：${error.message}` }),
      el('p', { class: 'muted small', text: stale ? '在运行它的终端里按 Ctrl+C，然后重新执行 `dick web` 即可看到精选主页。' : '' }),
    ]));
    throw error;
  }
}

function renderHome() {
  const data = state.featured;
  if (!data) return;
  const hero = $('#homeHero');
  hero.replaceChildren();
  if (data.hero) hero.append(heroCard(data.hero));
  const shelves = $('#homeShelves');
  shelves.replaceChildren();
  for (const section of data.sections) {
    if (!section.apps.length) continue;
    shelves.append(shelf(section));
  }
  renderSuggestions();
}

function heroCard(record) {
  const body = el('div', { class: 'hero-body' }, [
    el('span', { class: 'hero-kicker', text: '今日精选' }),
    el('h1', { class: 'hero-title', text: record.name }),
    el('p', { class: 'hero-tagline', text: record.tagline }),
    el('div', { class: 'pkg-meta' }, [
      el('span', { class: 'badge', text: record.category_title }),
      record.source ? el('span', { class: 'badge source', text: record.source }) : null,
      record.version ? el('span', { class: 'badge', text: record.version }) : null,
      record.repository ? el('span', { class: 'badge', text: record.repository }) : null,
    ]),
    el('div', { class: 'hero-actions' }, [
      actionButton(record, 'btn btn-primary'),
      el('button', { class: 'btn', text: '查看详情', onclick: () => openApp(record) }),
    ]),
  ]);
  return el('article', { class: 'hero' }, [
    el('img', { class: 'hero-icon', src: iconSrc(record), alt: record.name }),
    body,
  ]);
}

function actionButton(record, className = 'pill') {
  if (!record.resolved) {
    return el('button', {
      class: className, text: '搜索',
      title: '本机索引里没有这个包，去搜索同名软件包',
      onclick: () => searchFor(record.name),
    });
  }
  if (record.installed) {
    return el('button', {
      class: `${className} is-installed`, text: '已安装',
      title: '查看详情或卸载',
      onclick: () => openApp(record),
    });
  }
  return el('button', {
    class: className, text: '获取',
    title: '安装前会先演练命令',
    onclick: () => installFlow(appPkg(record)),
  });
}

function shelf(section) {
  const head = el('div', { class: 'shelf-head' }, [
    el('h2', { class: 'shelf-title', text: section.title }),
    el('span', { class: 'shelf-sub muted', text: section.subtitle }),
  ]);
  if (section.id === 'featured') {
    head.append(el('span', { class: 'badge', text: `${section.apps.length} 款` }));
  } else {
    head.append(el('button', { class: 'link', text: '查看全部 ›', onclick: () => openCategory(section.id) }));
  }
  const row = el('div', { class: 'tile-row' });
  for (const app of section.apps) row.append(appTile(app));
  return el('section', { class: 'shelf' }, [head, row]);
}

function appTile(record) {
  const node = $('#tpl-tile').content.firstElementChild.cloneNode(true);
  node.dataset.key = record.key;
  const image = $('.tile-icon', node);
  image.src = iconSrc(record);
  image.alt = record.name;
  $('.tile-name', node).textContent = record.name;
  $('.tile-tagline', node).textContent = record.tagline;
  const meta = $('.tile-meta', node);
  if (record.source) meta.append(el('span', { class: 'badge source', text: record.source }));
  if (record.version) meta.append(el('span', { class: 'badge', text: record.version }));
  else if (!record.resolved) meta.append(el('span', { class: 'badge', text: '未收录' }));
  $('.tile-main', node).addEventListener('click', () => openApp(record));
  const foot = $('.tile-foot', node);
  foot.append(el('span', { class: 'tile-cat muted small', text: record.category_title }));
  foot.append(actionButton(record));
  return node;
}

function renderBrowse() {
  const grid = $('#categoryGrid');
  if (!state.featured) {
    grid.replaceChildren(el('div', { class: 'shelf-loading' }, [
      el('span', { class: 'spinner' }), el('span', { text: '正在读取分类…' }),
    ]));
    loadFeatured().catch((error) => toast(error.message, 'err'));
    return;
  }
  grid.replaceChildren();
  for (const category of state.featured.categories) {
    if (category.id === 'featured') continue;
    grid.append(el('button', { class: 'cat-card', onclick: () => openCategory(category.id) }, [
      el('span', { class: 'cat-icon', text: category.icon }),
      el('span', { class: 'cat-text' }, [
        el('strong', { text: category.title }),
        el('small', { class: 'muted', text: category.subtitle }),
      ]),
      el('span', { class: 'badge', text: `${category.count} 款` }),
    ]));
  }
}

function openCategory(id) {
  const data = state.featured;
  if (!data) return;
  const section = data.sections.find((item) => item.id === id);
  if (!section) return;
  state.category = id;
  $('#categoryTitle').textContent = section.title;
  const resolved = section.apps.filter((app) => app.resolved).length;
  $('#categoryMeta').textContent = section.subtitle
    + ` · 共 ${section.apps.length} 款，本机可安装 ${resolved} 款`;
  const grid = $('#categoryApps');
  grid.replaceChildren();
  for (const app of section.apps) grid.append(appTile(app));
  showView('category');
}

function relatedApps(category) {
  if (!category || !state.featured) return [];
  const section = state.featured.sections.find((item) => item.id === category);
  return section ? section.apps.slice(0, 8) : [];
}

async function openApp(record) {
  if (record.resolved) { openDetail(appPkg(record)); return; }
  openDrawer([
    el('div', { class: 'drawer-head' }, [
      el('img', { src: iconSrc(record), alt: record.name }),
      el('div', { class: 'drawer-body' }, [
        el('h2', { class: 'drawer-title', text: record.name }),
        el('p', { class: 'muted', text: record.tagline }),
        el('div', { class: 'pkg-meta' }, [
          el('span', { class: 'badge', text: record.category_title }),
          el('span', { class: 'badge', text: '本机未收录' }),
        ]),
      ]),
    ]),
    el('p', { class: 'muted', text: '当前启用的来源里没有找到这个包，可能是名称不同或来源被禁用。' }),
    el('div', { class: 'btn-row' }, [
      el('button', {
        class: 'btn btn-primary', text: '搜索同名软件包',
        onclick: () => { closeDrawer(); searchFor(record.name); },
      }),
    ]),
  ]);
}


/* ------------------------------------------------------------------ 来源 */

async function loadSources(scan = false) {
  const data = await api('/api/sources', { query: { scan: scan ? 1 : undefined } });
  state.sources = data.sources;
  renderSources();
  renderChips();
  renderSettingsSources();
  if (data.errors && data.errors.length) toast(`来源提示：${data.errors.join('；')}`, 'err');
}

function renderSources() {
  const list = $('#sourceList');
  list.replaceChildren();
  for (const item of state.sources) {
    const row = el('li', { class: item.enabled ? '' : 'is-off' });
    row.append(el('span', { class: `dot ${item.available ? '' : 'off'}` }));
    const label = el('button', {
      class: 'link', text: item.source, title: '按来源过滤结果',
      onclick: () => { toggleFilter(item.source); },
    });
    label.style.color = state.filter.has(item.source) ? 'var(--accent-strong)' : 'inherit';
    label.style.fontWeight = state.filter.has(item.source) ? '600' : '400';
    row.append(label);
    const count = item.repositories.length;
    row.append(el('span', {
      class: 'count',
      text: count ? `${count} 仓库` : (item.available ? '无仓库' : '不可用'),
    }));
    row.append(el('input', {
      type: 'checkbox', title: item.enabled ? '禁用该来源' : '启用该来源',
      checked: item.enabled, onchange: (event) => switchSource(item.source, event.target.checked),
    }));
    list.append(row);
  }
}

function renderChips() {
  const box = $('#sourceChips');
  box.replaceChildren();
  box.append(el('button', {
    class: `chip ${state.filter.size ? '' : 'is-on'}`, text: '全部',
    onclick: () => { state.filter.clear(); renderChips(); renderSources(); },
  }));
  for (const item of state.sources) {
    if (!item.enabled) continue;
    const chip = el('button', {
      class: `chip ${state.filter.has(item.source) ? 'is-on' : ''}`,
      title: item.available ? '' : '原生管理器不可用',
      onclick: () => { toggleFilter(item.source); },
    });
    chip.append(el('span', { class: 'chip-dot' }), item.source);
    box.append(chip);
  }
}

function toggleFilter(source) {
  if (state.filter.has(source)) state.filter.delete(source);
  else state.filter.add(source);
  renderChips();
  renderSources();
  if (state.view === 'search' && state.packages.length && $('#searchInput').value.trim()) runSearch({ stay: true });
}

function renderSettingsSources() {
  const list = $('#settingsSources');
  list.replaceChildren();
  for (const item of state.sources) {
    const row = el('li', { class: item.enabled ? '' : 'is-off' });
    const box = el('input', {
      type: 'checkbox', checked: item.enabled,
      onchange: (event) => switchSource(item.source, event.target.checked),
    });
    row.append(box, el('strong', { text: item.source }));
    const detail = item.repositories.length
      ? item.repositories.map((repository) => repository.name).join('、')
      : (item.available ? '无仓库信息' : '原生管理器不可用');
    row.append(el('span', { class: 'count', text: detail }));
    list.append(row);
  }
}

async function switchSource(source, enable) {
  try {
    await api(`/api/sources/${enable ? 'enable' : 'disable'}`, { method: 'POST', body: { sources: [source] } });
    toast(`${enable ? '已启用' : '已禁用'} ${source}`);
    if (!enable) state.filter.delete(source);
    await loadSources(false);
    await loadStatus();
  } catch (error) {
    toast(error.message, 'err');
    await loadSources(false);
  }
}

/* ------------------------------------------------------------------ 状态与 AI 设置 */

async function loadStatus() {
  const status = await api('/api/status');
  state.status = status;
  state.ai = status.ai;
  $('#familyLabel').textContent = `${status.family} · ${status.architecture}`;
  $('#configLabel').textContent = status.config_path;

  const info = $('#runtimeInfo');
  info.replaceChildren();
  const rows = [
    ['版本', `DICK ${status.version}`],
    ['系统', `${status.family} / ${status.architecture}`],
    ['优先级', status.priority.join(' → ')],
    ['配置文件', status.config_path],
    ['缓存目录', status.cache_dir],
    ['根目录', status.root],
    ['离线图标', status.offline ? '是' : '否'],
  ];
  for (const [key, value] of rows) {
    info.append(el('dt', { text: key }), el('dd', { text: value }));
  }

  const ai = status.ai;
  $('#aiBaseUrl').value = ai.base_url || '';
  $('#aiModel').value = ai.model || '';
  $('#aiTarget').value = ai.target || '';
  $('#aiTimeout').value = ai.timeout || 60;
  $('#aiPrompt').value = ai.prompt || '';
  $('#aiEnabled').checked = Boolean(ai.enabled);
  $('#aiState').textContent = ai.configured
    ? `已配置：${ai.model} · ${ai.base_url} · 目标语言 ${ai.target}`
    : '尚未配置（填写接口地址、模型与 API Key 后即可翻译包描述）。';
}

async function saveAi() {
  const body = {
    enabled: $('#aiEnabled').checked,
    base_url: $('#aiBaseUrl').value.trim(),
    model: $('#aiModel').value.trim(),
    api_key: $('#aiKey').value,
    target: $('#aiTarget').value.trim(),
    timeout: Number($('#aiTimeout').value || 60),
    prompt: $('#aiPrompt').value,
  };
  try {
    state.ai = await api('/api/ai', { method: 'POST', body });
    $('#aiKey').value = '';
    $('#aiState').textContent = state.ai.configured
      ? `已配置：${state.ai.model} · ${state.ai.base_url} · 目标语言 ${state.ai.target}`
      : '尚未配置（填写接口地址、模型与 API Key 后即可翻译包描述）。';
    toast('AI 设置已保存');
  } catch (error) {
    toast(error.message, 'err');
  }
}

async function testAi() {
  try {
    const result = await api('/api/ai/test', { method: 'POST', body: {} });
    $('#aiState').textContent = `连通正常，示例译文：${result.translation}`;
    toast(`AI 连通正常：${result.translation}`);
  } catch (error) {
    toast(`AI 测试失败：${error.message}`, 'err');
  }
}

/* ------------------------------------------------------------------ 搜索 */

async function runSearch(options = {}) {
  const query = $('#searchInput').value.trim();
  if (!query) { toast('请输入搜索关键词'); return; }
  if (!options.stay) showView('search');
  $('#searchTitle').textContent = query;
  $('#searchMeta').textContent = '搜索中…';
  $('#results').replaceChildren();
  $('#resultsEmpty').hidden = true;
  try {
    const data = await api('/api/search', {
      query: { q: query, sources: selectedSources(), exact: $('#exactToggle').checked ? 1 : undefined },
    });
    state.packages = data.packages;
    renderResults();
    const sources = data.sources.join('、');
    $('#searchMeta').textContent = data.total
      ? `找到 ${data.total} 个包（来源：${sources}）${data.total > data.packages.length ? `，显示前 ${data.packages.length} 个` : ''}`
      : `没有找到匹配的包（来源：${sources}）`;
    if (data.errors && data.errors.length) toast(data.errors.join('；'), 'err');
    if (state.translateOn) translateVisible();
  } catch (error) {
    $('#searchMeta').textContent = '搜索失败';
    toast(error.message, 'err');
  }
}

function renderResults() {
  const grid = $('#results');
  grid.replaceChildren();
  $('#resultsEmpty').hidden = state.packages.length > 0;
  if (!state.packages.length) {
    $('#resultsEmpty').replaceChildren(el('div', { class: 'empty', text: '没有结果。换个关键词，或在左侧切换来源。' }));
    return;
  }
  for (const pkg of sortedPackages()) grid.append(packageCard(pkg));
  paintTranslations();
}

function sortedPackages() {
  const list = [...state.packages];
  if (state.sort === 'name') list.sort((a, b) => a.name.localeCompare(b.name));
  else if (state.sort === 'source') list.sort((a, b) => a.source.localeCompare(b.source) || a.name.localeCompare(b.name));
  return list;
}

function packageCard(pkg) {
  const node = $('#tpl-package').content.firstElementChild.cloneNode(true);
  node.dataset.key = `${pkg.source}|${pkg.name}`;
  const image = $('.pkg-icon', node);
  image.src = iconSrc(pkg);
  image.alt = pkg.name;
  $('.pkg-name', node).textContent = pkg.name;
  const description = $('.pkg-desc', node);
  description.dataset.original = pkg.description || '';
  description.textContent = pkg.description || '（该来源没有提供描述）';
  const meta = $('.pkg-meta', node);
  meta.append(el('span', { class: 'badge source', text: pkg.source }));
  if (pkg.version) meta.append(el('span', { class: 'badge', text: pkg.version }));
  if (pkg.repository) meta.append(el('span', { class: 'badge', text: pkg.repository }));
  if (pkg.architecture) meta.append(el('span', { class: 'badge', text: pkg.architecture }));
  if (pkg.installed) meta.append(el('span', { class: 'badge ok', text: '已安装' }));

  const actions = $('.pkg-actions', node);
  actions.append(el('button', { class: 'btn', text: '详情', onclick: () => openDetail(pkg) }));
  if (pkg.installed) {
    actions.append(el('button', { class: 'btn btn-danger', text: '卸载', onclick: () => removeFlow(pkg.name) }));
  } else {
    actions.append(el('button', { class: 'btn btn-primary', text: '获取', onclick: () => installFlow(pkg) }));
  }
  return node;
}

/* ------------------------------------------------------------------ AI 翻译 */

async function translateVisible() {
  if (!state.translateOn) return;
  const pending = [];
  const seen = new Set();
  for (const pkg of [...state.packages, ...state.installed]) {
    const text = (pkg.description || '').trim();
    if (!text) continue;
    const key = translationKey(text);
    if (state.translations.has(key) || seen.has(key)) continue;
    seen.add(key);
    pending.push(text);
  }
  if (!pending.length) { paintTranslations(); return; }
  $('#searchMeta').textContent = `正在用 AI 翻译 ${pending.length} 条描述…`;
  try {
    const data = await api('/api/translate', { method: 'POST', body: { texts: pending } });
    pending.forEach((text, index) => state.translations.set(translationKey(text), data.translations[index]));
    saveTranslations();
    paintTranslations();
    $('#searchMeta').textContent = `已翻译 ${pending.length} 条描述（目标语言：${data.target}）`;
  } catch (error) {
    toast(`AI 翻译失败：${error.message}`, 'err');
    state.translateOn = false;
    $('#translateToggle').checked = false;
    $('#searchMeta').textContent = 'AI 翻译不可用，请到设置里配置接口。';
  }
}

function paintTranslations() {
  for (const node of $$('.pkg')) {
    const [source, name] = (node.dataset.key || '').split('|');
    const pkg = [...state.packages, ...state.installed].find((item) => item.source === source && item.name === name);
    if (!pkg) continue;
    const description = $('.pkg-desc', node);
    const text = (pkg.description || '').trim();
    const translated = text ? state.translations.get(translationKey(text)) : null;
    if (translated) {
      description.textContent = translated;
      description.classList.add('is-translated');
      description.title = `原文：${text}`;
      const meta = $('.pkg-meta', node);
      if (!$('.badge.ai', meta)) meta.append(el('span', { class: 'badge ai', text: 'AI 译文', title: text }));
    } else {
      description.textContent = pkg.description || '（该来源没有提供描述）';
      description.classList.remove('is-translated');
      description.removeAttribute('title');
    }
  }
}

/* ------------------------------------------------------------------ 详情抽屉 */

function openDrawer(children) {
  $('#drawerBody').replaceChildren(...[].concat(children));
  $('#drawer').hidden = false;
}

function closeDrawer() {
  $('#drawer').hidden = true;
  $('#drawerBody').replaceChildren();
}

async function openDetail(pkg) {
  openDrawer(el('p', { class: 'muted', text: '读取详情…' }));
  try {
    const detail = await api('/api/package', {
      query: {
        source: pkg.source, name: pkg.name, version: pkg.version,
        description: pkg.description, repository: pkg.repository, architecture: pkg.architecture,
      },
    });
    detail.tagline = pkg.tagline || '';
    detail.category = pkg.category || '';
    renderDetail(detail);
  } catch (error) {
    openDrawer(el('div', { class: 'empty', text: error.message }));
  }
}

function renderDetail(detail) {
  const head = el('div', { class: 'drawer-head' });
  head.append(el('img', { src: iconSrc(detail), alt: detail.name }));
  const headText = el('div', { class: 'drawer-headtext' }, [
    el('h2', { class: 'drawer-title', text: detail.name }),
    detail.tagline ? el('p', { class: 'drawer-tagline muted', text: detail.tagline }) : null,
    el('div', { class: 'pkg-meta' }, [
      el('span', { class: 'badge source', text: detail.source }),
      detail.version ? el('span', { class: 'badge', text: detail.version }) : null,
      detail.repository ? el('span', { class: 'badge', text: detail.repository }) : null,
      detail.architecture ? el('span', { class: 'badge', text: detail.architecture }) : null,
      detail.installed ? el('span', { class: 'badge ok', text: '已安装' }) : null,
    ]),
    el('div', { class: 'hero-actions' }, [
      detail.installed
        ? el('button', {
            class: 'btn', text: '卸载',
            onclick: () => { closeDrawer(); removeFlow(detail.name); },
          })
        : el('button', {
            class: 'btn btn-primary', text: '获取（先演练）',
            onclick: () => { closeDrawer(); installFlow(detail); },
          }),
    ]),
  ]);
  head.append(headText);

  const sections = [];
  const description = (detail.description || '').trim();
  const descriptionNode = el('p', { class: 'drawer-desc', text: description || '（该来源没有提供描述）' });
  const descriptionSection = el('section', { class: 'drawer-section' }, [
    el('h3', { text: '描述' }),
    descriptionNode,
  ]);
  if (description) {
    descriptionSection.append(el('button', {
      class: 'link', text: '用 AI 翻译这段描述',
      onclick: async (event) => {
        const button = event.target;
        button.disabled = true;
        button.textContent = '翻译中…';
        try {
          const data = await api('/api/translate', { method: 'POST', body: { texts: [description] } });
          const key = translationKey(description);
          state.translations.set(key, data.translations[0]);
          saveTranslations();
          descriptionNode.textContent = data.translations[0];
          descriptionNode.title = `原文：${description}`;
          button.textContent = '已翻译';
        } catch (error) {
          toast(error.message, 'err');
          button.disabled = false;
          button.textContent = '用 AI 翻译这段描述';
        }
      },
    }));
  }
  sections.push(descriptionSection);

  if (detail.install_command) {
    sections.push(el('section', { class: 'drawer-section' }, [
      el('h3', { text: '安装命令' }),
      el('pre', { class: 'command', text: detail.install_command.join(' ') }),
    ]));
  } else if (detail.install_note) {
    sections.push(el('section', { class: 'drawer-section' }, [
      el('h3', { text: '安装命令' }),
      el('p', { class: 'muted small', text: detail.install_note }),
    ]));
  }
  if (detail.remove_command) {
    sections.push(el('section', { class: 'drawer-section' }, [
      el('h3', { text: '卸载命令' }),
      el('pre', { class: 'command', text: detail.remove_command.join(' ') }),
    ]));
  }

  const rows = [['来源', detail.source], ['包名', detail.name]];
  if (detail.version) rows.push(['版本', detail.version]);
  if (detail.repository) rows.push(['仓库', detail.repository]);
  if (detail.architecture) rows.push(['架构', detail.architecture]);
  const dl = el('dl', { class: 'kv' });
  for (const [key, value] of rows) dl.append(el('dt', { text: key }), el('dd', { text: value }));
  sections.push(el('section', { class: 'drawer-section' }, [el('h3', { text: '信息' }), dl]));

  const related = relatedApps(detail.category);
  if (related.length) {
    const row = el('div', { class: 'tile-row compact' });
    for (const app of related) row.append(appTile(app));
    sections.push(el('section', { class: 'drawer-section' }, [
      el('h3', { text: '同分类推荐' }), row,
    ]));
  }

  const actions = el('div', { class: 'btn-row' });
  if (detail.install_command) {
    actions.append(el('button', {
      class: 'btn', text: '复制安装命令',
      onclick: () => copyText(detail.install_command.join(' ')),
    }));
  }
  actions.append(el('button', {
    class: 'btn', text: '搜索该来源',
    onclick: () => { closeDrawer(); $('#searchInput').value = detail.name; state.filter = new Set([detail.source]); renderChips(); renderSources(); runSearch(); },
  }));
  if (detail.category) {
    actions.append(el('button', {
      class: 'btn', text: '查看该分类',
      onclick: async () => {
        closeDrawer();
        try { await loadFeatured(); openCategory(detail.category); } catch (error) { toast(error.message, 'err'); }
      },
    }));
  }

  const body = el('div', { class: 'drawer-body' }, [...sections, actions]);
  openDrawer([head, body]);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制到剪贴板');
  } catch (error) {
    toast('复制失败，请手动选择命令', 'err');
  }
}

/* ------------------------------------------------------------------ 安装 */

function installFlow(pkg) {
  startJob({
    action: 'install', targets: [pkg.name], sources: [pkg.source], dry_run: true, confirm: false,
  }, { title: `安装 ${pkg.name}` });
}

/* ------------------------------------------------------------------ 卸载（先扫描已安装） */

async function removeFlow(target) {
  try {
    const data = await api('/api/candidates', { query: { target } });
    if (!data.total) { toast(`没有找到已安装的 ${target}`, 'err'); return; }
    if (data.total === 1) { startRemove([data.candidates[0]], target); return; }
    openRemoveChooser(data);
  } catch (error) {
    toast(error.message, 'err');
  }
}

function openRemoveChooser(data) {
  const list = el('div', { class: 'list' });
  const boxes = [];
  data.candidates.forEach((candidate, index) => {
    const box = el('input', { type: 'checkbox', checked: index === 0 });
    boxes.push({ box, candidate });
    const row = el('article', { class: 'pkg' }, [
      el('img', { class: 'pkg-icon', src: iconSrc(candidate), alt: candidate.name }),
      el('div', { class: 'pkg-body' }, [
        el('h3', { class: 'pkg-name', text: candidate.name }),
        el('p', { class: 'pkg-desc', text: candidate.description || '（没有描述）' }),
        el('div', { class: 'pkg-meta' }, [
          el('span', { class: 'badge source', text: candidate.source }),
          candidate.version ? el('span', { class: 'badge', text: candidate.version }) : null,
          el('span', { class: 'badge', text: `匹配度 ${candidate.rank}` }),
        ]),
      ]),
      el('div', { class: 'pkg-actions' }, [box]),
    ]);
    list.append(row);
  });
  const deep = el('input', { type: 'checkbox' });
  openDrawer([
    el('h2', { class: 'drawer-title', text: `卸载 ${data.target}` }),
    el('p', { class: 'muted', text: `匹配到 ${data.total} 个已安装的包，选择要卸载的条目：` }),
    list,
    el('label', { class: 'switch' }, [deep, el('span', { text: '深度清理（连带配置与孤立依赖）' })]),
    el('div', { class: 'btn-row' }, [
      el('button', {
        class: 'btn btn-primary', text: '演练卸载',
        onclick: () => {
          const chosen = boxes.filter((item) => item.box.checked).map((item) => ({
            source: item.candidate.source, name: item.candidate.name, version: item.candidate.version,
          }));
          if (!chosen.length) { toast('请至少选择一个包', 'err'); return; }
          closeDrawer();
          startJob({
            action: 'remove', targets: chosen.map((item) => item.name), packages: chosen,
            deep: deep.checked, dry_run: true, confirm: false,
          }, { title: `卸载 ${chosen.map((item) => item.name).join('、')}` });
        },
      }),
      el('button', { class: 'btn', text: '取消', onclick: closeDrawer }),
    ]),
  ]);
}

function startRemove(candidates, target) {
  const chosen = candidates.map((item) => ({ source: item.source, name: item.name, version: item.version }));
  startJob({
    action: 'remove', targets: [target], packages: chosen, dry_run: true, confirm: false,
  }, { title: `卸载 ${target}` });
}

/* ------------------------------------------------------------------ 已安装 */

async function loadInstalled() {
  $('#installedMeta').textContent = '读取中…';
  try {
    const data = await api('/api/installed', { query: { sources: selectedSources() } });
    state.installed = data.packages;
    renderInstalled();
    const groups = new Map();
    for (const pkg of data.packages) groups.set(pkg.source, (groups.get(pkg.source) || 0) + 1);
    $('#installedMeta').textContent = data.total
      ? `共 ${data.total} 个已安装的包：` + Array.from(groups).map(([source, count]) => `${source} ${count}`).join('、')
      : '没有读取到已安装的包。';
    if (data.errors && data.errors.length) toast(data.errors.join('；'), 'err');
  } catch (error) {
    $('#installedMeta').textContent = '读取失败';
    toast(error.message, 'err');
  }
}

function renderInstalled() {
  const filter = $('#installedFilter').value.trim().toLowerCase();
  const list = $('#installedList');
  list.replaceChildren();
  const items = state.installed.filter((pkg) => !filter || pkg.name.toLowerCase().includes(filter));
  if (!items.length) {
    list.append(el('div', { class: 'empty', text: filter ? '没有匹配的已安装包。' : '暂无已安装的包。' }));
    return;
  }
  for (const pkg of items) {
    const node = $('#tpl-package').content.firstElementChild.cloneNode(true);
    node.dataset.key = `${pkg.source}|${pkg.name}`;
    const image = $('.pkg-icon', node);
    image.src = iconSrc(pkg);
    image.alt = pkg.name;
    $('.pkg-name', node).textContent = pkg.name;
    $('.pkg-desc', node).textContent = pkg.description || '（没有描述）';
    const meta = $('.pkg-meta', node);
    meta.append(el('span', { class: 'badge source', text: pkg.source }));
    if (pkg.version) meta.append(el('span', { class: 'badge', text: pkg.version }));
    const actions = $('.pkg-actions', node);
    actions.append(el('button', {
      class: 'btn', text: '详情', onclick: () => openDetail({
        source: pkg.source, name: pkg.name, version: pkg.version,
        description: pkg.description, repository: '', architecture: '',
      }),
    }));
    actions.append(el('button', { class: 'btn btn-danger', text: '卸载', onclick: () => removeFlow(pkg.name) }));
    list.append(node);
  }
  if (state.translateOn) translateVisible();
}

/* ------------------------------------------------------------------ 任务与日志 */

function setJobPanel(open) {
  $('#jobpanel').hidden = !open;
  $('#jobTab').setAttribute('aria-expanded', String(open));
}

function setJobTab(state, text) {
  const tab = $('#jobTab');
  tab.dataset.state = state;
  const slot = $('#jobTabState');
  slot.replaceChildren(state === 'running'
    ? el('span', { class: 'spinner' })
    : document.createTextNode({ idle: '◷', ok: '✓', failed: '✗' }[state] || '◷'));
  $('#jobTabText').textContent = text;
}

function toggleJobPanel() {
  setJobPanel($('#jobpanel').hidden);
}

function hideJobDock() {
  setJobPanel(false);
  $('#jobdock').hidden = true;
}

function startJob(request, options = {}) {
  stopJob();
  api('/api/action', { method: 'POST', body: request }).then((created) => {
    state.job = { id: created.job.id, offset: 0, request, timer: null, options };
    const title = created.job.title || options.title || '任务';
    $('#jobdock').hidden = false;
    setJobPanel(true);
    setJobTab('running', title);
    $('#jobTitle').textContent = title;
    $('#jobLog').textContent = '';
    $('#jobActions').replaceChildren();
    const status = $('#jobStatus');
    status.className = 'badge';
    status.replaceChildren(el('span', { class: 'spinner' }), ' 运行中');
    pollJob();
  }).catch((error) => toast(error.message, 'err'));
}

function stopJob() {
  if (state.job && state.job.timer) clearTimeout(state.job.timer);
  state.job = null;
}

function closeJob() {
  // 只收起面板：右下角的标签还在，随时可以再展开看日志
  setJobPanel(false);
}

async function pollJob() {
  const job = state.job;
  if (!job) return;
  let snapshot;
  try {
    snapshot = await api(`/api/job/${job.id}`, { query: { since: job.offset } });
  } catch (error) {
    toast(error.message, 'err');
    hideJobDock();
    stopJob();
    return;
  }
  if (!state.job || state.job.id !== job.id) return;
  const log = $('#jobLog');
  for (const line of snapshot.lines) log.append(document.createTextNode(line + '\n'));
  job.offset = snapshot.offset;
  log.scrollTop = log.scrollHeight;
  if (snapshot.status === 'running') {
    job.timer = setTimeout(pollJob, 900);
    return;
  }
  const failed = snapshot.status === 'failed';
  job.timer = null;
  job.snapshot = snapshot;
  const status = $('#jobStatus');
  status.className = `badge ${failed ? 'warn' : 'ok'}`;
  status.textContent = failed ? '失败' : '完成';
  setJobTab(failed ? 'failed' : 'ok', $('#jobTitle').textContent);
  const summary = summarize(snapshot, job.request);
  const authFailed = !!(snapshot.result && snapshot.result.auth_failed);
  if (summary) log.append(document.createTextNode(`\n${summary}\n`));
  if (authFailed) {
    log.append(document.createTextNode(
      '\nsudo 不接受刚才的密码，所以命令没有执行。请重新输入，或在宿主终端里给 pacman 配一次免密。\n'));
  }
  log.scrollTop = log.scrollHeight;

  const actions = $('#jobActions');
  actions.replaceChildren();
  const mutating = ['install', 'remove', 'upgrade'].includes(job.request.action);
  if (authFailed) {
    actions.append(el('button', {
      class: 'btn btn-primary', text: '重新输入密码',
      onclick: () => askPassword(job),
    }));
    actions.append(el('button', { class: 'btn', text: '关闭', onclick: closeJob }));
  } else if (job.request.dry_run && !failed && mutating) {
    actions.append(el('button', {
      class: 'btn btn-primary', text: '确认执行',
      onclick: () => askPassword(job),
    }));
    actions.append(el('button', { class: 'btn', text: '取消', onclick: closeJob }));
  } else {
    actions.append(el('button', { class: 'btn', text: '关闭', onclick: closeJob }));
    if (!failed && mutating) {
      actions.append(el('button', {
        class: 'btn', text: '刷新列表',
        onclick: () => {
          state.featured = null;
          if (state.view === 'installed') loadInstalled();
          if (['home', 'category', 'browse'].includes(state.view)) {
            loadFeatured(true).then(() => {
              if (state.view === 'category' && state.category) openCategory(state.category);
            }).catch((error) => toast(error.message, 'err'));
          }
          if (state.packages.length) runSearch({ stay: true });
        },
      }));
    }
  }
  pushHistory({
    id: job.id, title: $('#jobTitle').textContent, status: failed ? 'failed' : 'done',
    elapsed: snapshot.elapsed, summary, at: new Date(),
  });
  if (!failed && !job.request.dry_run && mutating) {
    state.featured = null;
    if (state.view === 'installed') loadInstalled();
    if (state.packages.length) runSearch({ stay: true });
    if (['home', 'category', 'browse'].includes(state.view)) {
      loadFeatured(true).then(() => {
        if (state.view === 'category' && state.category) openCategory(state.category);
      }).catch((error) => toast(error.message, 'err'));
    }
  }
}

function summarize(snapshot, request) {
  const result = snapshot.result;
  if (!result) return snapshot.error ? `错误：${snapshot.error}` : '';
  if (request.action === 'install') {
    return result.results.map((item) => item.success
      ? `已安装 ${item.name}（来源 ${item.source}）`
      : `未能安装 ${item.name}：` + item.attempts.filter((attempt) => attempt.error)
        .map((attempt) => `${attempt.source} ${attempt.error}`).join('；')).join('\n');
  }
  if (request.action === 'remove') {
    return result.results.map((item) => item.success
      ? `${item.package} 已卸载（${item.source}）`
      : `${item.package || item.name} 未卸载：${item.error || `退出码 ${item.returncode}`}`).join('\n');
  }
  if (request.action === 'upgrade') {
    return result.success ? '整机升级命令执行成功。' : `升级命令退出码 ${result.returncode}。`;
  }
  if (request.action === 'update') {
    return `刷新了 ${result.refreshed.length} 个仓库${result.errors.length ? `，${result.errors.length} 个失败` : ''}。`;
  }
  return '';
}

function pushHistory(entry) {
  state.history.unshift(entry);
  state.history = state.history.slice(0, 20);
  if (state.view === 'updates') renderHistory();
}

function renderHistory() {
  const list = $('#jobHistory');
  list.replaceChildren();
  if (!state.history.length) {
    list.append(el('div', { class: 'empty', text: '还没有任务记录。' }));
    return;
  }
  for (const entry of state.history) {
    const failed = entry.status === 'failed';
    const row = el('article', { class: 'pkg' }, [
      el('div', { class: 'pkg-icon', style: 'display:grid;place-items:center;font-size:18px' }, [failed ? '⚠' : '✓']),
      el('div', { class: 'pkg-body' }, [
        el('h3', { class: 'pkg-name', text: entry.title }),
        el('p', { class: 'pkg-desc', text: entry.summary || (failed ? '任务失败' : '任务完成') }),
        el('div', { class: 'pkg-meta' }, [
          el('span', { class: `badge ${failed ? 'warn' : 'ok'}`, text: failed ? '失败' : '完成' }),
          el('span', { class: 'badge', text: `${entry.elapsed}s` }),
        ]),
      ]),
      el('div', { class: 'pkg-actions' }, [
        el('button', { class: 'btn', text: '日志', onclick: () => reopenJob(entry) }),
      ]),
    ]);
    list.append(row);
  }
}

async function reopenJob(entry) {
  try {
    const snapshot = await api(`/api/job/${entry.id}`, { query: { since: 0 } });
    $('#jobdock').hidden = false;
    setJobPanel(true);
    setJobTab(snapshot.status === 'failed' ? 'failed' : 'ok', snapshot.title);
    $('#jobTitle').textContent = snapshot.title;
    $('#jobLog').textContent = snapshot.lines.join('\n');
    const status = $('#jobStatus');
    status.className = `badge ${snapshot.status === 'failed' ? 'warn' : 'ok'}`;
    status.textContent = snapshot.status === 'failed' ? '失败' : '完成';
    $('#jobActions').replaceChildren(el('button', { class: 'btn', text: '关闭', onclick: closeJob }));
  } catch (error) {
    toast(error.message, 'err');
  }
}

/* ------------------------------------------------------------------ 事件绑定 */

function bindEvents() {
  for (const button of $$('#nav .nav-item')) button.addEventListener('click', () => showView(button.dataset.view));
  $('#categoryBack').addEventListener('click', () => showView('browse'));
  $('#searchButton').addEventListener('click', () => runSearch());
  $('#searchInput').addEventListener('keydown', (event) => { if (event.key === 'Enter') runSearch(); });
  $('#exactToggle').addEventListener('change', () => { if ($('#searchInput').value.trim()) runSearch({ stay: state.view !== 'search' }); });
  $('#sortSelect').addEventListener('change', (event) => { state.sort = event.target.value; renderResults(); });
  $('#translateToggle').addEventListener('change', (event) => {
    state.translateOn = event.target.checked;
    if (state.translateOn) translateVisible();
    else paintTranslations();
  });
  $('#installedFilter').addEventListener('input', renderInstalled);
  $('#installedRefresh').addEventListener('click', loadInstalled);
  $('#scanSources').addEventListener('click', async () => {
    try { await loadSources(true); toast('已重新扫描仓库'); } catch (error) { toast(error.message, 'err'); }
  });
  $('#themeToggle').addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });
  $('#aiSave').addEventListener('click', saveAi);
  $('#aiTest').addEventListener('click', testAi);
  $('#tokenForm').addEventListener('submit', (event) => {
    event.preventDefault();
    submitToken($('#tokenInput').value);
  });
  $('#tokenSave').addEventListener('click', () => {
    const value = $('#tokenValue').value.trim();
    applyToken(value);
    toast(value ? '令牌已保存到本机' : '已清除本机保存的令牌');
    state.featured = null;
    bootstrap();
  });
  $('#tokenClear').addEventListener('click', () => {
    applyToken('');
    toast('已清除本机保存的令牌');
    openTokenGate('');
  });
  $('#jobCollapse').addEventListener('click', closeJob);
  $('#jobTab').addEventListener('click', toggleJobPanel);
  $('#jobTab').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggleJobPanel(); }
  });
  $('#jobTabClose').addEventListener('click', (event) => { event.stopPropagation(); hideJobDock(); });
  for (const node of $$('[data-close="drawer"]')) node.addEventListener('click', closeDrawer);
  for (const button of $$('[data-action]')) {
    button.addEventListener('click', () => {
      startJob({ action: button.dataset.action, dry_run: true, confirm: false, sources: selectedSources() },
        { title: button.dataset.action === 'update' ? '刷新索引' : '整机升级' });
    });
  }
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { closeDrawer(); return; }
    if (event.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
      event.preventDefault();
      $('#searchInput').focus();
    }
  });
}

/* ------------------------------------------------------------------ 启动 */

async function init() {
  restoreTheme();
  restoreTranslations();
  bindEvents();
  // `dick web --open` 会把令牌拼在地址上：收下它，再从地址栏抹掉
  let fromUrl = null;
  try { fromUrl = new URLSearchParams(location.search).get('token'); } catch (error) { fromUrl = null; }
  if (fromUrl) {
    applyToken(fromUrl);
    try {
      const params = new URLSearchParams(location.search);
      params.delete('token');
      const query = params.toString();
      history.replaceState(null, '', location.pathname + (query ? `?${query}` : '') + location.hash);
    } catch (error) { /* 忽略 */ }
  } else {
    applyToken(storedToken());
  }
  await bootstrap();
}

async function bootstrap() {
  try {
    await Promise.all([loadStatus(), loadSources(false)]);
    if (state.status && state.status.ai && state.status.ai.configured) {
      $('#translateToggle').checked = false;
    }
  } catch (error) {
    if ($('#tokenGate').hidden) toast(`无法读取状态：${error.message}`, 'err');
  }
  if (!$('#tokenGate').hidden) return;  // 令牌门开着，等用户输入后再继续
  showView('home');
}

document.addEventListener('DOMContentLoaded', init);

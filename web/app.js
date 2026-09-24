const TASTE_KEY = "gitnote-taste-v2";
const REAL_KEY = "gitnote-real-preferences-v1";
const ITEMS_KEY = "gitnote-session-items-v1";
const MEMORY_KEY = "gitnote-query-memory-v1";
const CATALOG_KEY = "gitnote-known-items-v1";
for (const [storage, oldKeys, newKey] of [
  [localStorage, ["giterest-taste-v2", "open-wander-taste-v2"], TASTE_KEY],
  [localStorage, ["giterest-real-preferences-v1", "open-wander-real-preferences-v1"], REAL_KEY],
  [sessionStorage, ["giterest-session-items-v1", "open-wander-session-items-v1"], ITEMS_KEY],
  [sessionStorage, ["giterest-query-memory-v1", "open-wander-query-memory-v1"], MEMORY_KEY],
  [sessionStorage, ["giterest-known-items-v1"], CATALOG_KEY]
]) {
  try {
    if (!storage.getItem(newKey)) {
      for (const oldKey of oldKeys) {
        const value = storage.getItem(oldKey);
        if (value) { storage.setItem(newKey, value); break; }
      }
    }
  } catch (_) {}
}
const $ = selector => document.querySelector(selector);
const grid = $("#feedGrid");
const blankProfile = () => ({ v: 2, topics: {}, languages: {}, corpus: { topics: {}, documents: 0 }, updatedAt: Date.now() });
const readJSON = (storage, key, fallback) => { try { return JSON.parse(storage.getItem(key) || "null") || fallback; } catch (_) { return fallback; } };
const state = {
  mode: "recommended", topic: "", language: "", starBand: "",
  realPrefs: readJSON(localStorage, REAL_KEY, { liked: [], saved: [], hidden: [], seen: [], imported: false }),
  profile: readJSON(localStorage, TASTE_KEY, blankProfile()),
  items: readJSON(sessionStorage, ITEMS_KEY, []), memory: readJSON(sessionStorage, MEMORY_KEY, {}),
  catalog: readJSON(sessionStorage, CATALOG_KEY, []), viewItems: [],
  listings: { search: null, stars: null, trending: null }, period: "daily", searchSort: "best",
  previousMode: "recommended", auth: { configured: false, connected: false, login: null }, busy: false
};
let meta = null;
let requestNumber = 0;
let remoteRequest = 0;
let firstDiscoveryStarted = false;
let toastTimer;
let tasteQueue = Promise.resolve();
let observer;
const visibleStarts = new Map();
const openedIds = new Set();
const detailCache = new Map();
let detailRequest = 0;
let detailOpener = null;
const labels = {
  recommended: ["为你推荐", "依据你的兴趣继续探索，也留出发现新话题的机会。"],
  latest: ["最近更新", "按当前候选池的更新时间排序。"],
  stars: ["Stars 总榜", "GitHub 公开仓库累计星标排名。"],
  trending: ["近期 Trending", "GitHub Trending 原榜顺序与所选周期新增 Stars。"],
  search: ["搜索 GitHub", "搜索整个 GitHub 公开仓库索引，最多浏览前 1,000 条。"],
  saved: ["我的收藏", "想再次看见的项目都在这里。"]
};

function saveState() {
  try {
    localStorage.setItem(REAL_KEY, JSON.stringify(state.realPrefs));
    localStorage.setItem(TASTE_KEY, JSON.stringify(state.profile));
    sessionStorage.setItem(ITEMS_KEY, JSON.stringify(state.items.slice(-250)));
    sessionStorage.setItem(MEMORY_KEY, JSON.stringify(state.memory));
    sessionStorage.setItem(CATALOG_KEY, JSON.stringify(state.catalog.slice(-1000)));
  } catch (_) { showToast("浏览器未允许保存数据，本次浏览仍可使用。"); }
}

function rememberItems(items) {
  const known = new Map(state.catalog.map(item => [item.id, item]));
  for (const item of items) known.set(item.id, item);
  const savedIds = new Set(state.realPrefs.saved);
  const entries = [...known.values()];
  state.catalog = [...entries.filter(item => !savedIds.has(item.id)).slice(-500),
    ...entries.filter(item => savedIds.has(item.id)).slice(-500)];
  saveState();
}

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
}

async function postJSON(path, body) {
  const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || `请求失败 (${response.status})`);
    error.retryAt = data.retry_at;
    if (data.rate) showRate(data.rate);
    throw error;
  }
  return data;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 4500);
}

function showRate(rate) {
  const search = rate?.resources?.search;
  const retry = rate?.retry_at;
  const parts = [];
  if (search && search.limit != null) parts.push(`GitHub Search ${search.remaining ?? "—"}/${search.limit}`);
  else parts.push("GitHub 搜索额度将在首次请求后显示");
  if (retry && retry * 1000 > Date.now()) parts.push(`可重试：${new Date(retry * 1000).toLocaleTimeString()}`);
  $("#rateStatus").textContent = parts.join(" · ");
}

async function refreshAuth() {
  try {
    state.auth = await (await fetch("/api/auth/status")).json();
    $("#authButton").textContent = state.auth.connected ? `${state.auth.login} · 断开` : "连接 GitHub";
    $("#authButton").title = state.auth.configured ? "" : "先在 Python 服务环境中设置 GH_CLIENT_ID 和 GH_CLIENT_SECRET";
    $("#importStars").hidden = !state.auth.connected || state.realPrefs.imported;
  } catch (_) { /* Keep anonymous discovery available. */ }
}

function realArtwork(item) {
  const initials = escapeHTML((item.name || "G").slice(0, 2).toUpperCase());
  return `<div class="card-art repo-art"><div class="repo-art-grid"></div><div class="repo-art-icon">${initials}</div><div class="repo-art-bottom"><span>GITHUB REPOSITORY</span><span>↗</span></div></div>`;
}

function renderCard(item, index) {
  const prefs = state.realPrefs;
  const liked = prefs.liked.includes(item.id), saved = prefs.saved.includes(item.id);
  const palette = ["peach", "lavender", "mint", "blue", "yellow", "rose"][index % 6];
  const tags = (item.topics || []).slice(0, 5);
  const url = item.source_url?.startsWith("https://github.com/") ? item.source_url : "#";
  return `<article class="project-card palette-${palette}" data-id="${escapeHTML(item.id)}" tabindex="0" aria-label="查看 ${escapeHTML(item.name)} 详情">
    <div class="card-topline"><span class="source-label">✳ GitHub 仓库</span><button class="more-button" type="button" data-action="hide" data-id="${escapeHTML(item.id)}" title="不感兴趣" aria-label="对${escapeHTML(item.name)}不感兴趣">×</button></div>
    ${realArtwork(item)}
    <div class="card-body"><div class="card-meta"><span class="language-dot"></span>${escapeHTML(item.language || "未知语言")}<span class="meta-separator">·</span>${escapeHTML(item.author)}</div><h3>${escapeHTML(item.name)}</h3><p class="card-summary">${escapeHTML(item.summary)}</p><div class="card-tags">${tags.map(tag => `<button type="button" data-action="tag" data-value="${escapeHTML(tag)}"># ${escapeHTML(tag)}</button>`).join("")}</div><div class="reason"><span>✦</span>${escapeHTML(item.reason || "GitHub 搜索结果")}</div></div>
    <div class="card-footer"><span class="stars">☆ ${Number(item.stars || 0).toLocaleString("en-US")}${item.trending_stars != null ? ` <small>+${Number(item.trending_stars).toLocaleString("en-US")}</small>` : ""}</span><div class="card-actions"><button class="icon-action ${liked ? "selected" : ""}" type="button" data-action="like" data-id="${escapeHTML(item.id)}" aria-pressed="${liked}" aria-label="${liked ? "取消喜欢" : "喜欢"}${escapeHTML(item.name)}">${liked ? "♥" : "♡"}</button><button class="icon-action ${saved ? "selected" : ""}" type="button" data-action="save" data-id="${escapeHTML(item.id)}" aria-pressed="${saved}" aria-label="${saved ? "取消收藏" : "收藏"}${escapeHTML(item.name)}">${saved ? "▣" : "▢"}</button><button class="icon-action share-action" type="button" data-action="share" data-id="${escapeHTML(item.id)}" title="复制链接" aria-label="分享${escapeHTML(item.name)}">↥</button><a href="${escapeHTML(url)}" data-action="open" data-id="${escapeHTML(item.id)}" target="_blank" rel="noopener noreferrer" title="打开 GitHub 来源" aria-label="打开${escapeHTML(item.name)}的来源">↗</a></div></div>
  </article>`;
}

function updateHeading(total) {
  const listing = state.listings[state.mode];
  $("#feedTitle").innerHTML = `${state.mode === "search" ? `搜索：${escapeHTML(state.searchText || "")}` : labels[state.mode][0]} <span class="title-spark">✳</span>`;
  $("#feedSubtitle").textContent = labels[state.mode][1];
  $("#resultCount").textContent = total;
  $("#countLabel").textContent = "个 GitHub 项目";
  $("#discoverButton").hidden = state.mode !== "recommended";
  $("#importStars").hidden = !state.auth.connected || state.realPrefs.imported;
  $("#feedFooter").textContent = state.mode === "trending" ? `✳ GitHub Trending · ${listing?.fetched_at ? new Date(listing.fetched_at).toLocaleString() : ""}${listing?.stale ? " · 缓存数据" : ""}` : state.mode === "stars" ? "✳ GitHub Stars 累计排名 · 榜单缓存最多 2 小时" : "✳ 公开仓库数据来自 GitHub · 推荐参考 GitTok.dev";
  $("#searchNav").hidden = !state.searchText;
  document.querySelectorAll("[data-mode]").forEach(button => button.classList.toggle("active", button.dataset.mode === state.mode));
  $("#periodWrap").hidden = state.mode !== "trending";
  $("#sortWrap").hidden = state.mode !== "search";
  $("#trendingSource").hidden = state.mode !== "trending";
  if (listing?.source_url && state.mode === "trending") $("#trendingSource").href = listing.source_url;
  $("#loadMore").hidden = !(["search", "stars"].includes(state.mode)
    && listing?.key === listingKey(state.mode) && listing?.has_more);
  const filters = ["stars", "trending"].includes(state.mode) ? [["语言", state.language]]
    : [["话题", state.topic], ["语言", state.language], ["Stars", state.starBand]];
  $("#activeFilters").innerHTML = filters.filter(([, value]) => value).map(([name, value]) => `<span>${escapeHTML(name)}：${escapeHTML(value)}</span>`).join("");
}

function disconnectObserver() {
  if (observer) observer.disconnect();
  visibleStarts.clear();
}

function startObserver() {
  disconnectObserver();
  if (!window.IntersectionObserver) return;
  observer = new IntersectionObserver(entries => {
    if (document.visibilityState !== "visible") return;
    for (const entry of entries) {
      const id = entry.target.dataset.id;
      if (entry.intersectionRatio >= 0.5) {
        if (!visibleStarts.has(id)) visibleStarts.set(id, performance.now());
        if (!state.realPrefs.seen.includes(id)) {
          state.realPrefs.seen.push(id);
          state.realPrefs.seen = state.realPrefs.seen.slice(-2000);
          saveState();
        }
      } else if (visibleStarts.has(id)) {
        const elapsed = performance.now() - visibleStarts.get(id);
        visibleStarts.delete(id);
        const item = state.viewItems.find(project => project.id === id);
        const kind = elapsed >= 12000 ? "dwell_long" : elapsed >= 4000 ? "dwell_medium" : elapsed < 1500 ? "skip_fast" : null;
        if (item && kind) recordSignal(item, kind);
      }
    }
  }, { threshold: [0, 0.5, 1] });
  grid.querySelectorAll(".project-card").forEach(card => observer.observe(card));
}

async function render() {
  const thisRequest = ++requestNumber;
  grid.classList.add("loading");
  try {
    let items;
    const prefs = state.realPrefs;
    if (["search", "stars", "trending"].includes(state.mode)) {
      items = (state.listings[state.mode]?.items || []).filter(item => !prefs.hidden.includes(item.id));
    } else if (state.mode === "saved") {
      items = state.catalog.filter(item => prefs.saved.includes(item.id) && !prefs.hidden.includes(item.id));
    } else {
      items = state.items.filter(item => !prefs.hidden.includes(item.id)
        && (!state.topic || item.topics?.includes(state.topic))
        && (!state.language || item.language === state.language)
        && (!state.starBand || inStarBand(item.stars, state.starBand)));
    }
    if (state.mode === "recommended" && items.length) {
      items = (await postJSON("/api/rank", { profile: state.profile, items })).items;
    } else if (state.mode === "latest") items.sort((a, b) => String(b.updated).localeCompare(String(a.updated)));
    if (thisRequest !== requestNumber) return;
    disconnectObserver();
    state.viewItems = items;
    grid.innerHTML = items.map(renderCard).join("");
    $("#emptyState").hidden = items.length !== 0;
    updateHeading(items.length);
    startObserver();
  } catch (error) {
    if (thisRequest === requestNumber) {
      grid.innerHTML = `<div class="load-error">${escapeHTML(error.message || "本地服务暂时无法响应")}</div>`;
      $("#emptyState").hidden = true;
      $("#resultCount").textContent = "—";
    }
  } finally { if (thisRequest === requestNumber) grid.classList.remove("loading"); }
}

function inStarBand(stars, band) {
  const match = /^stars:(\d+)\.\.(\d+)$/.exec(band);
  return !match || (stars >= Number(match[1]) && stars <= Number(match[2]));
}

function recordSignal(item, kind) {
  if (!item) return tasteQueue;
  tasteQueue = tasteQueue.catch(() => {}).then(async () => {
    const result = await postJSON("/api/taste/signal", { profile: state.profile,
      signal: { kind, topics: item.topics || [], language: item.language || null,
        suppressed: state.topic ? [state.topic] : [] } });
    state.profile = result.profile;
    saveState();
  }).catch(() => {});
  return tasteQueue;
}

function recordOpen(item) {
  if (item && !openedIds.has(item.id)) { openedIds.add(item.id); recordSignal(item, "open"); }
}

function renderDetail(item, extra = null, error = "") {
  const link = item.source_url?.startsWith("https://github.com/") ? item.source_url : "#";
  const homepage = extra?.homepage && /^https?:\/\//i.test(extra.homepage) ? extra.homepage : "";
  $("#detailContent").innerHTML = `<div class="detail-kicker">GITHUB REPOSITORY</div>
    <h2 id="detailTitle">${escapeHTML(item.full_name || item.name)}</h2>
    <p class="detail-description">${escapeHTML(extra?.description || item.summary)}</p>
    <div class="detail-stats"><span>☆ ${Number(item.stars || 0).toLocaleString("en-US")} Stars</span>${item.trending_stars != null ? `<span>+${Number(item.trending_stars).toLocaleString("en-US")} 本期 Stars</span>` : ""}${extra ? `<span>⑂ ${Number(extra.forks || 0).toLocaleString("en-US")} Forks</span><span>${Number(extra.open_issues || 0).toLocaleString("en-US")} Issues</span>` : ""}</div>
    <div class="detail-meta"><span>作者：${escapeHTML(item.author)}</span><span>语言：${escapeHTML(item.language || "未知")}</span>${extra?.license ? `<span>许可证：${escapeHTML(extra.license)}</span>` : ""}${extra?.created_at ? `<span>创建：${escapeHTML(String(extra.created_at).slice(0, 10))}</span>` : ""}${item.updated ? `<span>更新：${escapeHTML(item.updated)}</span>` : ""}</div>
    ${(item.topics || []).length ? `<div class="detail-topics">${item.topics.map(topic => `<span># ${escapeHTML(topic)}</span>`).join("")}</div>` : ""}
    ${extra?.readme_html || extra?.readme_error ? `<section class="detail-readme"><h3>README</h3><div id="readmeMount"></div>${extra?.readme_error ? `<p class="detail-error">${escapeHTML(extra.readme_error)}</p>` : ""}<a href="${escapeHTML(link)}#readme" target="_blank" rel="noopener noreferrer">在 GitHub 阅读完整 README ↗</a></section>` : ""}
    ${error ? `<p class="detail-error">${escapeHTML(error)}</p>` : ""}
    <div class="detail-links"><a class="detail-primary" href="${escapeHTML(link)}" target="_blank" rel="noopener noreferrer">前往 GitHub ↗</a>${homepage ? `<a href="${escapeHTML(homepage)}" target="_blank" rel="noopener noreferrer">项目主页 ↗</a>` : ""}</div>`;
  if (extra?.readme_html) mountReadme(item, extra);
}

function mountReadme(item, extra) {
  const mount = $("#readmeMount");
  if (!mount) return;
  if (!window.DOMPurify) {
    mount.textContent = "README 预览组件暂不可用，请在 GitHub 阅读原文。";
    return;
  }
  const branch = encodeURIComponent(extra.default_branch || "main");
  const path = String(extra.readme_path || "README.md").split("/").filter(part => part && part !== "." && part !== "..").map(encodeURIComponent).join("/");
  const base = `https://github.com/${item.full_name}/blob/${branch}/${path}`;
  const fragment = window.DOMPurify.sanitize(extra.readme_html, {
    RETURN_DOM_FRAGMENT: true, SANITIZE_NAMED_PROPS: true, ALLOW_DATA_ATTR: false,
    ALLOWED_TAGS: ["article", "div", "section", "span", "p", "h1", "h2", "h3", "h4", "h5", "h6",
      "a", "img", "pre", "code", "blockquote", "ul", "ol", "li", "table", "thead", "tbody",
      "tfoot", "tr", "th", "td", "hr", "br", "em", "strong", "b", "i", "s", "del", "ins",
      "kbd", "samp", "sup", "sub", "details", "summary", "input"],
    ALLOWED_ATTR: ["href", "src", "alt", "title", "class", "id", "dir", "role", "aria-label",
      "aria-hidden", "align", "width", "height", "colspan", "rowspan", "type", "checked", "disabled"]
  });
  const body = document.createElement("div");
  body.className = "markdown-body";
  body.append(fragment);
  body.querySelectorAll("input").forEach(input => {
    if (input.type !== "checkbox") input.remove();
    else input.disabled = true;
  });
  const resolve = raw => {
    if (!raw) return null;
    try {
      const url = new URL(raw, base);
      return url.protocol === "https:" && !url.username && !url.password ? url : null;
    } catch (_) { return null; }
  };
  body.querySelectorAll("a[href]").forEach(anchor => {
    const raw = anchor.getAttribute("href");
    const url = resolve(raw?.startsWith("#") ? item.source_url + raw : raw);
    if (!url) { anchor.removeAttribute("href"); return; }
    anchor.href = url.href;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
  });
  body.querySelectorAll("img").forEach(image => {
    const raw = image.getAttribute("src");
    const url = resolve(raw);
    image.removeAttribute("srcset");
    if (!url) { image.remove(); return; }
    if (raw && !/^(?:[a-z][a-z\d+.-]*:|\/)/i.test(raw) && url.hostname === "github.com") {
      url.pathname = url.pathname.replace("/blob/", "/raw/");
    }
    const githubImage = url.hostname === "github.com" || url.hostname.endsWith(".githubusercontent.com");
    if (githubImage) {
      image.src = url.href;
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
    } else {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "readme-image-toggle";
      button.textContent = `显示远程图片：${image.alt || url.hostname}`;
      button.addEventListener("click", () => {
        image.src = url.href;
        image.loading = "lazy";
        image.referrerPolicy = "no-referrer";
        button.replaceWith(image);
      });
      image.replaceWith(button);
    }
  });
  mount.replaceChildren(body);
}

async function openDetail(item, opener) {
  if (!item) return;
  detailOpener = opener;
  const call = ++detailRequest;
  renderDetail(item);
  $("#detailDialog").showModal();
  $("#detailClose").focus();
  recordOpen(item);
  if (detailCache.has(item.full_name)) return renderDetail(item, detailCache.get(item.full_name));
  try {
    const detail = await postJSON("/api/repo-detail", { full_name: item.full_name, readme: true });
    detailCache.set(item.full_name, detail);
    if (call === detailRequest && $("#detailDialog").open) renderDetail(item, detail);
  } catch (error) {
    if (call === detailRequest && $("#detailDialog").open) renderDetail(item, null, `详情暂时无法补全：${error.message}`);
  }
}

function applyTheme(theme, persist = false) {
  document.documentElement.dataset.theme = theme;
  $("#themeButton").innerHTML = theme === "dark" ? '☀ <span>浅色</span>' : '☾ <span>深色</span>';
  $("#themeButton").setAttribute("aria-pressed", String(theme === "dark"));
  $("meta[name='theme-color']").content = theme === "dark" ? "#1e1e1e" : "#f6f5f0";
  if (persist) try { localStorage.setItem("gitnote-theme", theme); } catch (_) {}
}

function fillSelect(selector, values, label) {
  const select = $(selector);
  select.innerHTML = `<option value="">${escapeHTML(label)}</option>`;
  values.forEach(value => select.add(new Option(value, value)));
}

function updateFilterOptions() {
  if (!meta) return;
  fillSelect("#topicFilter", meta.github_topics, "全部话题");
  fillSelect("#languageFilter", meta.github_languages, "全部语言");
  fillSelect("#starBandFilter", meta.star_bands, "全部范围");
  $("#topicFilter").value = state.topic;
  $("#languageFilter").value = state.language;
  $("#starBandFilter").value = state.starBand;
  syncFilterControls();
}

function syncFilterControls() {
  const restricted = ["stars", "trending"].includes(state.mode);
  $("#topicFilter").hidden = $("#topicFilter").previousElementSibling.hidden = restricted;
  $("#starBandFilter").hidden = $("#starBandLabel").hidden = restricted;
}

function clearFilters() {
  state.topic = state.language = state.starBand = "";
  $("#search").value = "";
  for (const selector of ["#topicFilter", "#languageFilter", "#starBandFilter"]) $(selector).value = "";
  if (state.mode === "search") return selectMode(state.previousMode);
  if (["stars", "trending"].includes(state.mode)) return loadRemote();
  render();
}

function listingKey(mode) {
  if (mode === "search") return JSON.stringify([state.searchText, state.language, state.topic, state.starBand, state.searchSort]);
  if (mode === "stars") return JSON.stringify([state.language]);
  return JSON.stringify([state.period, state.language]);
}

async function loadRemote(more = false) {
  const mode = state.mode;
  if (!["search", "stars", "trending"].includes(mode)) return render();
  const key = listingKey(mode);
  const previous = state.listings[mode];
  const maxAge = mode === "stars" ? 7200_000 : mode === "trending" ? (previous?.stale ? 300_000 : 1800_000) : 600_000;
  if (!more && previous?.key === key && Date.now() - previous.loadedAt < maxAge) return render();
  const call = ++remoteRequest;
  const page = more ? (previous?.page || 0) + 1 : 1;
  const path = mode === "search" ? "/api/search" : mode === "stars" ? "/api/stars-top" : "/api/trending";
  const body = mode === "search" ? { query: state.searchText, page, language: state.language,
    topic: state.topic, star_band: state.starBand, sort: state.searchSort }
    : mode === "stars" ? { page, language: state.language }
    : { period: state.period, language: state.language };
  const button = $("#loadMore");
  button.disabled = true;
  if (!more) { grid.innerHTML = '<div class="loading-state">正在读取 GitHub…</div>'; $("#emptyState").hidden = true; updateHeading(0); }
  try {
    const result = await postJSON(path, body);
    if (call !== remoteRequest || state.mode !== mode || key !== listingKey(mode)) return;
    state.listings[mode] = { ...result, key, loadedAt: Date.now(), query: mode === "search" ? state.searchText : "",
      items: more ? [...(previous?.items || []), ...result.items] : result.items };
    rememberItems(result.items);
    showRate(result.rate);
    await render();
    if (result.incomplete_results) showToast("GitHub 返回了部分搜索结果。");
    if (result.stale) showToast("Trending 暂时不可用，正在显示上次成功读取的榜单。");
  } catch (error) {
    if (call !== remoteRequest) return;
    if (!more) {
      grid.innerHTML = `<div class="load-error">${escapeHTML(error.message || "GitHub 数据暂时无法读取")}</div>`;
      updateHeading(0);
    }
    showToast(error.message || "加载失败，请稍后重试。");
  } finally { if (call === remoteRequest) button.disabled = false; }
}

function selectMode(mode) {
  ++remoteRequest;
  state.mode = mode;
  if (["stars", "trending"].includes(mode)) {
    state.topic = state.starBand = "";
    $("#topicFilter").value = $("#starBandFilter").value = "";
  }
  syncFilterControls();
  if (["search", "stars", "trending"].includes(mode)) loadRemote();
  else render();
  if (window.innerWidth < 760) $("#feed").scrollIntoView({ behavior: "smooth" });
}

async function discoverMore({ initial = false } = {}) {
  if (state.busy) return;
  firstDiscoveryStarted = true;
  state.busy = true;
  $("#discoverButton").disabled = true;
  $("#discoverButton").textContent = "正在换一批…";
  if (initial && !state.items.length && state.mode === "recommended") {
    grid.innerHTML = '<div class="loading-state">正在为你发现 GitHub 项目…</div>';
    $("#emptyState").hidden = true;
  }
  try {
    await tasteQueue;
    const result = await postJSON("/api/discover", { profile: state.profile, memory: state.memory,
      seen: [...new Set([...state.realPrefs.seen, ...state.items.map(item => item.id)])],
      hidden: state.realPrefs.hidden, topic: state.topic, language: state.language, star_band: state.starBand });
    state.profile = result.profile;
    state.memory = result.memory;
    if (result.items.length) {
      state.realPrefs.seen = [...new Set([...state.realPrefs.seen, ...state.items.map(item => item.id)])].slice(-2000);
      state.items = result.items;
      rememberItems(result.items);
    }
    saveState();
    showRate(result.rate);
    if (!result.items.length) showToast("这次没有找到新仓库，已保留当前卡片。试试更换筛选。");
    else if (result.incomplete) showToast("GitHub 返回了部分搜索结果。");
    if (state.mode === "recommended") await render();
  } catch (error) {
    showToast(error.message || "发现失败，请稍后重试。");
    if (state.mode === "recommended") await render();
  } finally { state.busy = false; $("#discoverButton").disabled = false; $("#discoverButton").textContent = "换一批 ↗"; }
}

async function remoteSearch() {
  const query = $("#search").value.trim();
  if (!query) return state.mode === "search" ? selectMode(state.previousMode) : showToast("先输入项目名称或关键词。");
  if (state.mode !== "search") state.previousMode = state.mode;
  state.searchText = query;
  state.mode = "search";
  updateFilterOptions();
  loadRemote();
}

async function init() {
  applyTheme(document.documentElement.dataset.theme || "light");
  try {
    meta = await (await fetch("/api/meta")).json();
    rememberItems(state.items);
    updateFilterOptions();
    await refreshAuth();
    showRate(await (await fetch("/api/rate")).json());
    if (localStorage.getItem(TASTE_KEY)) {
      const result = await postJSON("/api/taste/decay", { profile: state.profile });
      state.profile = result.profile;
      saveState();
    }
  } catch (_) { showToast("部分本地服务暂时不可用，请刷新页面。"); }
  await render();
  if (!firstDiscoveryStarted && state.mode === "recommended") discoverMore({ initial: true });
}

document.querySelectorAll("[data-mode]").forEach(button => button.addEventListener("click", () => {
  selectMode(button.dataset.mode);
}));
$("#discoverButton").addEventListener("click", () => discoverMore());
$("#remoteSearch").addEventListener("click", remoteSearch);
$("#search").addEventListener("keydown", event => { if (event.key === "Enter") remoteSearch(); });
$("#themeButton").addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true));
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", event => {
  if (!localStorage.getItem("gitnote-theme") && !localStorage.getItem("giterest-theme")) applyTheme(event.matches ? "dark" : "light");
});
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("#search").focus(); }
});
for (const [selector, key] of [["#topicFilter", "topic"], ["#languageFilter", "language"], ["#starBandFilter", "starBand"]]) {
  $(selector).addEventListener("change", event => { state[key] = event.target.value;
    if (["search", "stars", "trending"].includes(state.mode)) loadRemote(); else render(); });
}
$("#periodFilter").addEventListener("change", event => { state.period = event.target.value; loadRemote(); });
$("#searchSort").addEventListener("change", event => { state.searchSort = event.target.value; loadRemote(); });
$("#loadMore").addEventListener("click", () => loadRemote(true));
$("#clearFilters").addEventListener("click", clearFilters);
$("#emptyClear").addEventListener("click", () => {
  if (!state.items.length) return discoverMore();
  selectMode("recommended"); clearFilters();
});
$("#resetPreferences").addEventListener("click", () => {
  state.profile = blankProfile();
  state.realPrefs = { liked: [], saved: [], hidden: [], seen: [], imported: false };
  saveState(); refreshAuth(); render(); showToast("本地偏好已重置。");
});
$("#authButton").addEventListener("click", async () => {
  if (state.auth.connected) {
    await postJSON("/api/auth/disconnect", {});
    await refreshAuth(); showToast("已断开 GitHub 连接。");
  } else if (state.auth.configured) window.location.href = "/auth/start";
  else showToast("请先在 Python 服务环境中设置 GH_CLIENT_ID 和 GH_CLIENT_SECRET，然后重启服务。");
});
$("#importStars").addEventListener("click", async () => {
  if (state.busy) return;
  state.busy = true;
  $("#importStars").disabled = true;
  try {
    await tasteQueue;
    const result = await postJSON("/api/taste/import-stars", { profile: state.profile });
    state.profile = result.profile;
    state.realPrefs.imported = true;
    saveState(); await refreshAuth(); await render();
    showToast(`已从 ${result.imported} 个 Star 仓库建立偏好。`);
  } catch (error) { showToast(error.message || "导入失败。"); }
  finally { state.busy = false; $("#importStars").disabled = false; }
});
$("#aboutButton").addEventListener("click", () => $("#aboutDialog").showModal());
$("#aboutDialog").addEventListener("click", event => { if (event.target === $("#aboutDialog")) $("#aboutDialog").close(); });
$("#detailClose").addEventListener("click", () => $("#detailDialog").close());
$("#detailDialog").addEventListener("click", event => { if (event.target === $("#detailDialog")) $("#detailDialog").close(); });
$("#detailDialog").addEventListener("close", () => { ++detailRequest; detailOpener?.focus(); });
document.addEventListener("visibilitychange", () => { if (document.visibilityState !== "visible") visibleStarts.clear(); });
grid.addEventListener("click", async event => {
  const target = event.target.closest("[data-action]");
  if (!target) {
    const card = event.target.closest(".project-card");
    if (card) openDetail(state.viewItems.find(project => project.id === card.dataset.id), card);
    return;
  }
  const { action, id, value } = target.dataset;
  const item = state.viewItems.find(project => project.id === id);
  const prefs = state.realPrefs;
  if (action === "like" || action === "save") {
    const key = action === "like" ? "liked" : "saved";
    const wasSet = prefs[key].includes(id);
    prefs[key] = wasSet ? prefs[key].filter(entry => entry !== id) : [...prefs[key], id];
    saveState();
    if (action === "like" && !wasSet && item) recordSignal(item, "star");
    render();
  } else if (action === "hide") {
    if (!prefs.hidden.includes(id)) prefs.hidden.push(id);
    saveState();
    if (item) recordSignal(item, "not_interested");
    render(); showToast("已隐藏这个项目。");
  } else if (action === "tag") {
    if (["stars", "trending"].includes(state.mode)) {
      state.previousMode = state.mode;
      $("#search").value = value;
      state.searchText = value;
      state.mode = "search";
      syncFilterControls();
      loadRemote();
      return;
    }
    const select = $("#topicFilter");
    if (select) {
      if (![...select.options].some(option => option.value === value)) select.add(new Option(value, value));
      select.value = value; state.topic = value;
      if (state.mode === "search") loadRemote(); else render();
    }
  } else if (action === "open" && item) recordOpen(item);
  else if (action === "share" && item) {
    try { await navigator.clipboard.writeText(item.source_url); recordSignal(item, "share"); showToast("仓库链接已复制。"); }
    catch (_) { showToast("无法复制链接，请使用打开按钮。"); }
  }
});
grid.addEventListener("keydown", event => {
  if (!["Enter", " "].includes(event.key) || event.target.matches("button,a,select,input")) return;
  const card = event.target.closest(".project-card");
  if (card) { event.preventDefault(); openDetail(state.viewItems.find(project => project.id === card.dataset.id), card); }
});
init();

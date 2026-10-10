'use strict';

const $ = (selector, parent = document) => parent.querySelector(selector);
const state = { me: null, workspaces: [], workspace: null, path: [], mode: 'files', items: [], cursor: null, query: '', users: null, transfers: [], loadGeneration: 0, modalGeneration: 0, detail: null };
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const uuid = () => crypto.randomUUID();
const icons = {
  folder: ['M3 7V5a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7Z'],
  file: ['M14 2H5a1 1 0 0 0-1 1v18a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V8l-6-6Z', 'M14 2v6h6M8 13h8M8 17h5'],
  search: ['M21 21l-5-5', 'M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z'],
  upload: ['M12 16V3m-5 5 5-5 5 5M4 15v5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5'],
  plus: ['M12 5v14M5 12h14'],
  trash: ['M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7'],
  shield: ['M12 3 3 7v6c0 4 9 9 9 9s9-5 9-9V7l-9-4Z', 'm8 12 3 3 5-6'],
  clock: ['M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z', 'M12 7v5l3 2'],
  download: ['M12 3v13m-5-5 5 5 5-5M4 16v5h16v-5'],
  refresh: ['M20 6v5h-5M4 18v-5h5M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1'],
  close: ['m6 6 12 12M6 18 18 6'],
  link: ['m10 13 4-4M8 16l-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0M13 11a4 4 0 0 0 6 0l3-3a4 4 0 0 0-6-6l-3 3'],
  logout: ['M9 4H4v16h5M9 12h12m-5-5 5 5-5 5'],
};
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'class') node.className = value;
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat(Infinity)) if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  Object.entries({ viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.6', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', class: 'icon', 'aria-hidden': 'true' }).forEach(([key, value]) => svg.setAttribute(key, value));
  for (const d of icons[name] || icons.file) { const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', d); svg.append(path); }
  return svg;
}
function button(label, action, options = {}) {
  const { class: classes = '', icon: symbol, ...attrs } = options;
  return el('button', { type: 'button', class: `button ${classes}`, ...attrs, onclick: event => run(action, event.currentTarget) }, symbol && icon(symbol), label);
}
async function run(action, control) {
  if (control?.disabled) return;
  if (control) control.disabled = true;
  try { await action(); }
  catch (error) { report(error); }
  finally { if (control?.isConnected) control.disabled = false; }
}
function bytes(value) {
  if (value === undefined || value === null) return '—';
  if (value < 1024) return `${value} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  let n = value / 1024, i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${units[i]}`;
}
function date(value) { return value ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—'; }
function status(value) {
  const names = { CLEAN: 'Ready', PENDING_SCAN: 'Checking', REJECTED: 'Rejected', TRASHED: 'In trash', READY: 'Ready', PROCESSING: 'Checking', CONFLICT: 'Conflict', EXPIRED: 'Expired' };
  return el('span', { class: `status ${['PENDING_SCAN', 'PROCESSING'].includes(value) ? 'pending' : ['REJECTED', 'CONFLICT', 'EXPIRED'].includes(value) ? 'rejected' : value === 'TRASHED' ? 'neutral' : ''}` }, names[value] || value || '—');
}
function notify(message, error = false, requestId) {
  const node = el('div', { class: `notice ${error ? 'error' : ''}`, role: error ? 'alert' : 'status' }, el('div', { class: 'notice-text' }, message, requestId && el('small', {}, `Request ID: ${requestId}`)), el('button', { type: 'button', 'aria-label': 'Dismiss notification', onclick: () => node.remove() }, '×'));
  $('#notices').append(node);
  if (!error) setTimeout(() => node.remove(), 7000);
}
function report(error) {
  const message = error.message || 'Something went wrong. Please try again.';
  const modal = $('#dialog');
  if (modal.open) {
    let box = $('.dialog-error', modal);
    if (!box) { box = el('div', { class: 'dialog-error', role: 'alert', tabindex: '-1' }); $('.dialog-body', modal).prepend(box); }
    box.replaceChildren(el('div', {}, message));
    if (error.requestId) box.append(el('small', { class: 'mono' }, `Request ID: ${error.requestId}`));
    box.focus();
  } else notify(message, true, error.requestId);
  if (error.status === 401) notify('Your session has ended. Sign in again to continue.', true);
}
async function api(path, options = {}) {
  const { method = 'GET', body, key, retries = 2 } = options;
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') headers['X-CSRF-Token'] = state.me?.csrfToken || '';
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (key) headers['Idempotency-Key'] = key;
  const safeRetry = method === 'GET' || Boolean(key);
  for (let attempt = 0; ; attempt++) {
    let response;
    try { response = await fetch(`/api/v1${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin', cache: 'no-store' }); }
    catch (error) {
      if (safeRetry && attempt < retries) { await sleep(800 * 2 ** attempt); continue; }
      throw new Error('Unable to reach FileFlow. Check your connection and try again.');
    }
    if (response.ok) return response.status === 204 ? null : response.json();
    const problem = await response.json().catch(() => ({}));
    if (safeRetry && attempt < retries && [429, 502, 503, 504].includes(response.status)) {
      await sleep(Math.min(30000, Math.max(800 * 2 ** attempt, Number(response.headers.get('Retry-After') || 0) * 1000))); continue;
    }
    let message = problem.detail || `The request could not be completed (${response.status}).`;
    if (problem.code === 'VERSION_CONFLICT') message = `This file changed while you were working${problem.currentVersionNo ? ` (now version ${problem.currentVersionNo})` : ''}. Refresh its details and choose whether to upload your version again.`;
    if (problem.invalidParams?.length) message += ' ' + problem.invalidParams.map(p => `${p.name}: ${p.reason}`).join('; ');
    const error = new Error(message);
    Object.assign(error, { code: problem.code, requestId: problem.requestId || response.headers.get('X-Request-ID'), status: response.status });
    throw error;
  }
}
function currentFolderId() { return state.path.at(-1)?.id || state.workspace?.rootFolderId; }
function canEdit(file) { return ['OWNER', 'EDITOR'].includes(file?.myRole || state.workspace?.myRole); }
function isOwner(file) { return (file?.myRole || state.workspace?.myRole) === 'OWNER'; }
function isAdmin() { return Boolean(state.me?.isSecurityAdmin || state.me?.roles?.includes('SECURITY_ADMIN')); }
function brand() { return el('div', { class: 'brand' }, el('span', { class: 'brand-mark', 'aria-hidden': 'true' }, 'F'), 'FileFlow'); }
function renderLogin(error) {
  $('#app').replaceChildren(el('main', { class: 'login', id: 'main' }, el('div', {}, el('div', { class: 'login-card' }, brand(), el('div', { class: 'eyebrow' }, 'A place for your team’s work'), el('h1', {}, 'Your files.\nTogether.'), el('p', {}, 'Keep documents organized, share them with colleagues, and find every version in one secure workspace.'), error && el('div', { class: 'dialog-error' }, error), el('a', { class: 'button primary', href: '/auth/login' }, 'Sign in with your company', icon('logout')), el('details', {}, el('summary', {}, 'Using the local development setup?'), el('p', {}, 'Use the local identity provider with an account such as owner@fileflow.local, editor@fileflow.local, viewer@fileflow.local, security@fileflow.local, or finance@fileflow.local. Your generated password is in .local/credentials.txt on this machine.'))), el('p', { class: 'login-foot' }, 'Private workspaces · File history · Internal sharing'))));
}
function navButton(label, symbol, active, action) {
  return el('button', { type: 'button', class: `nav-button ${active ? 'active' : ''}`, 'aria-current': active ? 'page' : null, onclick: () => run(action) }, symbol === 'dot' ? el('span', { class: 'workspace-dot' }) : icon(symbol), el('span', { class: 'nav-text' }, label));
}
function renderShell() {
  const ws = state.workspace;
  const workspaceNav = el('div', {}, el('div', { class: 'nav-label' }, 'Workspaces'), el('div', { class: 'nav-list' }, state.workspaces.map(space => navButton(space.name, 'dot', ws?.id === space.id && ['files', 'search'].includes(state.mode), () => selectWorkspace(space))), !state.workspaces.length && el('p', { class: 'small muted' }, 'No workspace memberships.')));
  const toolsNav = el('div', {}, el('div', { class: 'nav-label' }, 'Manage'), el('div', { class: 'nav-list' }, ws && navButton('Trash', 'trash', state.mode === 'trash', () => changeMode('trash')), (isAdmin() || ws?.myRole === 'OWNER') && navButton('Workspace settings', 'shield', state.mode === 'settings', () => changeMode('settings')), isAdmin() && navButton('Audit log', 'clock', state.mode === 'audit', () => changeMode('audit'))));
  const sidebar = el('aside', { class: 'sidebar' }, brand(), el('nav', { 'aria-label': 'Main navigation' }, workspaceNav, toolsNav), el('div', { class: 'sidebar-footer' }, ws && el('div', {}, el('div', { class: 'quota-label' }, el('span', {}, 'Workspace storage'), el('span', {}, `${Math.round((ws.usedBytes || 0) / ws.quotaBytes * 100)}%`)), el('div', { class: 'quota-track' }, el('progress', { max: ws.quotaBytes, value: ws.usedBytes || 0, 'aria-label': 'Storage used' })), el('div', { class: 'storage-caption' }, `${bytes(ws.usedBytes)} of ${bytes(ws.quotaBytes)} used`)), el('p', { class: 'storage-caption sidebar-caption' }, 'A little order. A lot of possibility.')));
  const searchInput = el('input', { type: 'search', placeholder: 'Search files in this workspace…', 'aria-label': 'Search filenames', minlength: '2', maxlength: '128', value: state.mode === 'search' ? state.query : '', disabled: !ws, name: 'q' });
  const search = el('form', { class: 'search', role: 'search', onsubmit: event => { event.preventDefault(); const query = searchInput.value.trim(); if (query.length >= 2) { state.query = query; run(() => changeMode('search')); } else searchInput.reportValidity(); } }, icon('search'), searchInput, el('button', { class: 'button subtle small', type: 'submit', disabled: !ws }, 'Search'));
  const initials = (state.me.displayName || state.me.email).split(/[ @.]+/).slice(0, 2).map(x => x[0]).join('').toUpperCase();
  const account = el('div', { class: 'account' }, el('div', { class: 'avatar', 'aria-hidden': 'true' }, initials), el('div', { class: 'account-name' }, el('strong', {}, state.me.displayName || state.me.email), el('span', {}, isAdmin() ? 'Security administrator' : 'Company account')), button('Sign out', async () => { await api('/logout', { method: 'POST' }); location.reload(); }, { class: 'subtle small' }));
  $('#app').replaceChildren(el('div', { class: 'shell' }, sidebar, el('div', { class: 'workspace-main' }, el('header', { class: 'topbar' }, search, account), el('main', { id: 'main', class: 'content', tabindex: '-1' }))));
}
async function selectWorkspace(workspace) {
  state.workspace = workspace; state.path = []; state.mode = 'files'; state.query = ''; state.items = [];
  renderShell(); await loadItems();
}
async function changeMode(mode) {
  state.mode = mode; state.items = []; state.cursor = null;
  renderShell();
  if (mode === 'settings') await renderSettings(); else await loadItems();
}
function empty(title, message, symbol = 'folder', extra) {
  return el('div', { class: 'empty' }, icon(symbol), el('h3', {}, title), el('p', {}, message), extra);
}
function renderLoading() { $('#main').replaceChildren(empty('Loading…', 'Getting your workspace ready.')); }
async function loadItems(more = false) {
  if (!state.workspace && state.mode !== 'audit') {
    $('#main').replaceChildren(empty('Welcome to FileFlow', 'Your workspace memberships appear here. A colleague can also send you a personal file link.', 'shield', isAdmin() && button('Open workspace settings', () => changeMode('settings')))); return;
  }
  const generation = ++state.loadGeneration;
  if (!more) renderLoading();
  const params = new URLSearchParams();
  if (more && state.cursor) params.set('cursor', state.cursor);
  let path;
  if (state.mode === 'audit') path = '/audit-events';
  else if (state.mode === 'trash') path = `/workspaces/${state.workspace.id}/trash`;
  else if (state.mode === 'search') { path = '/search'; params.set('workspaceId', state.workspace.id); params.set('q', state.query); }
  else { path = `/workspaces/${state.workspace.id}/items`; if (state.path.length) params.set('parentId', currentFolderId()); }
  try {
    const data = await api(`${path}${params.size ? `?${params}` : ''}`);
    if (generation !== state.loadGeneration) return;
    state.items = more ? [...state.items, ...(data.items || data)] : data.items || data;
    state.cursor = data.nextCursor || null;
    if (state.mode === 'audit') renderAudit(); else renderItems();
  } catch (error) {
    if (generation !== state.loadGeneration) return;
    $('#main').replaceChildren(empty('Could not load this view', error.message, 'refresh', button('Try again', () => loadItems())));
    throw error;
  }
}
function heading(title, subtitle, actions = []) {
  return el('div', { class: 'heading' }, el('div', {}, el('div', { class: 'eyebrow' }, 'Your team, in sync'), el('h1', {}, title), el('p', {}, subtitle)), el('div', { class: 'actions' }, actions));
}
function renderItems() {
  const trash = state.mode === 'trash', search = state.mode === 'search';
  const title = trash ? 'Trash' : search ? 'Search results' : state.workspace.name;
  const subtitle = trash ? 'Recover a file before its retention period ends.' : search ? `Files matching “${state.query}”` : 'A shared home for your team’s files and ideas.';
  const controls = [];
  if (!trash && !search && canEdit()) controls.push(button('New folder', createFolder, { icon: 'plus' }), button('Upload files', () => chooseFiles(), { icon: 'upload', class: 'primary' }));
  const crumbs = el('div', { class: 'breadcrumbs', 'aria-label': 'Folder path' }, el('button', { type: 'button', onclick: () => run(async () => { state.path = []; state.mode = 'files'; await loadItems(); }) }, state.workspace.name));
  if (trash || search) crumbs.append(el('span', {}, '/'), el('span', {}, trash ? 'Trash' : 'Search'));
  else state.path.forEach((folder, i) => crumbs.append(el('span', {}, '/'), el('button', { type: 'button', onclick: () => run(async () => { state.path = state.path.slice(0, i + 1); await loadItems(); }) }, folder.name)));
  const table = el('table', {}, el('thead', {}, el('tr', {}, el('th', { scope: 'col' }, 'Name'), el('th', { scope: 'col' }, trash ? 'Deleted' : 'Status'), el('th', { scope: 'col', class: 'hide-mobile' }, trash ? 'Retained until' : 'Last updated'), el('th', { scope: 'col' }, el('span', { class: 'muted' }, 'Actions')))), el('tbody', {}, state.items.map(item => {
    const folder = item.kind === 'FOLDER';
    const name = el('button', { type: 'button', class: 'file-name', onclick: () => run(folder ? async () => { state.path.push({ id: item.id, name: item.name }); await loadItems(); } : () => openFile(item.id)) }, el('span', { class: `file-icon ${folder ? 'folder-icon' : ''}` }, icon(folder ? 'folder' : 'file')), el('span', {}, item.name));
    const actions = trash ? [button('Restore', () => recover(item), { class: 'small', disabled: !canEdit(item) })] : folder ? [] : [button('Details', () => openFile(item.id), { class: 'small subtle', 'aria-label': `Details for ${item.name}` })];
    return el('tr', {}, el('td', {}, name), el('td', {}, trash ? el('span', { class: 'muted small' }, date(item.deletedAt)) : folder ? el('span', { class: 'muted small' }, 'Folder') : status(item.fileStatus || item.status || (item.downloadableVersionNo ? 'CLEAN' : 'PENDING_SCAN'))), el('td', { class: 'hide-mobile muted small' }, trash ? item.legalHold ? 'Legal hold' : date(item.purgeAt) : date(item.updatedAt)), el('td', {}, el('div', { class: 'actions' }, actions)));
  })));
  const panel = el('section', { class: 'panel', 'aria-label': trash ? 'Deleted files' : 'Files and folders' }, el('div', { class: 'panel-toolbar' }, crumbs, button('', () => loadItems(), { icon: 'refresh', class: 'subtle icon-only', 'aria-label': 'Refresh files', title: 'Refresh' })), state.items.length ? el('div', { class: 'table-wrap' }, table) : empty(trash ? 'Your trash is empty' : search ? 'No matching files' : 'Make room for great work', trash ? 'Deleted files will appear here while they can still be restored.' : search ? 'Try a different filename or a shorter search.' : 'Create a folder or upload your first document to get this workspace started.', trash ? 'trash' : 'folder', !trash && !search && canEdit() && button('Upload your first file', () => chooseFiles(), { class: 'primary', icon: 'upload' })), el('div', { class: 'table-footer' }, `${state.items.length} ${state.items.length === 1 ? 'item' : 'items'}`, state.cursor ? button('Load more', () => loadItems(true), { class: 'small subtle' }) : 'All caught up'));
  const info = !trash && !search && el('div', { class: 'info-strip' }, icon('shield'), el('div', {}, el('strong', {}, 'Your team’s work stays protected.'), ' Every upload is checked before it can be downloaded. Previous versions stay available.'));
  $('#main').replaceChildren(heading(title, subtitle, controls), ...(info ? [info] : []), panel, el('p', { class: 'below-table' }, icon('shield'), 'Only people with permission can access these files.'));
}
function modal(title, children, options = {}) {
  const dialog = $('#dialog');
  state.modalGeneration++;
  state.detail = null;
  dialog.replaceChildren(el('div', { class: 'dialog-header' }, el('h2', { id: 'dialog-title' }, title), button('', () => closeModal(), { icon: 'close', class: 'subtle icon-only', 'aria-label': 'Close dialog' })), el('div', { class: 'dialog-body' }, children));
  if (!dialog.open) dialog.showModal();
  if (options.focus) setTimeout(() => $(options.focus, dialog)?.focus(), 0);
  return dialog;
}
function closeModal() { $('#dialog').close(); state.detail = null; state.modalGeneration++; }
$('#dialog').addEventListener('close', () => { state.detail = null; state.modalGeneration++; });
function field(label, input, hint) { return el('label', { class: 'field' }, label, input, hint && el('small', {}, hint)); }
function formSubmit(form, action) { form.addEventListener('submit', event => { event.preventDefault(); if (form.reportValidity()) run(action, $('button[type="submit"]', form)); }); return form; }
function createFolder() {
  const input = el('input', { name: 'folderName', required: true, maxlength: '255', autocomplete: 'off', placeholder: 'For example, Project documents' });
  const key = uuid();
  const form = el('form', {}, field('Folder name', input), el('div', { class: 'dialog-footer' }, button('Cancel', closeModal), el('button', { type: 'submit', class: 'button primary' }, 'Create folder')));
  formSubmit(form, async () => { await api(`/workspaces/${state.workspace.id}/folders`, { method: 'POST', key, body: { name: input.value.trim(), parentId: state.path.at(-1)?.id || null } }); closeModal(); notify('Folder created.'); await loadItems(); });
  modal('New folder', form, { focus: 'input' });
}
async function openFile(id) {
  modal('File details', el('p', {}, 'Loading file details…'));
  const generation = state.modalGeneration;
  const [file, versions] = await Promise.all([api(`/files/${id}`), api(`/files/${id}/versions`)]);
  if (generation !== state.modalGeneration || !$('#dialog').open) return;
  state.detail = file;
  const actions = [];
  if (!file.deletedAt) {
    actions.push(button('Download', () => downloadFile(file.id), { icon: 'download', class: 'primary', disabled: !file.downloadableVersionNo, title: file.downloadableVersionNo ? `Download version ${file.downloadableVersionNo}` : 'Available after a version passes its checks' }));
    if (canEdit(file)) actions.push(button('Upload new version', () => chooseFiles(file), { icon: 'upload' }));
    if (isOwner(file)) actions.push(button('Manage access', () => manageAccess(file), { icon: 'link' }));
    if (canEdit(file)) actions.push(button('Move to trash', async () => { if (!confirm(`Move “${file.name}” to trash? Its shared links will be revoked.`)) return; await api(`/files/${id}`, { method: 'DELETE' }); closeModal(); notify('File moved to trash.'); await loadItems(); }, { icon: 'trash', class: 'danger' }));
  } else if (canEdit(file)) actions.push(button('Restore from trash', () => recover(file), { icon: 'refresh', class: 'primary' }));
  const latest = versions.find(v => v.versionNo === file.latestVersionNo);
  const content = [el('div', { class: 'detail-summary' }, el('span', {}, `Version ${file.latestVersionNo}`), el('span', {}, bytes(latest?.sizeBytes)), el('span', {}, date(file.updatedAt)), file.legalHold && status('Legal hold')), el('div', { class: 'actions' }, actions)];
  if (file.downloadableVersionNo && file.downloadableVersionNo !== file.latestVersionNo) content.push(el('p', { class: 'callout' }, `The newest version is ${latest?.status === 'REJECTED' ? 'rejected' : 'being checked'}. Downloads currently use the last verified version, ${file.downloadableVersionNo}.`));
  if (latest?.status === 'PENDING_SCAN') content.push(el('p', { class: 'callout' }, 'This version is being checked for integrity and malware. Refresh its details to see the result.'));
  content.push(el('h3', {}, 'Version history'), ...versions.map(version => el('div', { class: 'version-row' }, el('div', {}, el('div', { class: 'version-meta' }, el('strong', {}, `Version ${version.versionNo}`), status(version.status), version.versionNo === file.latestVersionNo && el('span', { class: 'muted small' }, 'Latest')), el('small', {}, `${bytes(version.sizeBytes)} · ${date(version.createdAt)}`), el('details', {}, el('summary', { class: 'muted small' }, 'Checksum'), el('span', { class: 'mono' }, version.sha256))), !file.deletedAt && canEdit(file) && version.status === 'CLEAN' && version.versionNo !== file.latestVersionNo && button('Restore version', async () => { if (!confirm(`Create a new version from version ${version.versionNo}? All previous versions will remain.`)) return; await api(`/files/${id}/versions/${version.versionNo}/restore`, { method: 'POST', key: uuid(), body: { expectedVersionNo: file.latestVersionNo } }); notify('Previous content restored as a new version.'); await openFile(id); await loadItems(); }, { class: 'small' }))));
  content.push(el('div', { class: 'dialog-footer' }, button('Refresh details', () => openFile(id), { icon: 'refresh' }), button('Done', closeModal, { class: 'primary' })));
  modal(file.name, content);
  state.detail = file;
}
function safeLink(value) {
  const url = new URL(value, location.origin);
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('The server returned an invalid link.');
  return url.href;
}
function startDownload(link) {
  const anchor = el('a', { href: safeLink(link.url), target: '_blank', rel: 'noopener noreferrer', referrerpolicy: 'no-referrer' });
  document.body.append(anchor); anchor.click(); anchor.remove();
}
async function downloadFile(id) { startDownload(await api(`/files/${id}/download-link`)); }
async function recover(file) { await api(`/files/${file.id}/recover`, { method: 'POST', key: uuid() }); closeModal(); notify('File restored. Previously revoked links stay revoked.'); await refreshStorage(); await loadItems(); }
async function getUsers() { if (!state.users) { const data = await api('/users'); state.users = data.items || data; } return state.users; }
function userName(id) { const user = state.users?.find(item => item.id === id); return user ? `${user.displayName || user.email}${user.displayName ? ` (${user.email})` : ''}` : id; }
function userSelect(users, name = 'recipient') { return el('select', { name, required: true }, el('option', { value: '' }, 'Choose a colleague…'), users.map(user => el('option', { value: user.id }, `${user.displayName || user.email} · ${user.email}`))); }
async function manageAccess(file) {
  modal('Manage access', el('p', {}, 'Loading access settings…'));
  const generation = state.modalGeneration;
  const [permissions, links, users] = await Promise.all([api(`/files/${file.id}/permissions`), api(`/files/${file.id}/share-links`), getUsers()]);
  if (generation !== state.modalGeneration || !$('#dialog').open) return;
  const recipient = userSelect(users), role = el('select', { name: 'role' }, el('option', { value: 'VIEWER' }, 'Can view'), el('option', { value: 'EDITOR' }, 'Can edit'));
  const permissionForm = el('form', { class: 'form-row' }, field('Colleague', recipient), field('Permission', role), el('button', { class: 'button', type: 'submit' }, 'Save access'));
  formSubmit(permissionForm, async () => { await api(`/files/${file.id}/permissions/${recipient.value}`, { method: 'PUT', body: { role: role.value } }); notify('File access saved.'); await manageAccess(file); });
  const linkRecipient = userSelect(users, 'linkRecipient'), ttl = el('input', { type: 'number', name: 'ttl', required: true, min: '1', max: '168', value: '24' });
  const result = el('div');
  const shareForm = el('form', { class: 'form-row' }, field('Link recipient', linkRecipient), field('Expires in (hours)', ttl), el('button', { class: 'button primary', type: 'submit' }, 'Create link'));
  const linkList = el('div');
  function renderLinks(list) {
    linkList.replaceChildren(...list.map(link => el('div', { class: 'access-row' }, el('div', {}, el('span', { class: 'small' }, userName(link.recipientUserId)), el('small', {}, `${link.revokedAt ? 'Revoked' : new Date(link.expiresAt) < new Date() ? 'Expired' : 'Expires'} · ${date(link.revokedAt || link.expiresAt)}`)), !link.revokedAt && new Date(link.expiresAt) > new Date() && button('Revoke', async () => { await api(`/share-links/${link.id}`, { method: 'DELETE' }); renderLinks(await api(`/files/${file.id}/share-links`)); result.replaceChildren(); notify('Shared link revoked.'); }, { class: 'small danger' }))));
  }
  formSubmit(shareForm, async () => {
    const link = await api(`/files/${file.id}/share-links`, { method: 'POST', key: uuid(), body: { recipientUserId: linkRecipient.value, expiresInHours: Number(ttl.value) } });
    const input = el('input', { readonly: true, value: safeLink(link.url), 'aria-label': 'New share link', onclick: event => event.currentTarget.select() });
    result.replaceChildren(el('div', { class: 'token-result' }, field('Copy this link now — it is shown only once.', input), button('Copy link', async () => { try { await navigator.clipboard.writeText(input.value); notify('Link copied.'); } catch { input.focus(); input.select(); notify('Select and copy the link from the field.'); } }, { class: 'small' })));
    renderLinks(await api(`/files/${file.id}/share-links`));
  });
  renderLinks(links);
  modal('Manage access', [el('p', {}, file.name), el('h3', {}, 'Direct file permissions'), permissionForm, ...permissions.map(permission => el('div', { class: 'access-row' }, el('div', {}, el('span', { class: 'small' }, userName(permission.userId)), el('small', {}, permission.role === 'EDITOR' ? 'Can edit' : 'Can view')), button('Remove', async () => { await api(`/files/${file.id}/permissions/${permission.userId}`, { method: 'DELETE' }); await manageAccess(file); notify('Direct permission removed.'); }, { class: 'small danger' }))), el('p', { class: 'callout' }, 'Workspace membership also grants access. Removing a direct permission does not remove access inherited from the workspace.'), el('h3', {}, 'Internal share links'), el('p', { class: 'muted small' }, 'Only the selected colleague can open a link after signing in. Links last up to 7 days.'), shareForm, result, linkList, el('div', { class: 'dialog-footer' }, button('Back to file', () => openFile(file.id)), button('Done', closeModal, { class: 'primary' }))]);
}
async function renderSettings() {
  const generation = ++state.loadGeneration;
  renderLoading();
  const spaces = isAdmin() ? await api('/admin/workspaces') : state.workspaces.filter(ws => ws.myRole === 'OWNER');
  if (generation !== state.loadGeneration) return;
  const list = spaces.items || spaces;
  const choice = el('select', { name: 'workspace' }, list.map(ws => el('option', { value: ws.id, selected: ws.id === state.workspace?.id }, ws.name)));
  const quota = el('input', { type: 'number', name: 'quota', min: '1', step: '1', required: true, readonly: !isAdmin() });
  const days = el('input', { type: 'number', name: 'retention', min: '1', max: '365', required: true, readonly: !isAdmin() });
  const policyNotice = el('p', { class: 'muted small', role: 'status' });
  let policyGeneration = 0;
  async function loadPolicy() { const current = ++policyGeneration; const policy = await api(`/workspaces/${choice.value}/policy`); if (current !== policyGeneration) return; quota.value = policy.quotaBytes; days.value = policy.retentionDays; policyNotice.textContent = `${bytes(policy.quotaBytes)} storage quota`; }
  choice.addEventListener('change', () => run(loadPolicy));
  const policyForm = el('form', {}, field('Workspace', choice), field('Storage quota (bytes)', quota), policyNotice, field('Trash retention (days)', days, 'From 1 to 365 days. Changes apply to future deletions.'), isAdmin() && el('button', { class: 'button primary', type: 'submit' }, 'Save policy'));
  formSubmit(policyForm, async () => { await api(`/workspaces/${choice.value}/policy`, { method: 'PUT', body: { quotaBytes: Number(quota.value), retentionDays: Number(days.value) } }); policyNotice.textContent = `Saved · ${bytes(Number(quota.value))} storage quota`; notify('Workspace policy saved.'); await refreshStorage(); });
  const cards = [el('section', { class: 'panel admin-card' }, el('h2', {}, 'Workspace policy'), el('p', {}, isAdmin() ? 'Control how much your teams store and how long deleted files remain recoverable.' : 'Your workspace’s storage and retention settings. A security administrator can change these values.'), list.length ? policyForm : el('p', {}, 'No workspaces available.'))];
  if (isAdmin()) {
    const fileId = el('input', { name: 'fileId', required: true, placeholder: 'File UUID', pattern: '[0-9a-fA-F-]{36}' });
    const hold = el('select', { name: 'hold' }, el('option', { value: 'true' }, 'Place legal hold'), el('option', { value: 'false' }, 'Release legal hold'));
    const holdForm = el('form', {}, field('File ID', fileId, 'Find the file ID in its audit event.'), field('Action', hold), el('button', { type: 'submit', class: 'button primary' }, 'Apply legal hold setting'));
    formSubmit(holdForm, async () => { await api(`/files/${fileId.value.trim()}/legal-hold`, { method: 'PATCH', body: { legalHold: hold.value === 'true' } }); notify(hold.value === 'true' ? 'Legal hold placed. Physical deletion is blocked.' : 'Legal hold released. Retention policy still applies.'); });
    cards.push(el('section', { class: 'panel admin-card' }, el('h2', {}, 'Legal hold'), el('p', {}, 'Preserve a file for review. A hold prevents permanent deletion and does not grant access to its contents.'), holdForm));
  }
  $('#main').replaceChildren(heading('Workspace settings', 'Clear policies for your company’s files.'), el('div', { class: 'admin-grid' }, cards));
  if (list.length) await loadPolicy();
}
function renderAudit() {
  const table = el('table', { class: 'audit-table' }, el('thead', {}, el('tr', {}, ['Time', 'Action', 'Actor / target', 'Result', 'Request ID'].map(name => el('th', { scope: 'col' }, name)))), el('tbody', {}, state.items.map(event => el('tr', {}, el('td', {}, date(event.at || event.createdAt)), el('td', {}, event.action), el('td', {}, el('div', { class: 'mono' }, event.actorUserId || 'System'), el('small', { class: 'mono muted' }, event.targetId || '—')), el('td', {}, event.result), el('td', { class: 'mono' }, event.requestId)))));
  $('#main').replaceChildren(heading('Audit log', 'A record of file activity, access changes, and security decisions.', [button('Refresh', () => loadItems(), { icon: 'refresh' })]), el('section', { class: 'panel', 'aria-label': 'Audit events' }, state.items.length ? el('div', { class: 'table-wrap' }, table) : empty('No events yet', 'Activity will appear here as people use FileFlow.', 'clock'), el('div', { class: 'table-footer' }, `${state.items.length} events`, state.cursor && button('Load more', () => loadItems(true), { class: 'small' }))));
}
async function refreshStorage() {
  try { state.workspaces = await api('/workspaces'); if (state.workspace) state.workspace = state.workspaces.find(ws => ws.id === state.workspace.id) || state.workspace; }
  catch { /* The next browse action will surface a persistent connection error. */ }
}

let uploadQueue = Promise.resolve();
function chooseFiles(replacement = null) {
  const folderId = currentFolderId();
  if (!replacement && !folderId) throw new Error('Open a folder before uploading a file.');
  const input = el('input', { type: 'file', multiple: !replacement, hidden: true, 'aria-label': replacement ? 'Select new file version' : 'Select files to upload' });
  document.body.append(input);
  input.addEventListener('cancel', () => input.remove(), { once: true });
  input.addEventListener('change', () => {
    const selected = Array.from(input.files || []);
    input.remove();
    if (!selected.length) return;
    closeModal();
    for (const file of selected) {
      if (!file.size) { notify(`“${file.name}” is empty. Choose a file containing at least one byte.`, true); continue; }
      if (file.size > 2147483648) { notify(`“${file.name}” is too large. The maximum file size is 2 GiB.`, true); continue; }
      const transfer = { id: uuid(), name: file.name, status: 'queued', label: 'Waiting to upload', progress: 0, cancelled: false, xhrs: new Set() };
      state.transfers.push(transfer);
      uploadQueue = uploadQueue.then(() => uploadFile(file, transfer, { folderId, replacement })).catch(error => { transfer.status = 'failed'; transfer.label = error.message; renderTransfers(); });
    }
    renderTransfers();
  }, { once: true });
  input.click();
}
function updateTransfer(transfer, statusName, label, progress) {
  transfer.status = statusName; transfer.label = label;
  if (progress !== undefined) transfer.progress = progress;
  renderTransfers();
}
function renderTransfers() {
  const section = $('#transfers');
  section.hidden = !state.transfers.length;
  section.replaceChildren(el('h3', {}, 'File transfers'), ...state.transfers.map(transfer => {
    const finished = ['done', 'failed', 'cancelled'].includes(transfer.status);
    return el('div', { class: `transfer ${transfer.status === 'failed' ? 'failed' : ''}` }, el('div', { class: 'transfer-heading' }, el('strong', { title: transfer.name }, transfer.name), el('button', { type: 'button', 'aria-label': `${finished ? 'Dismiss' : 'Cancel'} ${transfer.name}`, onclick: () => {
      if (finished) { state.transfers = state.transfers.filter(item => item !== transfer); persistTransfers(); renderTransfers(); }
      else if (transfer.status === 'checking' || transfer.status === 'completing') { notify('File checks continue on the server. You can keep working while they finish.'); }
      else { transfer.cancelled = true; transfer.cancelHash?.(); transfer.xhrs?.forEach(xhr => xhr.abort()); updateTransfer(transfer, 'cancelled', 'Transfer cancelled. Temporary parts will expire automatically.', 0); }
    } }, transfer.status === 'checking' || transfer.status === 'completing' ? '·' : '×')), !finished && el('progress', { max: '100', value: transfer.progress, 'aria-label': `${transfer.name}: ${transfer.label}` }), el('p', { role: finished ? 'status' : null }, transfer.label), transfer.status === 'failed' && transfer.uploadId && button('Check upload status', () => retryTransfer(transfer), { class: 'small subtle' }));
  }));
}
function persistTransfers() {
  const recoverable = state.transfers.filter(transfer => transfer.uploadId && transfer.parts && !['done', 'cancelled'].includes(transfer.status)).map(({ id, uploadId, name, parts, completionKey }) => ({ id, uploadId, name, parts, completionKey }));
  try { sessionStorage.setItem('fileflow.transfers', JSON.stringify(recoverable)); } catch { /* Uploads still complete when browser storage is unavailable. */ }
}
function hashFile(file, transfer) {
  return new Promise((resolve, reject) => {
    const worker = new Worker('/static/hash-worker.js');
    function finish(error, value) { worker.terminate(); delete transfer.cancelHash; error ? reject(error) : resolve(value); }
    transfer.cancelHash = () => finish(new Error('Transfer cancelled.'));
    worker.onerror = () => finish(new Error('Unable to calculate the file checksum. Please try again.'));
    worker.onmessage = ({ data }) => {
      if (data.error) finish(new Error(data.error));
      else if (data.sha256) finish(null, data.sha256);
      else updateTransfer(transfer, 'hashing', `Preparing file · ${Math.round(data.progress * 100)}%`, data.progress * 15);
    };
    worker.postMessage(file);
  });
}
function putPart(signed, blob, transfer, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    transfer.xhrs.add(xhr);
    xhr.open('PUT', safeLink(signed.url));
    xhr.timeout = 300000;
    for (const [key, value] of Object.entries(signed.requiredHeaders || {})) xhr.setRequestHeader(key, value);
    xhr.upload.onprogress = event => { if (event.lengthComputable) onProgress(event.loaded); };
    function finish(error, etag) { transfer.xhrs.delete(xhr); error ? reject(error) : resolve(etag); }
    xhr.onload = () => {
      if (xhr.status < 200 || xhr.status >= 300) return finish(new Error(`Storage could not accept a file part (${xhr.status}).`));
      const etag = xhr.getResponseHeader('ETag');
      if (!etag) return finish(new Error('Storage did not return a part receipt. Please check its CORS configuration.'));
      onProgress(blob.size); finish(null, etag);
    };
    xhr.onerror = () => finish(new Error('Unable to reach file storage. Please check your connection.'));
    xhr.ontimeout = () => finish(new Error('A file part took too long to upload.'));
    xhr.onabort = () => finish(new Error('Transfer cancelled.'));
    if (transfer.cancelled) { transfer.xhrs.delete(xhr); reject(new Error('Transfer cancelled.')); return; }
    xhr.send(blob);
  });
}
async function uploadFile(file, transfer, context) {
  if (transfer.cancelled) return;
  try {
    updateTransfer(transfer, 'hashing', 'Preparing file…', 0);
    const sha256 = await hashFile(file, transfer);
    if (transfer.cancelled) return;
    const body = { sizeBytes: file.size, sha256, contentType: file.type || 'application/octet-stream' };
    const path = context.replacement ? `/files/${context.replacement.id}/uploads` : `/folders/${context.folderId}/uploads`;
    if (context.replacement) body.expectedVersionNo = context.replacement.latestVersionNo; else body.name = file.name;
    const session = await api(path, { method: 'POST', key: transfer.id, body });
    transfer.uploadId = session.uploadId;
    transfer.completionKey = uuid();
    updateTransfer(transfer, 'uploading', 'Uploading to your private storage…', 15);
    const parts = new Array(session.partCount), progress = new Array(session.partCount).fill(0);
    let nextPart = 0, failed = false;
    const worker = async () => {
      while (nextPart < session.partCount && !failed && !transfer.cancelled) {
        const index = nextPart++, partNumber = index + 1;
        const blob = file.slice(index * session.partSizeBytes, Math.min((index + 1) * session.partSizeBytes, file.size));
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            const signed = await api(`/uploads/${session.uploadId}/parts/${partNumber}/url`);
            if (failed || transfer.cancelled) return;
            const etag = await putPart(signed, blob, transfer, loaded => {
              progress[index] = loaded;
              const uploaded = progress.reduce((sum, value) => sum + value, 0);
              updateTransfer(transfer, 'uploading', `${bytes(uploaded)} of ${bytes(file.size)} uploaded`, 15 + uploaded / file.size * 75);
            });
            parts[index] = { partNumber, etag }; break;
          } catch (error) {
            if (attempt === 2 || transfer.cancelled) { failed = true; throw error; }
            progress[index] = 0;
            await sleep(750 * 2 ** attempt);
          }
        }
      }
    };
    const results = await Promise.allSettled(Array.from({ length: Math.min(4, session.partCount) }, worker));
    const failure = results.find(result => result.status === 'rejected');
    if (failure) throw failure.reason;
    if (transfer.cancelled) return;
    transfer.parts = parts;
    persistTransfers();
    updateTransfer(transfer, 'completing', 'Saving your file…', 93);
    const completed = await api(`/uploads/${session.uploadId}/complete`, { method: 'POST', key: transfer.completionKey, body: { parts } });
    updateTransfer(transfer, 'checking', 'Checking file integrity and scanning for malware…', 96);
    await refreshStorage();
    if (['files', 'search'].includes(state.mode)) await loadItems();
    // Verification runs independently so the next selected file can start uploading.
    pollTransfer(transfer, completed).catch(error => { updateTransfer(transfer, 'failed', error.message); persistTransfers(); });
  } catch (error) {
    if (!transfer.cancelled) { updateTransfer(transfer, 'failed', error.message); notify(`${file.name}: ${error.message}`, true, error.requestId); }
    persistTransfers();
  }
}
async function pollTransfer(transfer, initial) {
  let session = initial, errors = 0;
  while (true) {
    if (session?.status === 'READY') {
      updateTransfer(transfer, 'done', 'Ready · integrity verified and scan passed', 100);
      persistTransfers();
      await refreshStorage();
      if (['files', 'search'].includes(state.mode)) await loadItems();
      if (state.detail?.id === session.fileId && $('#dialog').open) await openFile(session.fileId);
      return;
    }
    if (['REJECTED', 'CONFLICT', 'EXPIRED'].includes(session?.status)) {
      const messages = { REJECTED: session.errorCode === 'CHECKSUM_MISMATCH' ? 'Checksum mismatch. The file cannot be downloaded.' : 'This file did not pass its security scan and cannot be downloaded.', CONFLICT: 'Another version was saved first. Refresh the file and upload again if needed.', EXPIRED: 'The upload session expired. Please select the file again.' };
      updateTransfer(transfer, 'failed', messages[session.status]);
      delete transfer.parts;
      persistTransfers();
      if (['files', 'search'].includes(state.mode)) await loadItems();
      return;
    }
    updateTransfer(transfer, 'checking', errors ? 'Waiting for the server. Verification will continue automatically.' : 'Checking file integrity and scanning for malware…', 96);
    await sleep(errors ? Math.min(30000, errors * 5000) : 2500);
    try { session = await api(`/uploads/${transfer.uploadId}`); errors = 0; }
    catch (error) { if ([401, 403, 404].includes(error.status)) throw error; errors++; }
  }
}
async function retryTransfer(transfer) {
  const session = await api(`/uploads/${transfer.uploadId}`);
  if (session.status === 'INITIATED') {
    if (!transfer.parts) throw new Error('The upload was interrupted before all parts arrived. Select the file again to start a new upload.');
    const completed = await api(`/uploads/${transfer.uploadId}/complete`, { method: 'POST', key: transfer.completionKey, body: { parts: transfer.parts } });
    pollTransfer(transfer, completed).catch(error => updateTransfer(transfer, 'failed', error.message));
  } else pollTransfer(transfer, session).catch(error => updateTransfer(transfer, 'failed', error.message));
}
function resumeTransfers() {
  try {
    const saved = JSON.parse(sessionStorage.getItem('fileflow.transfers') || '[]');
    for (const entry of saved) {
      const transfer = { ...entry, status: 'checking', label: 'Reconnecting to your upload…', progress: 96, xhrs: new Set() };
      state.transfers.push(transfer);
      retryTransfer(transfer).catch(error => updateTransfer(transfer, 'failed', error.message));
    }
    renderTransfers();
  } catch { /* Ignore invalid browser-local state. */ }
}
function pendingShare() {
  try {
    const fragment = location.hash.slice(1);
    const params = new URLSearchParams(fragment);
    const token = params.get('share') || (fragment && !fragment.includes('=') && fragment.length >= 20 ? fragment : null);
    if (token) { sessionStorage.setItem('fileflow.pendingShare', token); history.replaceState(null, '', `${location.pathname}${location.search}`); }
    return sessionStorage.getItem('fileflow.pendingShare');
  } catch { return null; }
}
async function resolveShare(token) {
  modal('A file shared with you', el('p', {}, 'Verifying your access…'));
  try {
    const result = await api('/share-links/resolve', { method: 'POST', body: { token } });
    modal(result.fileName, [el('p', {}, 'This file was shared specifically with your company account.'), el('div', { class: 'callout' }, 'The download uses the latest version that has passed its integrity and security checks.'), el('div', { class: 'dialog-footer' }, button('Close', () => { sessionStorage.removeItem('fileflow.pendingShare'); closeModal(); }), button('Download file', async () => { const refreshed = await api('/share-links/resolve', { method: 'POST', body: { token } }); startDownload(refreshed.download); }, { class: 'primary', icon: 'download' }))]);
  } catch (error) {
    modal('Could not open this share link', [el('p', {}, error.message), error.requestId && el('p', { class: 'mono' }, `Request ID: ${error.requestId}`), el('div', { class: 'dialog-footer' }, button('Try again', () => resolveShare(token)), button('Close', () => { sessionStorage.removeItem('fileflow.pendingShare'); closeModal(); }))]);
  }
}
window.addEventListener('hashchange', () => { const token = pendingShare(); if (token && state.me) run(() => resolveShare(token)); });
window.addEventListener('beforeunload', event => { if (state.transfers.some(transfer => ['queued', 'hashing', 'uploading'].includes(transfer.status))) { event.preventDefault(); event.returnValue = ''; } });
async function boot() {
  const token = pendingShare();
  try { state.me = await api('/me', { retries: 0 }); }
  catch (error) { renderLogin(error.status === 401 ? null : error.message); return; }
  try {
    state.workspaces = await api('/workspaces');
    state.workspace = state.workspaces[0] || null;
    renderShell();
    await loadItems();
    resumeTransfers();
    if (token) await resolveShare(token);
  } catch (error) { renderShell(); $('#main').replaceChildren(empty('Could not open your workspace', error.message, 'refresh', button('Try again', boot))); }
}
boot();

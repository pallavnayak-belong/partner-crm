const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const actor = () => { try { return localStorage.getItem('pcrm-actor') || ''; } catch { return ''; } };
const hdrs = (extra = {}) => ({'X-CRM': '1', 'X-Actor': encodeURIComponent(actor()), ...extra});
const api = async (path, opt = {}) => {
  const hasBody = opt.body !== undefined;
  const r = await fetch(path, {method: opt.method || (hasBody ? 'POST' : 'GET'), headers: hdrs(hasBody ? {'Content-Type': 'application/json'} : {}), body: hasBody ? JSON.stringify(opt.body) : undefined});
  const j = await r.json(); if (!r.ok) { staleCheck(j); throw new Error(j.error || r.status); } return j;
};
// any action that fails without its own handler now says why, instead of silently doing nothing
addEventListener('unhandledrejection', e => { toast((e.reason && e.reason.message) || 'Something went wrong'); });
const staleCheck = j => { if (j && j.error === 'missing X-CRM header') { alert('The app was updated. This page will reload now.'); location.reload(); } };
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const toast = m => { const t = $('#toast'); t.textContent = m; t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => t.hidden = true, 2400); };
const STAGES = ['New', 'Contacted', 'Replied', 'Applied', 'In discussion', 'Onboarded', 'Not interested'];
const sdot = s => `<i class="dot st${Math.max(0, STAGES.indexOf(s))}"></i>`;
const iso = d => d.toISOString().slice(0, 10);
const today = () => iso(new Date());
const addDays = n => { const d = new Date(); d.setDate(d.getDate() + n); return iso(d); };
const ago = ts => {
  if (!ts) return '';
  const m = (Date.now() - new Date(ts)) / 60000;
  if (m < 60) return Math.max(1, Math.round(m)) + 'm ago';
  if (m < 1440) return Math.round(m / 60) + 'h ago';
  const d = Math.round(m / 1440); return d < 30 ? d + 'd ago' : new Date(ts).toISOString().slice(0, 10);
};
const fdate = s => s ? new Date(s + (s.length === 10 ? 'T00:00' : '')).toLocaleDateString('en-GB', {day: 'numeric', month: 'short'}) : '';
const view = $('#view');
const prefs = (() => { try { return JSON.parse(localStorage.getItem('pcrm') || '{}'); } catch { return {}; } })();
const savePrefs = () => { try { localStorage.setItem('pcrm', JSON.stringify(prefs)); } catch {} };

/* ---------------- theme ---------------- */
const setTheme = t => {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('pcrm-theme', t); } catch {}
  $('#themelab').textContent = t === 'dark' ? 'Light' : 'Dark';
  $('#themeico').setAttribute('d', t === 'dark' ? 'M12 3v2M12 19v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M3 12h2M19 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4M12 8a4 4 0 100 8 4 4 0 000-8z' : 'M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z');
};
setTheme(document.documentElement.dataset.theme || 'light');
$('#themebtn').onclick = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
$('#addnew').onclick = () => addPartnerDialog();

/* ---------------- router ---------------- */
const routes = {reach, sends, dashboard, pipeline, tasks, discover, partner, templates, settings, funnel, sequence, applications, performance, onboarding, backups};
async function route() {
  const [, name = 'dashboard', arg] = location.hash.replace('#', '').split('/');
  const key = name === 'partner' ? 'pipeline' : name;
  closeModal();
  $$('#nav a').forEach(a => a.classList.toggle('on', a.dataset.r === key));
  try { await (routes[name] || dashboard)(arg); } catch (e) { view.innerHTML = `<p class="due">${esc(e.message)}</p>`; }
  refreshSide();
}
async function refreshSide() {
  const d = await api('/api/dashboard');
  $('#navapp').hidden = !d.apps_pending; $('#navapp').textContent = d.apps_pending;
  { const nr = d.reach ? d.reach.missing + d.reach.invalid : 0; $('#navreach').hidden = !nr; $('#navreach').textContent = nr; }
  const n = d.overdue + d.due_today;
  $('#navdue').hidden = !n; $('#navdue').textContent = n;
  try { const sq = await api('/api/sequence'); const n2 = sq.steps.reduce((x, s) => x + s.due.length, 0); $('#navseq').hidden = !n2; $('#navseq').textContent = n2; } catch {}
  try {
    const tm = (await api('/api/team')).rows, w = $('#whoami'), cur = actor();
    w.innerHTML = `<option value="">Not set</option>${tm.map(m => `<option ${m.name === cur ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}`;
    w.onchange = () => { try { localStorage.setItem('pcrm-actor', w.value); } catch {} toast(w.value ? 'Working as ' + w.value : 'No name set'); };
  } catch {}
  try { const st = await api('/api/backup/status'); $('#navbk').hidden = st.state === 'ok'; const ch = $('#bkchip'); ch.hidden = false; ch.className = st.state === 'ok' ? '' : st.state; ch.title = st.detail; ch.innerHTML = `<i></i><span>${esc(st.label)}</span>`;
    const bn = $('#bkbanner'); const onPage = location.hash.startsWith('#/backups'); bn.hidden = !(st.state === 'bad' || st.state === 'warn') || onPage; bn.innerHTML = `${esc(st.detail)} <a href="#/backups">Open Backups</a>`; } catch {}
  $('#sidecount').textContent = `${d.in_pipeline} partners · ${d.universe.toLocaleString()} in registry`;
}
addEventListener('hashchange', route);
setInterval(() => { if (!document.hidden) refreshSide().catch(() => {}); }, 120000);

/* ---------------- dashboard ---------------- */
async function dashboard() {
  const d = await api('/api/dashboard');
  const st = d.stages, tot = d.in_pipeline;
  const engaged = (st['Replied'] || 0) + (st['Applied'] || 0) + (st['In discussion'] || 0) + (st['Onboarded'] || 0);
  const max = Math.max(...STAGES.map(s => st[s] || 0), 1);
  const colors = ['--s0', '--s1', '--s2', '--s3', '--s4', '--s5', '--s6'];
  const [mc, ac, yc, sq] = await Promise.all([api('/api/mail/config'), api('/api/attribution/config'), api('/api/youform/config'), api('/api/sequence').catch(() => ({steps: []}))]);
  const seqDue = sq.steps.reduce((x, s) => x + s.due.length, 0), todo = d.overdue + d.due_today;
  const setup = [[mc.configured, 'Connect your mailbox to send emails', '#/settings'], [!!ac.onelink_base, "Add the AppsFlyer link base so each partner's installs are counted", '#/settings'], [!!(yc.relay_url && yc.has_relay_token), 'Connect the sign-up form so applications arrive here', '#/settings']];
  const todos = [[d.apps_pending, 'application' + (d.apps_pending === 1 ? '' : 's') + ' to review', '#/applications'], [todo, 'to-do' + (todo === 1 ? '' : 's') + ' due', '#/tasks'], [seqDue, 'follow-up email' + (seqDue === 1 ? '' : 's') + ' ready to send', '#/sequence']].filter(x => x[0]);
  view.innerHTML = `<div class="hero"><div><h2>${todos.length ? 'Here is what needs you today' : "You're all caught up"}</h2><p>${todos.length ? todos.map(([n, l, h]) => `<a href="${h}" style="color:#fff;text-decoration:underline"><b>${n}</b> ${l}</a>`).join(' · ') : 'No applications, to-dos or follow-up emails waiting.'}</p></div><span class="sp"></span><button id="dadd">+ Add partner</button></div>
  ${setup.some(s => !s[0]) ? `<div class="panel setup"><h3>Get set up</h3>${setup.map(([ok, t, h]) => `<div class="item ${ok ? 'done' : ''}"><span class="tick">${ok ? '✓' : ''}</span><span class="t">${t}</span>${ok ? '' : `<a href="${h}">Set up</a>`}</div>`).join('')}</div>` : ''}
  <div class="kpis">
    <div class="kpi"><div class="n">${tot}</div><div class="l">Partners in pipeline</div></div>
    <div class="kpi"><div class="n">${st['Contacted'] || 0}</div><div class="l">Contacted</div></div>
    <div class="kpi"><div class="n">${engaged}</div><div class="l">Replied or further</div></div>
    <div class="kpi ${d.apps_pending ? 'warn' : ''}"><div class="n">${(st['Applied'] || 0) + (st['In discussion'] || 0) + (st['Onboarded'] || 0)}</div><div class="l">Applied or further${d.apps_pending ? ` (${d.apps_pending} to review)` : ''}</div></div>
    <div class="kpi"><div class="n">${st['Onboarded'] || 0}</div><div class="l">Onboarded</div></div>
    <div class="kpi ${d.overdue ? 'warn' : ''}"><div class="n">${d.overdue + d.due_today}</div><div class="l">To-dos due${d.overdue ? ` (${d.overdue} overdue)` : ''}</div></div>
    <div class="kpi"><div class="n">${d.touches_7d}</div><div class="l">Touches, last 7 days</div></div>
  </div>
  <div class="cols"><div>
    <div class="panel funnel"><h3>Pipeline by stage</h3>${STAGES.map((s, i) => `<div class="row"><span class="lab">${sdot(s)}${s}</span>
      <div class="bar"><i style="width:${(st[s] || 0) / max * 100}%;background:var(${colors[i]})"></i></div><span class="num">${st[s] || 0}</span></div>`).join('')}</div>
    <div class="panel"><h3>Recent activity</h3>${d.recent.length ? d.recent.map(a => `<div class="row"><div class="grow"><a href="#/partner/${a.partner_id}">${esc(a.name)}</a>
      <small> · ${esc(a.type)}${a.type === 'stage' ? ' ' + esc(a.body) : ''}</small>${a.type !== 'stage' ? `<div class="mut grow">${esc(a.body)}</div>` : ''}</div><small>${ago(a.ts)}</small></div>`).join('')
      : '<div class="mut">Nothing logged yet. Open a partner and log a call, meeting or note.</div>'}</div>
  </div><div>
    <div class="panel"><h3>Next to-dos</h3>${d.tasks.length ? d.tasks.map(t => `<div class="row"><div class="grow"><a href="#/partner/${t.partner_id}">${esc(t.name)}</a><br><small>${esc(t.title)}</small></div>
      <span class="${t.due <= today() ? 'due' : 'mut'}">${fdate(t.due)}</span></div>`).join('')
      : '<div class="mut">Nothing scheduled.</div>'}</div>
    <div class="panel"><h3>Partners by region</h3>${d.regions.map(x => `<div class="row"><span class="grow"><span class="region ${x.k}">${x.k}</span></span><span>${x.n}</span></div>`).join('')}${d.countries.map(x => `<div class="row"><span class="grow mut">&nbsp;&nbsp;${esc(x.k)}</span><span class="mut">${x.n}</span></div>`).join('')}</div>
    <div class="panel"><h3>Partners by segment</h3>${d.segments.map(x => `<div class="row"><span class="grow">${esc(x.k)}</span><span>${x.n}</span></div>`).join('')}</div>
    <div class="panel"><h3>Partners by type</h3>${d.types.map(x => `<div class="row"><span class="grow">${esc(x.k)}</span><span>${x.n}</span></div>`).join('')}</div>
  </div></div>`;
  $('#dadd').onclick = () => addPartnerDialog();
}

/* ---------------- pipeline (board / list) ---------------- */
let pf = {q: '', segment: '', region: '', country: '', tag: '', owner: '', archived: '', email: '', shown: 100, mode: prefs.mode || 'board'};
const pageSize = {};
async function pipeline() {
  const sig = [pf.q, pf.segment, pf.region, pf.country, pf.tag, pf.owner, pf.archived, pf.email].join('|');
  if (pipeline.sig !== sig) { pipeline.sig = sig; pf.shown = 100; }          // a new search starts at the first page again
  const p = new URLSearchParams({limit: pf.mode === 'board' ? 5000 : pf.shown, ids: 1, q: pf.q, segment: pf.segment, region: pf.region, country: pf.country, tag: pf.tag, owner: pf.owner, archived: pf.archived, email: pf.email || ''});   // the list loads a page at a time; the board needs every card
  const [d, tgs, tm, vw] = await Promise.all([api('/api/partners?' + p), api('/api/tags'), api('/api/team'), api('/api/views?page=pipeline')]);
  view.innerHTML = `<div class="top"><div><h1>Pipeline</h1><div class="sub" id="psub"></div></div><span class="sp"></span>
    <button id="padd" class="primary">+ Add partner</button>
    <input id="pq" placeholder="Search partners…" value="${esc(pf.q)}" style="min-width:220px">
    <select id="pseg"><option value="">All segments</option>${['Corporate', 'Individual', 'Bank'].map(s => `<option ${pf.segment === s ? 'selected' : ''}>${s}</option>`).join('')}</select>
    <select id="preg"><option value="">All regions</option><option ${pf.region === 'Indian' ? 'selected' : ''}>Indian</option><option ${pf.region === 'Foreign' ? 'selected' : ''}>Foreign</option></select>
    ${pf.region === 'Foreign' ? `<select id="pcty"><option value="">All countries</option>${d.countries.filter(x => x.country !== 'India').map(x => `<option value="${esc(x.country)}" ${pf.country === x.country ? 'selected' : ''}>${esc(x.country)} (${x.n})</option>`).join('')}</select>` : ''}
    ${tgs.rows.length ? `<select id="ptag"><option value="">All tags</option>${tgs.rows.map(t => `<option value="${t.id}" ${String(pf.tag) === String(t.id) ? 'selected' : ''}>${esc(t.name)} (${t.n})</option>`).join('')}</select>` : ''}
    <select id="pemail" title="Whether the partner has an email address"><option value="">Any email</option><option value="has" ${pf.email === 'has' ? 'selected' : ''}>Has email</option><option value="missing" ${pf.email === 'missing' ? 'selected' : ''}>Missing email</option></select>
    ${tm.rows.length ? `<select id="pown"><option value="">Any owner</option>${tm.rows.map(m => `<option ${pf.owner === m.name ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}</select>` : ''}
    <select id="pview" title="Saved views"><option value="">Views…</option>${vw.rows.map(v => `<option value="${v.id}">${esc(v.name)}</option>`).join('')}<option value="__save">＋ Save current filters…</option>${vw.rows.length ? '<option value="__del">✕ Delete a view…</option>' : ''}</select>
    ${d.archived ? `<label class="ck"><input type="checkbox" id="parc" ${pf.archived === '1' ? 'checked' : ''}> Archived (${d.archived})</label>` : ''}
    <div class="seg"><button data-m="board" class="${pf.mode === 'board' ? 'on' : ''}">Board</button><button data-m="list" class="${pf.mode === 'list' ? 'on' : ''}">List</button></div></div>
    <div id="pbody"></div>`;
  $('#psub').textContent = `${d.total} partners`;
  $('#pq').oninput = e => { clearTimeout(pipeline.t); pipeline.t = setTimeout(() => { pf.q = e.target.value; pipeline().then(() => { const i = $('#pq'); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }); }, 250); };
  $('#padd').onclick = () => addPartnerDialog();
  $('#pseg').onchange = e => { pf.segment = e.target.value; pipeline(); };
  const pt = $('#ptag'); if (pt) pt.onchange = e => { pf.tag = e.target.value; pipeline(); };
  $('#pemail').onchange = e => { pf.email = e.target.value; pf.shown = 100; pipeline(); };
  const po = $('#pown'); if (po) po.onchange = e => { pf.owner = e.target.value; pipeline(); };
  const pa = $('#parc'); if (pa) pa.onchange = e => { pf.archived = e.target.checked ? '1' : ''; pipeline(); };
  $('#pview').onchange = async e => {
    const v = e.target.value; e.target.value = ''; if (!v) return;
    if (v === '__save') { const name = prompt('Name this view (e.g. "Foreign leads")'); if (!name) return; await api('/api/views', {body: {name, page: 'pipeline', filters: {...pf}}}); toast('View saved'); return pipeline(); }
    if (v === '__del') { const name = prompt('Delete which view? Type its name:\n' + vw.rows.map(x => x.name).join(', ')); const hit = vw.rows.find(x => x.name.toLowerCase() === (name || '').trim().toLowerCase()); if (hit) { await api('/api/views/' + hit.id, {method: 'DELETE'}); toast('View deleted'); pipeline(); } return; }
    const hit = vw.rows.find(x => x.id === +v); if (hit) { pf = {q: '', segment: '', region: '', country: '', tag: '', owner: '', archived: '', email: '', shown: 100, mode: pf.mode, ...hit.filters}; pipeline(); }
  };
  $('#preg').onchange = e => { pf.region = e.target.value; pf.country = ''; pipeline(); };
  const pc = $('#pcty'); if (pc) pc.onchange = e => { pf.country = e.target.value; pipeline(); };
  $$('.seg button').forEach(b => b.onclick = () => { pf.mode = prefs.mode = b.dataset.m; savePrefs(); pipeline(); });
  pf.mode === 'board' ? board(d.rows) : list(d.rows, d.total, d.ids || d.rows.map(r => r.id));
}
function cardHTML(r) {
  const fu = r.follow_up ? `<span class="${r.follow_up <= today() ? 'due' : ''}">⏰ ${fdate(r.follow_up)}</span>` : '';
  const last = r.last_activity ? `<span>${ago(r.last_activity)}</span>` : '';
  return `<div class="card" draggable="true" data-id="${r.id}"><div class="nm" title="${esc(r.name)}">${esc(r.name)}</div>
    <div class="meta">${esc([r.segment, r.city, r.region === 'Foreign' ? r.country : ''].filter(Boolean).join(' · '))}</div>${r.tags ? `<div class="tags" style="margin-top:5px">${r.tags.split('|').map(t => `<span>${esc(t)}</span>`).join('')}</div>` : ''}<div class="ft">${fu}${last}</div></div>`;
}
function board(rows) {
  const by = Object.fromEntries(STAGES.map(s => [s, []]));
  rows.forEach(r => (by[r.stage] || by['New']).push(r));
  STAGES.forEach(s => by[s].sort((a, b) => (a.follow_up || '9') < (b.follow_up || '9') ? -1 : 1));
  $('#pbody').innerHTML = `<div class="board">${STAGES.map(s => `<div class="col" data-s="${s}"><h4>${sdot(s)}${s}<span>${by[s].length}</span></h4><div class="cards"></div></div>`).join('')}</div>`;
  STAGES.forEach(s => {
    const box = $(`.col[data-s="${s}"] .cards`), n = pageSize[s] || 30;
    box.innerHTML = by[s].slice(0, n).map(cardHTML).join('') + (by[s].length > n ? `<div class="more" data-s="${s}">Show ${Math.min(30, by[s].length - n)} more of ${by[s].length - n}</div>` : '');
  });
  $$('.more').forEach(m => m.onclick = () => { pageSize[m.dataset.s] = (pageSize[m.dataset.s] || 30) + 30; board(rows); });
  $$('.card').forEach(c => {
    c.onclick = () => location.hash = '#/partner/' + c.dataset.id;
    c.ondragstart = e => { e.dataTransfer.setData('text/plain', c.dataset.id); c.classList.add('drag'); };
    c.ondragend = () => c.classList.remove('drag');
  });
  $$('.col').forEach(col => {
    col.ondragover = e => { e.preventDefault(); col.classList.add('over'); };
    col.ondragleave = () => col.classList.remove('over');
    col.ondrop = async e => {
      e.preventDefault(); col.classList.remove('over');
      const id = +e.dataTransfer.getData('text/plain'), row = rows.find(r => r.id === id);
      if (!row || row.stage === col.dataset.s) return;
      const move = async extra => { await api('/api/partners/' + id, {method: 'PATCH', body: {stage: col.dataset.s, ...extra}}); toast(`${row.name} → ${col.dataset.s}`); row.stage = col.dataset.s; board(rows); refreshSide(); };
      try { if (col.dataset.s === 'Not interested') lostDialog(row.name, ['No response', 'Not interested', 'Using another platform', 'Not a fit (size / type)', 'Compliance or regulatory', 'Other'], (r, n) => move({lost_reason: r, lost_note: n})); else await move({}); }
      catch (err) { toast(err.message); }
    };
  });
}
const lsel = new Set();
function list(rows, total, allIds) {
  const shownIds = rows.map(r => r.id);
  const more = total - rows.length;
  $('#pbody').innerHTML = `<div class="sel"><span id="lcount"></span><button id="lall2" class="ghost" ${more > 0 ? '' : 'hidden'}>Select all ${total} matching</button><button id="ltag" disabled>Tag selected</button><button id="lemail" class="primary" disabled>Email selected</button></div>
  <table><thead><tr><th><input type="checkbox" id="lall"></th><th>Partner</th><th>Segment</th><th>Country</th><th>City</th><th>Contact</th><th>Email</th><th>Stage</th><th>Follow-up</th><th>Last touch</th></tr></thead><tbody>
    ${rows.map(r => `<tr class="link" data-id="${r.id}"><td><input type="checkbox" class="lc" data-id="${r.id}" ${lsel.has(r.id) ? 'checked' : ''}></td><td><b>${esc(r.name)}</b>${r.tags ? ` <span class="tags">${r.tags.split('|').map(t => `<span>${esc(t)}</span>`).join('')}</span>` : ''}</td><td>${esc(r.segment)}</td><td>${r.region === 'Foreign' ? `<span class="region Foreign">${esc(r.country)}</span>` : esc(r.country || 'India')}</td><td>${esc(r.city)}</td><td>${esc(r.contact_person)}</td><td class="mut">${r.email ? esc(r.email) : '—'}</td>
    <td>${sdot(r.stage)}${esc(r.stage)}</td><td class="${r.follow_up && r.follow_up <= today() ? 'due' : ''}">${fdate(r.follow_up)}</td><td class="mut">${ago(r.last_activity)}</td></tr>`).join('')}</tbody></table>
  ${more > 0 ? `<div style="text-align:center;margin:14px"><span class="mut">Showing ${rows.length} of ${total}. </span><button id="lmore">Show 100 more</button></div>` : ''}`;
  const sel = () => allIds.filter(i => lsel.has(i));
  const sync = () => { const n = sel().length; $('#lcount').textContent = n ? `${n} selected` : `${rows.length}${more > 0 ? ' of ' + total : ''} partners — tick rows to email them`; $('#lemail').disabled = $('#ltag').disabled = !n; $('#lemail').textContent = n ? `Email ${n} selected` : 'Email selected'; };
  $$('tr.link').forEach(tr => tr.onclick = e => { if (e.target.tagName !== 'INPUT') location.hash = '#/partner/' + tr.dataset.id; });
  $$('.lc').forEach(cb => cb.onchange = () => { cb.checked ? lsel.add(+cb.dataset.id) : lsel.delete(+cb.dataset.id); sync(); });
  $('#lall').onchange = e => { shownIds.forEach(i => e.target.checked ? lsel.add(i) : lsel.delete(i)); $$('.lc').forEach(cb => cb.checked = e.target.checked); sync(); };
  $('#lall2').onclick = () => { allIds.forEach(i => lsel.add(i)); $$('.lc').forEach(cb => cb.checked = true); $('#lall').checked = true; sync(); toast(`All ${allIds.length} matching partners selected`); };
  const lm = $('#lmore'); if (lm) lm.onclick = () => { pf.shown += 100; pipeline(); };
  $('#lemail').onclick = () => bulkEmailDialog(sel());
  $('#ltag').onclick = () => tagBulkDialog(sel(), () => pipeline());
  sync();
}

/* ---------------- follow-ups ---------------- */
async function tasks() {
  const d = await api('/api/tasks'), t = d.today, wk = addDays(7);
  const groups = [['Overdue', r => r.due < t], ['Today', r => r.due === t], ['This week', r => r.due > t && r.due <= wk], ['Later', r => r.due > wk]];
  view.innerHTML = `<h1>To-do</h1><div class="sub">Everything you've scheduled across partners. Add tasks from a partner's page.</div>` +
    (d.rows.length ? groups.map(([lab, fn]) => { const rs = d.rows.filter(fn); return rs.length ? `<div class="panel"><h3 class="${lab === 'Overdue' ? 'due' : ''}">${lab} · ${rs.length}</h3>${rs.map(r => `
      <div class="row"><input type="checkbox" class="tdone" data-id="${r.id}" title="Mark done"><span class="mut" style="width:52px">${fdate(r.due)}</span>
      <div class="grow"><b>${esc(r.title)}</b> <small>· <a href="#/partner/${r.partner_id}">${esc(r.name)}</a>${r.assignee ? ' · ' + esc(r.assignee) : ''}</small></div>
      <small>${sdot(r.stage)}${esc(r.stage)}</small><button class="ghost snz" data-id="${r.id}" data-d="1">+1 day</button><button class="ghost snz" data-id="${r.id}" data-d="7">+1 week</button></div>`).join('')}</div>` : ''; }).join('')
      : '<div class="empty">Nothing to do. Open a partner and add a task to schedule your next step.</div>');
  $$('.tdone').forEach(b => b.onchange = async () => { await api('/api/tasks/' + b.dataset.id, {method: 'PATCH', body: {done: true}}); toast('Done'); tasks(); refreshSide(); });
  $$('.snz').forEach(b => b.onclick = async () => { const r = d.rows.find(x => x.id === +b.dataset.id); const dt = new Date(Math.max(Date.now(), new Date(r.due + 'T00:00').getTime())); dt.setDate(dt.getDate() + +b.dataset.d); await api('/api/tasks/' + r.id, {method: 'PATCH', body: {due: iso(dt)}}); tasks(); refreshSide(); });
}

/* ---------------- partner page ---------------- */
async function partner(id) {
  const {partner: p, universe: u, activities, referral: rf, onboarding: onb, region, countries, contacts, tags, tasks: ptasks, team, lost_reasons: lostReasons} = await api('/api/partners/' + id);
  const edits = (await api('/api/changes?partner_id=' + id + '&limit=8').catch(() => ({rows: []}))).rows.filter(g => g.undoable);
  const f = (k, label, type = 'text') => `<span>${label}</span><input type="${type}" data-k="${k}" value="${esc(p[k])}">`;
  const link = (v, t) => v && /^https?:\/\//i.test(v) ? `<a href="${esc(v)}" target="_blank" rel="noopener">${t}</a>` : '<span class="mut">—</span>';
  let ctype = 'note';
  const ph = {note: 'Add a note…', call: 'What was discussed on the call?', meeting: 'Meeting summary…', email: 'Email summary / what was sent…', whatsapp: 'WhatsApp conversation summary…'};
  view.innerHTML = `<a class="crumb" href="#/pipeline">← Pipeline</a>
  <div class="ph"><div><h1>${esc(p.name)}</h1><div class="sub" style="margin:0">${esc([p.category, p.city, p.segment].filter(Boolean).join(' · '))} · <span class="region ${region}">${region === 'Indian' ? 'Indian' : 'Foreign'}${p.country && p.country !== 'India' ? ' · ' + esc(p.country) : ''}</span></div></div><span style="flex:1"></span><select id="pmore"><option value="">More…</option><option value="archive">${p.archived ? 'Restore from archive' : 'Archive'}</option><option value="merge">Merge with a duplicate…</option><option value="export">Export this partner's data</option><option value="delete">Delete permanently…</option></select><button id="sendmail" class="primary">Send email</button></div>
  ${p.do_not_contact ? `<div class="note warn" style="margin-top:10px">Do not contact (${esc(p.dnc_reason || 'manual')}). Emails to this partner are blocked.</div>` : ''}
  ${p.archived ? `<div class="note warn" style="margin-top:10px">Archived${p.archive_reason ? ': ' + esc(p.archive_reason) : ''}. Hidden from the pipeline and never emailed. Use More → Restore to bring them back.</div>` : ''}
  ${p.stage === 'Not interested' && p.lost_reason ? `<div class="note" style="margin-top:10px">Not interested. Reason: <b>${esc(p.lost_reason)}</b></div>` : ''}
  <div class="tagrow" id="tagrow">${tags.map(t => `<span class="tag">${esc(t.name)}<button class="tagx" data-id="${t.id}" title="Remove">×</button></span>`).join('')}<input id="newtag" list="taglist" placeholder="+ tag" style="width:110px"><datalist id="taglist"></datalist></div>
  <div class="stepper">${STAGES.map(s => `<button data-s="${s}" class="${s === p.stage ? 'on' : ''}">${s}</button>`).join('')}</div>
  <div class="pg"><div>
    <h3>Details</h3><div class="fields">
      <span>Owner</span><select data-k="owner"><option value="">Unassigned</option>${[...new Set([...team.map(t => t.name), p.owner].filter(Boolean))].map(n => `<option ${n === p.owner ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select><span>Country</span><input list="cl" data-k="country" value="${esc(p.country || 'India')}"><datalist id="cl">${countries.map(c => `<option value="${esc(c)}">`).join('')}</datalist>${f('contact_person', 'Main contact')}${f('designation', 'Designation')}${f('email', 'Email')}${f('phone', 'Phone')}
      <span>Website</span><div>${link(p.website, esc(p.website))}</div><span>LinkedIn</span><div>${link(p.linkedin, 'Open profile')}</div>
      <span>Last contacted</span><div>${esc(p.last_contacted) || '<span class="mut">—</span>'}</div>
      <span>Do not contact</span><label class="ck"><input type="checkbox" id="dnc" ${p.do_not_contact ? 'checked' : ''}> ${p.do_not_contact ? esc(p.dnc_reason || 'manual') : 'blocks all emails'}</label></div>
    <div class="info"><h3>Tasks</h3>
      ${ptasks.length ? ptasks.map(t => `<div class="row" style="padding:5px 0"><input type="checkbox" class="ptdone" data-id="${t.id}" ${t.done_at ? 'checked' : ''}><div class="grow" style="white-space:normal;${t.done_at ? 'text-decoration:line-through;color:var(--mut)' : ''}">${esc(t.title)}<br><small class="${!t.done_at && t.due <= today() ? 'due' : 'mut'}">${fdate(t.due)}${t.assignee ? ' · ' + esc(t.assignee) : ''}</small></div><button class="ghost ptdel" data-id="${t.id}" title="Delete">✕</button></div>`).join('') : '<div class="mut" style="margin-bottom:8px">No tasks yet.</div>'}
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px"><input id="ttitle" placeholder="e.g. Send agreement draft" style="flex:1;min-width:140px"><input id="tdue" type="date" value="${addDays(1)}" style="width:140px">
        ${team.length ? `<select id="tas"><option value="">Anyone</option>${team.map(m => `<option>${esc(m.name)}</option>`).join('')}</select>` : ''}<button id="tadd" class="primary">Add</button></div>
      <div class="quick" style="margin:6px 0 0"><button data-d="1">Tomorrow</button><button data-d="3">3 days</button><button data-d="7">1 week</button><button data-d="14">2 weeks</button></div></div>
    <div class="info"><h3>People at ${esc(p.name.length > 24 ? p.name.slice(0, 24) + '…' : p.name)}</h3>
      <div class="row" style="padding:5px 0"><div class="grow" style="white-space:normal"><b>${esc(p.contact_person || 'Main contact')}</b> <span class="chip">Main</span><br><small class="mut">${esc([p.designation, p.email, p.phone].filter(Boolean).join(' · ')) || 'Fill in the main contact above'}</small></div></div>
      ${contacts.map(k => `<div class="row" style="padding:5px 0"><div class="grow" style="white-space:normal"><b>${esc(k.name || k.email)}</b>${k.role ? ` <span class="chip">${esc(k.role)}</span>` : ''}${k.bounced ? ' <span class="chip" style="color:var(--warn)">bounced</span>' : ''}<br><small class="mut">${esc([k.designation, k.email, k.phone].filter(Boolean).join(' · '))}</small></div>
        <button class="ghost ckprim" data-id="${k.id}" title="Make this the main contact">Make main</button><button class="ghost ckedit" data-id="${k.id}">Edit</button></div>`).join('')}
      <button id="addcontact" style="margin-top:8px">+ Add a person</button></div>
    <div class="info"><h3>Onboarding</h3>${onb ? `<div class="row" style="border:0;padding:0"><span class="chip ob-${esc(onb.status.replace(' ', ''))}">${esc(onb.status)}</span><small>${onb.done}/${onb.total} documents verified</small><span style="flex:1"></span><a href="#/onboarding/${id}">Open</a></div><div style="margin-top:10px"><span class="chip cm-${esc(onb.commercials)}">Commercials: ${esc(onb.commercials)}</span>${(onb.terms || []).slice(0, 4).map(t => `<div class="mut" style="font-size:12.5px;margin-top:4px">${esc(t)}</div>`).join('')}${(onb.terms || []).length > 4 ? `<div class="mut" style="font-size:12.5px">…</div>` : ''}</div>`
      : `<button id="startonb">Start onboarding</button><div class="mut" style="margin-top:6px">Collect due-diligence documents, the signed agreement and AppsFlyer link.</div>`}</div>
    <div class="info"><h3>Referral &amp; clients</h3><div class="fields"><span>Distributor code</span><div>${rf.distributor_code ? `<b>${esc(rf.distributor_code)}</b>` : '<span class="mut">Assigned when onboarding is completed</span>'}</div><span>Code</span><input id="pcode" value="${esc(rf.code)}" title="Used as the AppsFlyer campaign. Change only if this partner already has a link in use.">
      <span>Link</span>${rf.configured && rf.link ? `<div style="display:flex;gap:6px"><input id="plink" readonly value="${esc(rf.link)}"><button id="pcopy">Copy</button></div>` : (rf.configured ? '<div class="mut" style="white-space:normal">No link yet. Set this partner\'s code above to the name used in their link (the last part of app.getbelong.com/a0PJ/…), or paste their link on the Onboarding page.</div>' : '<div class="mut" style="white-space:normal">Set the OneLink base in <a href="#/settings">Settings</a> to generate this partner\'s link.</div>')}</div>
      <div class="kv mut" style="margin-top:10px;display:grid;grid-template-columns:1fr auto;gap:2px 10px">${[['installed', 'Installed'], ['signed_up', 'Signed up'], ['kyc_done', 'KYC completed'], ['activated', 'Activated'], ['funded', 'Funded wallet'], ['mf_txn', 'MF transaction']].map(([k, l]) => `<span>${l}</span><b style="color:var(--fg)">${rf.stats[k] || 0}</b>`).join('')}</div></div>
    <div class="info"><h3>Registry</h3><div class="fields">${u ? `<span>Registration</span><div>${esc(u.key)}</div>${u.rank ? `<span>MFD rank</span><div>#${u.rank}${u.top_500 ? ' (Top 500)' : ''}</div>` : ''}
      ${u.avg_aum_cr != null ? `<span>Avg AUM</span><div>₹${u.avg_aum_cr.toLocaleString()} Cr</div>` : ''}${u.kind === 'MFD' ? `<span>SIF authorised</span><div>${u.sif_authorized ? 'Yes' : 'No'}</div>` : ''}
      <span>Valid till</span><div>${esc(u.validity)}</div><span>Address</span><div style="white-space:normal">${esc(u.address)}</div>` : `<span>Source</span><div>${esc(p.source)}</div>`}
      ${p.other_social ? `<span>Social</span><div style="white-space:normal;font-size:12px">${esc(p.other_social.replace(/ \| [A-Za-z]+: Not Applicable/g, '').replace(/^[A-Za-z]+: Not Applicable \| ?/, ''))}</div>` : ''}</div></div>
  </div><div>
    <div class="compose"><div class="tabs">${['note', 'call', 'meeting', 'email', 'whatsapp'].map(t => `<button data-t="${t}" class="${t === 'note' ? 'on' : ''}">${t[0].toUpperCase() + t.slice(1)}</button>`).join('')}</div>
      <textarea id="cbody" placeholder="${ph.note}"></textarea><div class="ft"><button class="primary" id="clog">Log</button></div></div>
    ${edits.length ? `<h3>Recent edits <small class="mut">you can undo</small></h3><div id="edits" class="panel" style="margin-bottom:14px">${changesHtml(edits, false)}</div>` : ''}
    <h3>Activity</h3><div class="tl">${activities.map(a => `<div class="ev t-${esc(a.type)}"><div class="h">${esc(a.type)} · ${esc(a.ts.replace('T', ' ').slice(0, 16))}${a.actor ? ' · ' + esc(whoLabel(a.actor)) : ''}${a.edited ? ' · edited' : ''}${editable(a) ? ` <button class="ghost aed" data-id="${a.id}">Edit</button><button class="ghost adl" data-id="${a.id}">Delete</button>` : ''}</div><div class="b" id="ab${a.id}">${esc(a.body)}</div></div>`).join('')}</div>
  </div></div>`;
  $('#sendmail').onclick = () => emailDialog(id);
  const ed = $('#edits'); if (ed) wireUndo(ed, () => partner(id));
  const so = $('#startonb'); if (so) so.onclick = async () => { await api('/api/onboarding/start', {body: {partner_id: id}}); location.hash = '#/onboarding/' + id; };
  $('#pcode').onchange = async e => { try { await api('/api/partners/' + id, {method: 'PATCH', body: {partner_code: e.target.value.trim() === rf.code && !rf.custom_code ? '' : e.target.value}}); toast('Code saved'); partner(id); } catch (err) { toast(err.message); e.target.value = rf.code; } };
  const pc = $('#pcopy'); if (pc) pc.onclick = async () => { try { await navigator.clipboard.writeText($('#plink').value); toast('Link copied'); } catch { $('#plink').select(); toast('Press Cmd+C to copy'); } };
  if (p.do_not_contact) $('#sendmail').disabled = true;
  $('#dnc').onchange = async e => { await api('/api/partners/' + id, {method: 'PATCH', body: {do_not_contact: e.target.checked ? 1 : 0}}); toast(e.target.checked ? 'Marked do-not-contact' : 'Removed'); partner(id); };
  const patch = async (body, msg) => { await api('/api/partners/' + id, {method: 'PATCH', body}); toast(msg || 'Saved'); };
  $$('.stepper button').forEach(b => b.onclick = async () => {
    if (b.dataset.s === p.stage) return;
    try {
      if (b.dataset.s === 'Not interested') return lostDialog(p.name, lostReasons, async (reason, note) => { await patch({stage: 'Not interested', lost_reason: reason, lost_note: note}, 'Stage updated'); partner(id); refreshSide(); });
      await patch({stage: b.dataset.s}, 'Stage updated'); partner(id); refreshSide();
    } catch (e) { toast(e.message); }
  });
  dirtyBar($('.pg .fields'), '[data-k]', async body => { await patch(body, 'Saved'); partner(id); refreshSide(); });
  const tl_ = (await api('/api/tags')).rows; $('#taglist').innerHTML = tl_.map(t => `<option value="${esc(t.name)}">`).join('');
  $('#newtag').onkeydown = async e => { if (e.key === 'Enter' && e.target.value.trim()) { try { await api(`/api/partners/${id}/tags`, {body: {name: e.target.value}}); partner(id); } catch (err) { toast(err.message); } } };
  $$('.tagx').forEach(b => b.onclick = async () => { await api(`/api/partners/${id}/tags/${b.dataset.id}`, {method: 'DELETE'}); partner(id); });
  const addTask = async () => { try { await api(`/api/partners/${id}/tasks`, {body: {title: $('#ttitle').value, due: $('#tdue').value, assignee: $('#tas') ? $('#tas').value : ''}}); toast('Task added'); partner(id); refreshSide(); } catch (e) { toast(e.message); } };
  $('#tadd').onclick = addTask; $('#ttitle').onkeydown = e => { if (e.key === 'Enter') addTask(); };
  $$('.quick button').forEach(b => b.onclick = () => { $('#tdue').value = addDays(+b.dataset.d); });
  $$('.ptdone').forEach(b => b.onchange = async () => { await api('/api/tasks/' + b.dataset.id, {method: 'PATCH', body: {done: b.checked}}); partner(id); refreshSide(); });
  $$('.ptdel').forEach(b => b.onclick = async () => { await api('/api/tasks/' + b.dataset.id, {method: 'DELETE'}); partner(id); refreshSide(); });
  $('#addcontact').onclick = () => contactDialog(id, null, () => partner(id));
  $$('.ckedit').forEach(b => b.onclick = () => contactDialog(id, contacts.find(k => k.id === +b.dataset.id), () => partner(id)));
  $$('.ckprim').forEach(b => b.onclick = async () => { await api(`/api/contacts/${b.dataset.id}/primary`, {body: {}}); toast('Main contact changed'); partner(id); });
  $$('.aed').forEach(b => b.onclick = () => { const a = activities.find(x => x.id === +b.dataset.id); const box = $('#ab' + a.id); box.innerHTML = `<textarea style="width:100%" rows="3">${esc(a.body)}</textarea><div style="margin-top:4px"><button class="primary asv">Save</button> <button class="ghost acn">Cancel</button></div>`; box.querySelector('.acn').onclick = () => partner(id); box.querySelector('.asv').onclick = async () => { try { await api('/api/activities/' + a.id, {method: 'PATCH', body: {body: box.querySelector('textarea').value}}); partner(id); } catch (e) { toast(e.message); } }; });
  $$('.adl').forEach(b => b.onclick = async () => { if (!b.dataset.sure) { b.dataset.sure = 1; b.textContent = 'Sure?'; return; } try { await api('/api/activities/' + b.dataset.id, {method: 'DELETE'}); partner(id); } catch (e) { toast(e.message); } });
  $('#pmore').onchange = async e => {
    const v = e.target.value; e.target.value = ''; if (!v) return;
    try {
      if (v === 'archive') { if (p.archived) { await api(`/api/partners/${id}/archive`, {body: {archived: false}}); toast('Restored'); return partner(id); } const r = prompt('Why archive? (optional)'); if (r === null) return; await api(`/api/partners/${id}/archive`, {body: {archived: true, reason: r}}); toast('Archived'); location.hash = '#/pipeline'; }
      if (v === 'merge') return mergeDialog(id, p.name);
      if (v === 'export') { const d = await api(`/api/partners/${id}/export`); const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], {type: 'application/json'})); a.download = `partner-${id}-data.json`; a.click(); return; }
      if (v === 'delete') return deleteDialog(id, p.name);
    } catch (err) { toast(err.message); }
  };
  $$('.compose .tabs button').forEach(b => b.onclick = () => { ctype = b.dataset.t; $$('.compose .tabs button').forEach(x => x.classList.toggle('on', x === b)); $('#cbody').placeholder = ph[ctype]; $('#cbody').focus(); });
  $('#clog').onclick = async () => {
    const body = $('#cbody').value.trim(); if (!body) return;
    try { await api(`/api/partners/${id}/activities`, {body: {type: ctype, body}}); toast('Logged'); partner(id); refreshSide(); } catch (e) { toast(e.message); }
  };
}

/* Explicit save for a group of fields: shows "unsaved changes" with Save / Discard instead of silently saving on blur. */
function dirtyBar(fieldsEl, selector, save) {
  const els = $$(selector, fieldsEl), orig = new Map(els.map(e => [e, e.value]));
  const bar = document.createElement('div'); bar.className = 'savebar'; bar.hidden = true;
  bar.innerHTML = '<span>You have unsaved changes</span><span style="flex:1"></span><button class="ghost sv-d">Discard</button><button class="primary sv-s">Save changes</button>';
  fieldsEl.insertAdjacentElement('afterend', bar);
  const changed = () => els.filter(e => e.value !== orig.get(e));
  const refresh = () => { bar.hidden = !changed().length; };
  els.forEach(e => { e.addEventListener('input', refresh); e.addEventListener('change', refresh); e.addEventListener('keydown', ev => { if (ev.key === 'Enter' && e.tagName === 'INPUT') { ev.preventDefault(); bar.querySelector('.sv-s').click(); } }); });
  bar.querySelector('.sv-d').onclick = () => { els.forEach(e => { e.value = orig.get(e); }); refresh(); };
  bar.querySelector('.sv-s').onclick = async () => {
    const body = {}; changed().forEach(e => { body[e.dataset.k] = e.value; });
    if (!Object.keys(body).length) return;
    const btn = bar.querySelector('.sv-s'); btn.disabled = true;
    try { await save(body); els.forEach(e => orig.set(e, e.value)); refresh(); }
    catch (err) { toast(err.message); }
    btn.disabled = false;
  };
}
const editable = a => ['note', 'call', 'meeting', 'whatsapp', 'email'].includes(a.type) && !/^(Sent “|Task done:|Contact added:|Contact removed:|Primary contact is now|Country:|Marked do-not-contact|Do-not-contact removed|Archived|Restored from archive)/.test(a.body || '');
function lostDialog(name, reasons, done) {
  const m = openModal(`<h2>Mark ${esc(name)} as not interested</h2><div class="sub" style="margin-bottom:12px">Recording why helps you see patterns later (Funnel page).</div>
    <div class="fields"><span>Reason</span><select id="lr"><option value="">Choose a reason…</option>${reasons.filter(r => r !== 'Unsubscribed').map(r => `<option>${esc(r)}</option>`).join('')}</select><span>Note</span><input id="ln" placeholder="Optional detail"></div>
    <div class="acts"><button id="lc" class="ghost">Cancel</button><button id="lok" class="primary">Mark not interested</button></div>`);
  $('#lc').onclick = closeModal;
  $('#lok').onclick = async () => { if (!$('#lr').value) return toast('Choose a reason'); try { await done($('#lr').value, $('#ln').value.trim()); closeModal(); } catch (e) { toast(e.message); } };
}
function contactDialog(pid, k, done) {
  const m = openModal(`<h2>${k ? 'Edit person' : 'Add a person'}</h2><div class="form2">
    <label>Name<input id="k_name" value="${esc(k?.name)}"></label><label>Role<input id="k_role" value="${esc(k?.role)}" placeholder="e.g. Operations, Compliance, Decision maker"></label>
    <label>Designation<input id="k_designation" value="${esc(k?.designation)}"></label><label>Email<input id="k_email" value="${esc(k?.email)}"></label>
    <label>Phone<input id="k_phone" value="${esc(k?.phone)}"></label></div>
    <div class="acts">${k ? '<button id="k_del" class="ghost" style="margin-right:auto">Remove</button>' : ''}<button id="k_c" class="ghost">Cancel</button><button id="k_ok" class="primary">Save</button></div>`);
  $('#k_c').onclick = closeModal;
  $('#k_ok').onclick = async () => { const body = {}; ['name', 'role', 'designation', 'email', 'phone'].forEach(f => body[f] = $('#k_' + f).value); try { await (k ? api('/api/contacts/' + k.id, {method: 'PATCH', body}) : api(`/api/partners/${pid}/contacts`, {body})); closeModal(); toast('Saved'); done(); } catch (e) { toast(e.message); } };
  if (k) $('#k_del').onclick = async () => { await api('/api/contacts/' + k.id, {method: 'DELETE'}); closeModal(); done(); };
}
function mergeDialog(id, name) {
  const m = openModal(`<h2>Merge “${esc(name)}” into another partner</h2><div class="note warn">This record will disappear. Its history, emails, people, tasks and tags move to the partner you pick, and blank fields on that partner are filled in. A backup copy is taken first.</div>
    <div class="bkrow" id="bkrow"></div><input id="mq" placeholder="Search the partner to keep…" style="width:100%"><div id="mlist" style="margin-top:8px"></div><div class="acts"><button id="mc" class="ghost">Cancel</button></div>`);
  $('#mc').onclick = closeModal; backupNowRow($('#bkrow'));
  const load = async () => { const q = $('#mq').value.trim(); if (q.length < 2) { $('#mlist').innerHTML = '<div class="mut">Type at least 2 letters.</div>'; return; }
    const r = await api('/api/partners?limit=15&q=' + encodeURIComponent(q));
    $('#mlist').innerHTML = r.rows.filter(x => x.id !== id).map(x => `<div class="row"><div class="grow">${esc(x.name)}<br><small>${esc(x.stage)} · ${esc([x.email, x.city].filter(Boolean).join(' · '))}</small></div><button data-k="${x.id}">Keep this one</button></div>`).join('') || '<div class="mut">No matches.</div>';
    $$('#mlist button').forEach(b => b.onclick = async () => { if (!confirm('Merge into this partner? This cannot be undone (a backup copy is taken first).')) return; try { await api(`/api/partners/${id}/merge`, {body: {into: +b.dataset.k}}); closeModal(); toast('Merged'); location.hash = '#/partner/' + b.dataset.k; refreshSide(); } catch (e) { toast(e.message); } }); };
  let t; $('#mq').oninput = () => { clearTimeout(t); t = setTimeout(load, 250); }; $('#mq').focus();
}
function deleteDialog(id, name) {
  const m = openModal(`<h2>Delete ${esc(name)} permanently</h2><div class="note warn">This erases the partner and everything about them: emails sent, notes, people, tasks, onboarding documents and commercials. It can't be undone from the app (a backup copy is taken first and ages out of backups after 30 days). If you only want them out of the way, use Archive instead.</div>
    <div class="fields"><span>Type the name</span><input id="dn" placeholder="${esc(name)}"><span>Reason</span><input id="dr" placeholder="e.g. duplicate entry, data deletion request"></div>
    <div class="acts"><button id="dc" class="ghost">Cancel</button><button id="dok" class="primary" style="background:var(--warn);border-color:var(--warn)">Delete permanently</button></div>`);
  $('#dc').onclick = closeModal;
  $('#dok').onclick = async () => { try { await api('/api/partners/' + id, {method: 'DELETE', body: {confirm: $('#dn').value, reason: $('#dr').value}}); closeModal(); toast('Deleted'); location.hash = '#/pipeline'; refreshSide(); } catch (e) { toast(e.message); } };
}
async function tagBulkDialog(ids, done) {
  const tags = (await api('/api/tags')).rows;
  const m = openModal(`<h2>Tag ${ids.length} partner${ids.length === 1 ? '' : 's'}</h2><input id="bt" list="btl" placeholder="Tag name, e.g. NRI-focused" style="width:100%"><datalist id="btl">${tags.map(t => `<option value="${esc(t.name)}">`).join('')}</datalist>
    <div class="acts"><button id="btc" class="ghost">Cancel</button><button id="btr">Remove tag</button><button id="bta" class="primary">Add tag</button></div>`);
  $('#btc').onclick = closeModal; $('#bt').focus();
  const go = action => async () => { try { await api('/api/tags/bulk', {body: {ids, name: $('#bt').value, action}}); closeModal(); toast(action === 'add' ? 'Tagged' : 'Tag removed'); done(); } catch (e) { toast(e.message); } };
  $('#bta').onclick = go('add'); $('#btr').onclick = go('remove');
}


/* ---------------- discover (universe) ---------------- */
let uOffset = 0, uTotal = 0, selected = new Set(), allMatching = false, facets = null;
const U = {q: '', kind: '', category: '', classification: '', city: '', top500: '', has_email: '', in_pipeline: '0', sort: ''};
async function discover() {
  facets = facets || await api('/api/facets');
  const opt = (arr, cur) => arr.map(v => `<option ${v === cur ? 'selected' : ''}>${esc(v)}</option>`).join('');
  view.innerHTML = `<div class="top"><div><h1>Find partners</h1><div class="sub">Browse the AMFI + SEBI registry and add partners to your pipeline</div></div></div>
  <div class="top" style="margin-top:-6px">
    <input id="uq" placeholder="Search name, ARN/reg no, email, city…" style="min-width:260px" value="${esc(U.q)}">
    <select id="ukind"><option value="">All types</option>${opt(facets.kinds, U.kind)}</select>
    <select id="ucat"><option value="">All categories</option>${opt(facets.categories, U.category)}</select>
    <select id="ucls"><option value="">All segments</option>${opt(facets.classifications, U.classification)}</select>
    <select id="ucity"><option value="">All cities</option>${opt(facets.cities, U.city)}</select>
    <label class="ck"><input type="checkbox" id="utop" ${U.top500 ? 'checked' : ''}> Top 500</label>
    <label class="ck"><input type="checkbox" id="uemail" ${U.has_email ? 'checked' : ''}> Has email</label>
    <label class="ck"><input type="checkbox" id="unot" ${U.in_pipeline === '0' ? 'checked' : ''}> Not in pipeline</label>
    <select id="usort" title="Fit score = Top-500 status, rank, AUM, SIF authorisation, segment and having an email (MFDs); category, segment and email (SEBI)"><option value="">Best fit first</option><option value="rank" ${U.sort === 'rank' ? 'selected' : ''}>MFD rank</option></select></div>
  <div class="sel"><span id="ucount"></span><span id="selinfo"></span><button id="selall" hidden></button><button id="addbtn" class="primary" disabled>Add to pipeline</button></div>
  <table><thead><tr><th><input type="checkbox" id="chkpage"></th><th>Name</th><th>ID</th><th>Segment</th><th>City</th><th>Email</th><th class="r" title="Fit score 0-100">Fit</th><th class="r">Rank</th><th>Pipeline</th></tr></thead><tbody id="ubody"></tbody></table>
  <div class="foot"><button id="more" hidden>Load more</button></div>`;
  let t; $('#uq').oninput = e => { clearTimeout(t); t = setTimeout(() => { U.q = e.target.value; loadU(true); }, 250); };
  [['#ukind', 'kind'], ['#ucat', 'category'], ['#ucls', 'classification'], ['#ucity', 'city']].forEach(([s, k]) => $(s).onchange = e => { U[k] = e.target.value; loadU(true); });
  $('#utop').onchange = e => { U.top500 = e.target.checked ? 1 : ''; loadU(true); };
  $('#uemail').onchange = e => { U.has_email = e.target.checked ? 1 : ''; loadU(true); };
  $('#unot').onchange = e => { U.in_pipeline = e.target.checked ? '0' : ''; loadU(true); };
  $('#usort').onchange = e => { U.sort = e.target.value; loadU(true); };
  $('#selall').onclick = () => { allMatching = true; usync(); };
  $('#chkpage').onchange = e => { $$('.rc:not(:disabled)').forEach(cb => { cb.checked = e.target.checked; e.target.checked ? selected.add(+cb.dataset.id) : selected.delete(+cb.dataset.id); }); allMatching = false; usync(); };
  $('#more').onclick = () => loadU(false);
  $('#addbtn').onclick = async () => {
    const body = allMatching ? {filter: {...U, in_pipeline: 0}} : {ids: [...selected]};
    $('#addbtn').disabled = true;
    const r = await api('/api/pipeline/add', {body});
    toast(`Added ${r.added}${r.skipped ? `, skipped ${r.skipped} already in pipeline` : ''}`); loadU(true); refreshSide();
  };
  loadU(true);
}
async function loadU(reset) {
  if (reset) { uOffset = 0; selected.clear(); allMatching = false; $('#ubody').innerHTML = ''; }
  const d = await api('/api/universe?' + new URLSearchParams({...U, limit: 100, offset: uOffset})); uTotal = d.total;
  $('#ubody').insertAdjacentHTML('beforeend', d.rows.map(r => `<tr><td><input type="checkbox" class="rc" data-id="${r.id}" ${r.partner_id ? 'disabled' : ''} ${selected.has(r.id) ? 'checked' : ''}></td>
    <td title="${esc(r.name)}">${esc(r.name)}</td><td class="mut">${esc(r.key)}</td><td>${esc(r.classification)}</td><td>${esc(r.city)}</td><td class="mut">${esc(r.email)}</td>
    <td class="r">${r.score != null ? Math.round(r.score) : ''}</td><td class="r">${r.rank ?? ''}</td><td>${r.partner_id ? `<a href="#/partner/${r.partner_id}">${sdot(r.stage)}${esc(r.stage)}</a>` : ''}</td></tr>`).join(''));
  uOffset += d.rows.length; $('#more').hidden = uOffset >= uTotal;
  $$('#ubody .rc').forEach(cb => cb.onchange = () => { cb.checked ? selected.add(+cb.dataset.id) : selected.delete(+cb.dataset.id); allMatching = false; usync(); });
  usync();
}
function usync() {
  $('#ucount').textContent = `${uTotal.toLocaleString()} match`;
  const n = allMatching ? uTotal : selected.size;
  $('#selinfo').textContent = n ? `· ${n.toLocaleString()} selected` : '';
  $('#addbtn').disabled = !n; $('#addbtn').textContent = n ? `Add ${n.toLocaleString()} to pipeline` : 'Add to pipeline';
  $('#selall').hidden = !(selected.size && !allMatching && uTotal > selected.size && U.in_pipeline === '0');
  $('#selall').textContent = `Select all ${uTotal.toLocaleString()} matching`;
}

/* ---------------- modal ---------------- */
function openModal(html) { closeModal(); const m = document.createElement('div'); m.className = 'modal'; m.id = 'modal'; m.innerHTML = `<div class="mbox">${html}</div>`; m.onclick = e => { if (e.target === m) closeModal(); }; document.body.appendChild(m); return m; }
function closeModal() { const m = $('#modal'); if (m) m.remove(); }
const cidFix = h => h.replace(/cid:(img\d+)/g, (m, id) => `/assets/${id}.${id === 'img0' ? 'jpg' : 'png'}`);
const sampleMerge = h => h.replace(/\{\{\s*country\s*\}\}/g, 'Singapore').replace(/\{\{\s*first_name\s*\}\}/g, 'Alex').replace(/\{\{\s*name\s*\}\}/g, 'Sample Partner').replace(/\{\{\s*city\s*\}\}/g, 'Mumbai');

/* CC chips: saved contacts, click to toggle; auto ones start selected */
const ccPicker = (contacts, sel, to) => !contacts.length ? `<span class="mut">None saved yet — add some in <a href="#/settings">Settings</a>.</span>`
  : contacts.map(x => `<button type="button" class="cc ${sel.has(x.email) ? 'on' : ''}" data-e="${esc(x.email)}" title="${esc(x.email)}" ${x.email.toLowerCase() === (to || '').toLowerCase() ? 'disabled' : ''}>${esc(x.label)}</button>`).join('');
const wireCc = (root, sel) => $$('.cc', root).forEach(b => b.onclick = () => { const e = b.dataset.e; sel.has(e) ? sel.delete(e) : sel.add(e); b.classList.toggle('on', sel.has(e)); });

/* ---------------- send email (single) ---------------- */
async function emailDialog(partnerId) {
  const [{partner: p, recipients: rcp}, tl, cfg, ccl] = await Promise.all([api('/api/partners/' + partnerId), api('/api/templates'), api('/api/mail/config'), api('/api/mail/cc')]);
  let toSel = (rcp.find(r => r.primary) || rcp[0] || {}).email || '';
  const ccSel = new Set(ccl.rows.filter(x => x.auto).map(x => x.email));
  let tid = ''; // '' = auto by segment
  const m = openModal(`<h2>Email ${esc(p.name)}</h2><div id="ebody"></div>`);
  const draw = async () => {
    let r, pl, fl = []; try { r = await api('/api/mail/preview', {body: {partner_id: partnerId, template_id: tid || null}}); pl = await api('/api/mail/plan', {body: {partner_ids: [partnerId], template_id: tid || null}}); fl = ((await api('/api/mail/plan', {body: {partner_ids: [partnerId], template_id: tid || null, force: true, review: true}})).review.rows[0] || {flags: []}).flags; } catch (e) { $('#ebody').innerHTML = `<div class="note warn">${esc(e.message)}</div><div class="acts"><button id="ecancel">Close</button></div>`; $('#ecancel').onclick = closeModal; return; }
    $('#ebody').innerHTML = `${fl.length ? `<div class="note warn"><b>Heads-up:</b> ${fl.map(esc).join('; ')}.</div>` : ''}${sizeWarn({[r.template_name]: {mb: r.size_mb, attachments: r.attachments.length, attachment_mb: Math.round(r.attachments.reduce((n, a) => n + a.size, 0) / 104857.6) / 10}}, 1, r.est_seconds)}${pl.already_sent ? '<div class="note">This partner already received this template before. It will be sent again.</div>' : ''}${!cfg.configured ? '<div class="note warn">Mail isn\'t set up yet. Add the app password in <a href="#/settings">Settings</a> before sending.</div>' : ''}
      ${!p.email ? '<div class="note warn">This partner has no email address. Add one on the partner page first.</div>' : ''}
      <div class="fields"><span>From</span><div>${esc(cfg.address || '—')}</div><span>To</span>${rcp.length > 1 ? `<select id="eto">${rcp.map(r => `<option value="${esc(r.email)}" ${r.email === toSel ? 'selected' : ''} ${r.bounced ? 'disabled' : ''}>${esc((r.name ? r.name + ' · ' : '') + r.email + (r.primary ? ' (main)' : r.role ? ' (' + r.role + ')' : '') + (r.bounced ? ' · bounced' : ''))}</option>`).join('')}</select>` : `<div>${esc(toSel || '—')}</div>`}<span>Cc</span><div class="ccs">${ccPicker(ccl.rows, ccSel, p.email)}</div>
      <span>Template</span><select id="etpl"><option value="">Auto (by segment) → ${esc(r.template_name)}</option>${tl.rows.map(t => `<option value="${t.id}" ${String(t.id) === String(tid) ? 'selected' : ''}>${esc(t.name)}${t.segment && t.segment !== 'step' ? ' · ' + esc(t.segment) : ''}</option>`).join('')}</select>
      <span>Subject</span><div>${esc(r.subject)}</div>
      ${r.attachments.length ? `<span>Attached</span><div>${r.attachments.map(a => `📎 ${esc(a.name)} <small class="mut">${fsize(a.size)}</small>`).join('<br>')}</div>` : ''}
      <span></span><div><a href="#/templates" id="edtpl">Edit this template</a></div></div>
      <iframe class="prev" id="eprev" sandbox></iframe>
      <div class="acts"><button id="etest">Send test to me</button><button id="ecancel" class="ghost">Cancel</button><button id="esend" class="primary" ${cfg.configured && p.email ? '' : 'disabled'}>Send email</button></div>`;
    $('#eprev').srcdoc = cidFix(r.html);
    wireCc($('#ebody'), ccSel);
    const et = $('#eto'); if (et) et.onchange = () => { toSel = et.value; };
    $('#etpl').onchange = e => { tid = e.target.value; draw(); };
    $('#edtpl').onclick = () => { tplSel = r.template_id; };
    $('#ecancel').onclick = closeModal;
    const lock = on => ['#etest', '#esend', '#ecancel'].forEach(s => { const b = $(s); if (b) b.disabled = on; });
    $('#etest').onclick = async () => { lock(true); await runSend({title: 'Test email', est: r.est_seconds, start: () => api('/api/mail/send-test', {body: {partner_id: partnerId, template_id: tid || null}}), okText: () => `Test email sent to ${cfg.address}. Open that mailbox to check it.`}); lock(false); };
    $('#esend').onclick = async () => {
      lock(true);
      const res = await runSend({title: 'Email to ' + p.name, est: r.est_seconds, start: () => api('/api/mail/send', {body: {partner_ids: [partnerId], template_id: tid || null, cc: [...ccSel], force: true, ...(rcp.length > 1 ? {to: toSel} : {})}}),
        okText: () => `Sent to ${toSel || p.email}${ccSel.size ? ', copied to ' + [...ccSel].join(', ') : ''}.`});
      if (res.ok) { closeModal(); partner(partnerId); refreshSide(); } else { lock(false); }
    };
  };
  draw();
}
/* Every send/test goes through here: a popup that blocks the page while it runs, then states plainly whether it was sent or failed. */
async function runSend({title, est = 60, start, okText}) {
  const el = document.createElement('div'); el.className = 'modal'; el.style.zIndex = 40; document.body.appendChild(el);
  const show = html => { el.innerHTML = `<div class="mbox" style="width:500px;margin-top:12vh">${html}</div>`; };
  const t0 = Date.now(), deadline = Math.round(est * 1.5 + 45);
  const tick = () => { const s = Math.round((Date.now() - t0) / 1000); show(`<h2>${esc(title)}</h2><div class="sending"><span class="spin"></span><div><b>Sending…</b><div class="mut">${s}s elapsed · this gives up after ${deadline}s</div><div class="mut" style="margin-top:6px">Please keep this window open. The result will appear here.</div></div></div>`); };
  tick(); const timer = setInterval(tick, 1000);
  let out;
  try {
    const job = await start();
    for (;;) {
      const j = await api('/api/mail/jobs/' + job.id);
      if (j.state === 'done') { out = {job: j}; break; }
      if ((Date.now() - t0) / 1000 > deadline) { out = {timeout: true}; break; }
      await new Promise(r => setTimeout(r, 1000));
    }
  } catch (e) { out = {error: e.message}; }
  clearInterval(timer);
  let kind = 'fail', head, body, sent = 0;
  if (out.timeout) { head = 'Failed to send: timed out'; body = `There was no confirmation after ${deadline} seconds, so this is treated as <b>not sent</b>. A very large email can still arrive late, so check the partner's activity or the Sent folder before trying again.`; }
  else if (out.error) { head = 'Failed to send'; body = esc(out.error); }
  else {
    const j = out.job; sent = j.sent;
    const why = [...new Set([...j.failed.map(f => f.why), ...j.skipped.map(s => s.why), j.stopped_reason].filter(Boolean))];
    const detail = why.length ? `<ul style="margin:8px 0 0;padding-left:18px">${why.map(w => `<li>${esc(w)}</li>`).join('')}</ul>` : '';
    if (j.total === 1) {
      if (j.sent === 1 && !j.failed.length) { kind = 'ok'; head = 'Sent'; body = esc(okText ? okText(j) : 'The email was sent.'); }
      else { head = 'Failed to send'; body = (why.length ? 'Reason:' : 'The email was not sent.') + detail; }
    } else if (j.sent && !j.failed.length && !j.stopped_reason) { kind = 'ok'; head = `Sent ${j.sent} email${j.sent === 1 ? '' : 's'}`; body = j.skipped.length ? `${j.skipped.length} skipped:${detail}` : 'All of them went out.'; }
    else if (j.sent) { kind = 'warn'; head = `Sent ${j.sent}, but ${j.failed.length + j.skipped.length} did not go`; body = detail; }
    else { head = 'Nothing was sent'; body = detail || 'No email went out.'; }
  }
  show(`<div class="result ${kind}"><div class="ico">${kind === 'ok' ? '✓' : kind === 'warn' ? '!' : '✕'}</div><div><h2 style="margin:0 0 6px">${esc(head)}</h2><div>${body}</div></div></div><div class="acts"><button class="primary" id="srok">OK</button></div>`);
  return new Promise(res => { $('#srok', el).onclick = () => { el.remove(); res({ok: kind === 'ok', sent, kind}); }; $('#srok', el).focus(); });
}

/* ---------------- bulk email from the list ---------------- */
async function bulkEmailDialog(ids, opts = {}) {
  const [tl, cfg, ccl] = await Promise.all([api('/api/templates'), api('/api/mail/config'), api('/api/mail/cc')]);
  const ccSel = new Set(ccl.rows.filter(x => x.auto).map(x => x.email));
  let tid = opts.templateId || '';
  const st = {excluded: new Set(), sample: 0, ack: false, auto: false};
  const step = opts.step || 0; let again = !!step;   // follow-ups you trigger always go; plain bulk sends skip repeats unless you opt in
  openModal(`<h2>${step ? `Send follow-up ${step} to` : 'Email'} ${ids.length} partners</h2><div id="bbody"></div>`);
  const draw = async () => {
    const pl = await api('/api/mail/plan', {body: {partner_ids: ids, template_id: tid || null, step, cc: [...ccSel], force: again, review: true, exclude: [...st.excluded]}});
    if (!st.auto) { st.auto = true; const dups = pl.review.rows.filter(r => r.dup && !st.excluded.has(r.id)); if (dups.length) { dups.forEach(r => st.excluded.add(r.id)); return draw(); } }   // the same address twice: only the first gets it unless you tick it back
    const capHit = pl.cap_remaining !== null && pl.sendable > pl.cap_remaining;
    $('#bbody').innerHTML = `${!pl.configured ? '<div class="note warn">Mail isn\'t set up yet. Add the app password in <a href="#/settings">Settings</a>.</div>' : ''}
      <div class="fields"><span>From</span><div>${esc(pl.from || '—')}</div>
      <span>Cc</span><div class="ccs">${ccPicker(ccl.rows, ccSel, '')}</div><span>Template</span><select id="btpl" ${step ? 'disabled' : ''}><option value="">Auto (by segment)</option>${tl.rows.map(t => `<option value="${t.id}" ${String(t.id) === String(tid) ? 'selected' : ''}>${esc(t.name)}${t.segment && t.segment !== 'step' ? ' · ' + esc(t.segment) : ''}</option>`).join('')}</select></div>
      <div class="note"><b>${pl.sendable}</b> will be sent${Object.entries(pl.templates).map(([k, v]) => { const at = (tl.rows.find(x => x.name === k) || {attachments: []}).attachments; return `<br>· ${v} × ${esc(k)}${at.length ? ` <small class="mut">📎 ${at.map(a => esc(a.name)).join(', ')}</small>` : ''}`; }).join('')}
      ${pl.no_email ? `<br>${pl.no_email} skipped — no valid email` : ''}${pl.already_sent ? `<br>${pl.already_sent} skipped — already received this ${step ? 'follow-up' : 'template'}` : ''}${pl.dnc ? `<br>${pl.dnc} skipped — do not contact` : ''}</div>
      ${!step && (pl.already_sent || again) ? `<label class="ck" style="margin-bottom:10px"><input type="checkbox" id="bagain" ${again ? 'checked' : ''}> Also send to partners who already received this</label>` : ''}
      ${capHit ? `<div class="note warn">Daily cap leaves room for ${pl.cap_remaining} more today. Sending stops there; re-run tomorrow for the rest.</div>` : ''}
      ${sizeWarn(pl.sizes, pl.sendable, pl.est_seconds)}
      ${reviewHtml(pl, st)}
      <div class="note warn">Emails send immediately and can't be recalled. Use "Send test to me" first if you haven't checked this template.</div>
  <div class="bkrow" id="bkrow"></div><div id="bprog"></div>
      <div class="acts"><button id="btest">Send test to me</button><button id="bcancel" class="ghost">Cancel</button>
      <button id="bsend" class="primary" ${pl.configured && pl.sendable && (pl.sendable < 10 || st.ack) ? '' : 'disabled'}>Send ${pl.sendable} email${pl.sendable === 1 ? '' : 's'}</button></div>`;
    wireCc($('#bbody'), ccSel); backupNowRow($('#bkrow')); wireReview(pl, st, draw);
    const ba = $('#bagain'); if (ba) ba.onchange = () => { again = ba.checked; draw(); };
    $('#btpl').onchange = e => { tid = e.target.value; draw(); };
    $('#bcancel').onclick = closeModal;
    const lock = on => ['#btest', '#bsend', '#bcancel'].forEach(s => { const b = $(s); if (b) b.disabled = on; });
    $('#btest').onclick = async () => { lock(true); await runSend({title: 'Test email', est: 60, start: () => api('/api/mail/send-test', {body: {partner_id: ids[0], template_id: tid || null}}), okText: () => `Test email sent to ${cfg.address}. Open that mailbox to check it.`}); lock(false); };
    $('#bsend').onclick = async () => {
      lock(true);
      const res = await runSend({title: `Sending ${pl.sendable} email${pl.sendable === 1 ? '' : 's'}`, est: pl.est_seconds || 60, start: () => api('/api/mail/send', {body: {partner_ids: ids.filter(i => !st.excluded.has(i)), template_id: tid || null, step, cc: [...ccSel], force: again}})});
      if (res.sent) { closeModal(); (opts.onDone || pipeline)(); refreshSide(); } else { lock(false); }
    };
  };
  draw();
}

/* ---------------- templates ---------------- */
let tplSel = null;
const MERGE_TAGS = [['{{first_name}}', 'First name'], ['{{name}}', 'Firm name'], ['{{city}}', 'City'], ['{{country}}', 'Country'], ['{{referral_link}}', "Partner's app link"], ['{{distributor_code}}', 'Distributor code']];
const REGION_LABEL = {'': 'Any country', Indian: 'Indian partners only', Foreign: 'Foreign partners only'};
const SEG_LABEL = {'': 'Everyone (default)', Individual: 'Individuals', Corporate: 'Corporates', Bank: 'Banks', step: 'Follow-ups & manual only'};
let tplTab = 'edit';
const htmlToText = h => {
  const d = new DOMParser().parseFromString(h, 'text/html');
  d.querySelectorAll('style,script,head,img').forEach(n => n.remove());
  d.querySelectorAll('a[href]').forEach(a => { const t = a.textContent.trim(), u = a.getAttribute('href'); if (/^https?:/.test(u) && t !== u) a.textContent = `${t} (${u})`; });
  d.querySelectorAll('br').forEach(b => b.replaceWith('\n'));
  d.querySelectorAll('li').forEach(l => l.prepend('• '));
  d.querySelectorAll('p,div,tr,li,h1,h2,h3,h4,ul,ol,table').forEach(n => n.append('\n'));
  return d.body.textContent.replace(/[ \t ]+/g, ' ').replace(/ *\n */g, '\n').replace(/\n{3,}/g, '\n\n').trim();
};
const uncid = h => h.replace(/\/assets\/(img\d+)\.(?:png|jpg)/g, 'cid:$1');

async function templates() {
  const d = await api('/api/templates');
  if (tplSel === null || (tplSel !== 'new' && !d.rows.find(t => t.id === tplSel))) tplSel = d.rows[0]?.id ?? 'new';
  const t = tplSel === 'new' ? {name: '', subject: '', plain: '', html: '<p>Hi {{first_name}},</p>\n<p>Write your message here.</p>\n<p>Regards,<br>Team Belong</p>', segment: 'step', region: '', attachments: []} : d.rows.find(x => x.id === tplSel);
  const cur = {html: t.html, plain: t.plain, plainTouched: false};
  const fullDoc = /<html/i.test(t.html);
  view.innerHTML = `<div class="top"><div><h1>Email templates</h1><div class="sub">Write once, use everywhere. Pick a template whenever you send an email; attachments you add here go out with every email using that template.</div></div></div>
  <div class="tpl"><div>${d.rows.map(x => `<div class="item ${x.id === tplSel ? 'on' : ''}" data-id="${x.id}"><b>${esc(x.name.replace(/ — DRAFT.*$/, ''))}</b><small>For: ${esc(SEG_LABEL[x.segment] || x.segment)}${x.region ? ' · ' + esc(x.region) + ' only' : ''}</small>
      <div class="badges">${/DRAFT/.test(x.name) ? '<span class="badge draft">Draft: review</span>' : ''}${x.attachments.length ? `<span class="badge att">📎 ${x.attachments.length}</span>` : ''}</div></div>`).join('')}
    ${tplSel === 'new' ? '<div class="item on"><b>New template</b><small>Not saved yet</small></div>' : ''}
    <button id="tnew" class="primary" style="width:100%">+ New template</button></div>
  <div class="editor">
    <div class="form2"><label>Template name<input id="tname" value="${esc(t.name)}" placeholder="e.g. Intro for corporates"></label>
      <label>Use for<select id="tseg">${Object.entries(SEG_LABEL).map(([v, l]) => `<option value="${v}" ${v === t.segment ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
      <label>Partner location<select id="treg">${Object.entries(REGION_LABEL).map(([v, l]) => `<option value="${v}" ${v === (t.region || '') ? 'selected' : ''}>${l}</option>`).join('')}</select></label><span></span>
      <label class="w2">Subject line<input id="tsub" value="${esc(t.subject)}" placeholder="What partners see in their inbox"></label></div>
    <div class="etabs">${[['edit', 'Write'], ['preview', 'Preview'], ['html', 'HTML'], ['plain', 'Plain text']].map(([k, l]) => `<button data-t="${k}" class="${k === tplTab ? 'on' : ''}">${l}</button>`).join('')}</div>
    <div id="tpane"></div>
    <div class="attbox"><h3>Attachments</h3><div id="tatt"></div></div>
    <div style="display:flex;gap:8px;margin-top:18px;flex-wrap:wrap"><button id="tsave" class="primary">Save template</button><button id="ttest" ${t.id ? '' : 'disabled'}>Send test to me</button><button id="tdup" ${t.id ? '' : 'disabled'}>Duplicate</button><span style="flex:1"></span>${t.id ? '<button id="tdel" class="ghost">Delete</button>' : ''}</div>
  </div></div>`;
  const frame = () => $('#tframe');
  const grab = () => {  // pull the latest edits out of whichever pane is showing
    if (tplTab === 'edit' && frame() && frame().contentDocument?.body) {
      const doc = frame().contentDocument;
      cur.html = uncid(fullDoc ? '<!DOCTYPE html>\n' + doc.documentElement.outerHTML : doc.body.innerHTML);
    } else if (tplTab === 'html' && $('#thtml')) cur.html = $('#thtml').value;
    else if (tplTab === 'plain' && $('#tplain')) { cur.plain = $('#tplain').value; }
  };
  const pane = () => {
    const p = $('#tpane');
    if (tplTab === 'edit') {
      p.innerHTML = `<div class="tools"><button data-c="bold" title="Bold"><b>B</b></button><button data-c="italic" title="Italic"><i>I</i></button><button data-c="insertUnorderedList" title="Bullet list">• List</button>
        <button id="tlink">Link</button><input id="turl" placeholder="https://…" style="display:none;width:220px"><button data-c="removeFormat" title="Clear formatting">Clear</button>
        <span class="sp"></span><span class="hint">Insert:</span><span class="mergechips">${MERGE_TAGS.map(([tag, l]) => `<button data-m="${esc(tag)}" title="${esc(l)}">${esc(tag)}</button>`).join('')}</span></div>
        <iframe id="tframe" class="prev" style="height:480px" sandbox="allow-same-origin"></iframe><div class="hint" style="margin-top:6px">Click in the email to edit it directly. Merge fields like {{first_name}} are filled in for each partner when sent.</div>`;
      const f = frame();
      f.onload = () => { f.contentDocument.designMode = 'on'; };
      f.srcdoc = cidFix(cur.html);
      $$('.tools [data-c]', p).forEach(b => b.onmousedown = e => { e.preventDefault(); f.contentDocument.execCommand(b.dataset.c, false, null); });
      $$('.tools [data-m]', p).forEach(b => b.onmousedown = e => { e.preventDefault(); f.contentWindow.focus(); f.contentDocument.execCommand('insertText', false, b.dataset.m); });
      $('#tlink').onmousedown = e => { e.preventDefault(); const u = $('#turl'); u.style.display = 'inline-block'; u.focus(); };
      $('#turl').onkeydown = e => { if (e.key === 'Enter') { f.contentWindow.focus(); f.contentDocument.execCommand('createLink', false, e.target.value.trim()); e.target.value = ''; e.target.style.display = 'none'; } if (e.key === 'Escape') e.target.style.display = 'none'; };
    } else if (tplTab === 'preview') {
      p.innerHTML = `<div class="hint" style="padding:10px 0">Shown with sample details (Alex, Sample Partner, Mumbai). The unsubscribe line is added automatically when sent.</div><iframe id="tprev" class="prev" style="height:520px" sandbox></iframe>`;
      $('#tprev').srcdoc = cidFix(sampleMerge(cur.html));
    } else if (tplTab === 'html') {
      p.innerHTML = `<div class="hint" style="padding:10px 0">For people comfortable with HTML. Images use <code>cid:img1</code>-style references that ship with the email.</div><textarea id="thtml" class="code" style="min-height:420px" spellcheck="false">${esc(cur.html)}</textarea>`;
    } else {
      p.innerHTML = `<div class="hint" style="padding:10px 0">The text-only version, shown by email apps that don't display formatting. <button id="tregen" class="ghost" style="color:var(--brand)">Regenerate from the email</button></div><textarea id="tplain" class="code" style="min-height:360px">${esc(cur.plain)}</textarea>`;
      $('#tplain').oninput = () => { cur.plainTouched = true; };
      $('#tregen').onclick = () => { cur.plain = htmlToText(cur.html); cur.plainTouched = false; $('#tplain').value = cur.plain; };
    }
  };
  const attList = () => {
    const a = $('#tatt');
    if (!t.id) { a.innerHTML = '<div class="hint">Save the template first, then add attachments.</div>'; return; }
    a.innerHTML = `${t.attachments.length ? t.attachments.map(x => `<div class="fl"><span>📎</span><a href="/api/templates/attachments/${x.id}" download>${esc(x.name)}</a><small class="mut">${fsize(x.size)}</small><button class="ghost rma" data-id="${x.id}">Remove</button></div>`).join('') : '<div class="hint">No attachments. Anything you add is sent with every email that uses this template.</div>'}
      <div style="margin-top:8px"><label class="upl">+ Add attachment<input type="file" id="tfile" multiple hidden></label> <span class="hint">PDF, Word, Excel, images · up to 10 MB each, 15 MB per template</span></div>`;
    const reloadAtt = async () => {
      t.attachments = (await api('/api/templates')).rows.find(x => x.id === t.id)?.attachments || [];
      const badges = $('.tpl .item.on .badges'); if (badges) badges.innerHTML = `${/DRAFT/.test(t.name) ? '<span class="badge draft">Draft: review</span>' : ''}${t.attachments.length ? `<span class="badge att">📎 ${t.attachments.length}</span>` : ''}`;
      attList();
    };
    $$('.rma', a).forEach(b => b.onclick = async () => { try { await api('/api/templates/attachments/' + b.dataset.id, {method: 'DELETE'}); toast('Removed'); reloadAtt(); } catch (e) { toast(e.message); } });
    $('#tfile').onchange = async e => {
      const files = [...e.target.files]; let ok = 0;
      for (const f of files) {
        try { const r = await fetch(`/api/templates/${t.id}/attachments?name=${encodeURIComponent(f.name)}`, {method: 'POST', headers: hdrs(), body: f}); const j = await r.json(); if (!r.ok) { staleCheck(j); throw new Error(j.error); } ok++; }
        catch (err) { toast(`${f.name}: ${err.message}`); }
      }
      if (ok) toast(`${ok} attached. Your other edits are untouched; remember to save the template if you changed the text.`);
      reloadAtt();
    };
  };
  pane(); attList();
  $$('.etabs button').forEach(b => b.onclick = () => { grab(); tplTab = b.dataset.t; $$('.etabs button').forEach(x => x.classList.toggle('on', x === b)); pane(); });
  $$('.tpl .item[data-id]').forEach(i => i.onclick = () => { tplSel = +i.dataset.id; templates(); });
  $('#tnew').onclick = () => { tplSel = 'new'; templates(); };
  const save = async () => {
    grab();
    const plain = (cur.plain.trim() && (cur.plainTouched || t.id)) ? cur.plain : htmlToText(cur.html);
    const r = await api('/api/templates', {body: {id: t.id, name: $('#tname').value, segment: $('#tseg').value, region: $('#treg').value, subject: $('#tsub').value, plain, html: cur.html}});
    tplSel = r.id; return r;
  };
  $('#tsave').onclick = async () => { try { await save(); toast('Template saved'); templates(); } catch (e) { toast(e.message); } };
  $('#ttest').onclick = async () => { $('#ttest').disabled = true; try { await save(); } catch (e) { toast(e.message); $('#ttest').disabled = false; return; } const r = await runSend({title: 'Test email', est: 60 + (t.attachments || []).reduce((n, x) => n + x.size, 0) * 1.34 / 100000, start: () => api('/api/mail/send-test', {body: {template_id: t.id}}), okText: () => 'Test email sent to your mailbox. Open it to check how it looks.'}); if (r.ok) templates(); else $('#ttest').disabled = false; };
  $('#tdup').onclick = async () => { try { const r = await api(`/api/templates/${t.id}/duplicate`, {body: {}}); tplSel = r.id; toast('Duplicated. Edit the copy.'); templates(); } catch (e) { toast(e.message); } };
  if (t.id) $('#tdel').onclick = async () => { if ($('#tdel').dataset.sure) { try { await api('/api/templates/' + t.id, {method: 'DELETE', body: {}}); tplSel = null; toast('Deleted'); templates(); } catch (e) { toast(e.message); } } else { $('#tdel').dataset.sure = 1; $('#tdel').textContent = 'Click again to delete'; } };
}

/* ---------------- settings ---------------- */
async function settings() {
  const [c, yc, ac, nc, ccl, tm, tg, bk] = await Promise.all([api('/api/mail/config'), api('/api/youform/config'), api('/api/attribution/config'), api('/api/notify/config'), api('/api/mail/cc'), api('/api/team'), api('/api/tags'), api('/api/backup')]);
  view.innerHTML = `<h1>Settings</h1><div class="sub">Email sending and reply tracking</div>
  <div class="panel" style="max-width:640px"><h3>Mailbox</h3>
    <div class="note">Use a Google <b>app password</b> for the mailbox you send from (Google Account → Security → 2-Step Verification → App passwords). It is stored only in <code>~/partner-crm/data/mail.json</code> on this Mac. If your Workspace admin has disabled app passwords, tell me and we'll switch to OAuth.</div>
    <div class="fields"><span>Email</span><input id="sa" value="${esc(c.address)}" placeholder="e.g. partnerships@getbelong.com (type it in)"><span>App password</span><input id="sp" type="password" placeholder="${c.has_password ? '•••• saved — leave blank to keep' : 'xxxx xxxx xxxx xxxx'}" autocomplete="off">
    <span>From name</span><input id="sn" value="${esc(c.from_name)}"><span>Daily cap</span><input id="sc" type="number" min="0" value="${c.daily_cap}"><span>Delay (sec)</span><input id="sd" type="number" min="0" value="${c.delay}"></div>
    <div style="display:flex;gap:8px;margin-top:12px"><button id="ssave" class="primary">Save</button><button id="stest">Test connection</button></div><div id="sres" class="mut" style="margin-top:10px"></div></div>
  <div class="panel" style="max-width:640px"><h3>Backups</h3><div class="mut" style="margin-bottom:10px">Local, Google Drive and Google Sheet backups, with a health dashboard and restore steps, are on their own page.</div><a href="#/backups"><button class="primary">Open Backups</button></a></div>
  <div class="panel" style="max-width:640px"><h3>Team</h3>
    <div class="note">Add the people who use this CRM. They appear as owners, task assignees and in the “Working as” menu, which stamps who logged each note or change.</div>
    ${tm.rows.map(m => `<div class="row"><div class="grow">${esc(m.name)}</div><button class="ghost tmdel" data-id="${m.id}">Remove</button></div>`).join('') || '<div class="mut">No one yet.</div>'}
    <div style="display:flex;gap:8px;margin-top:12px"><input id="tmn" placeholder="Name"><button id="tmadd" class="primary">Add</button></div></div>
  <div class="panel" style="max-width:640px"><h3>Tags</h3>
    ${tg.rows.map(t => `<div class="row"><div class="grow">${esc(t.name)} <small>· ${t.n} partner${t.n === 1 ? '' : 's'}</small></div><button class="ghost tgdel" data-id="${t.id}">Delete</button></div>`).join('') || '<div class="mut">No tags yet. Add them from a partner page or tag several partners from the Pipeline list view.</div>'}</div>
  <div class="panel" style="max-width:640px"><h3>CC contacts</h3>
    <div class="note">Colleagues you can copy on outgoing emails. In any send dialog they appear as buttons: click to add or remove. “Always” pre-selects a contact every time. Their replies are ignored by reply tracking, so a reply-all never counts as a partner reply.</div>
    ${ccl.rows.map(x => `<div class="row"><div class="grow"><b>${esc(x.label)}</b> <small>${esc(x.email)}</small></div><label class="ck"><input type="checkbox" class="ccauto" data-id="${x.id}" ${x.auto ? 'checked' : ''}> always</label><button class="ghost ccdel" data-id="${x.id}">Remove</button></div>`).join('') || '<div class="mut">No CC contacts yet.</div>'}
    <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap"><input id="ccl" placeholder="Name, e.g. Ayush (Ops)" style="width:190px"><input id="cce" placeholder="email@getbelong.com" style="width:230px"><label class="ck"><input type="checkbox" id="cca"> always</label><button id="ccadd" class="primary">Add</button></div></div>
  <div class="panel" style="max-width:640px"><h3>Youform applications</h3>
    <div class="note">Applications arrive through a small Google Apps Script relay (file: <code>~/partner-crm/youform_relay.gs</code>) that catches Youform's webhook and holds it until the CRM collects it. Paste the relay's web-app URL and token below. You can also import a Youform CSV export on the Applications page.</div>
    <div class="fields"><span>Relay URL</span><input id="yu" value="${esc(yc.relay_url)}" placeholder="https://script.google.com/macros/s/…/exec"><span>Relay token</span><input id="yt" type="password" autocomplete="off" placeholder="${yc.has_relay_token ? '•••• saved — leave blank to keep' : 'the TOKEN you set in the script'}"></div>
    <div style="display:flex;gap:8px;margin-top:12px"><button id="ysave" class="primary">Save</button><button id="ypull" ${yc.relay_url && yc.has_relay_token ? '' : 'disabled'}>Pull now</button></div><div id="yres" class="mut" style="margin-top:10px"></div></div>
  <div class="panel" style="max-width:640px"><h3>Partner attribution</h3>
    <div class="note">Each partner gets a link with their own code as the campaign. The CRM reads install and activation events from ClickHouse (read-only) and counts the clients each partner brought. Paste the link base here, without <code>?</code> parameters. For <code>app.getbelong.com/…</code> bases the link is <code>base/Code</code> (e.g. …/a0PJ/Mehul) and only partners with a code set get one. For other OneLink bases the CRM appends <code>?pid=</code> (media source) and <code>&amp;c=</code> (code). Media source is optional: leave it empty to count installs from any source.</div>
    <div class="fields"><span>OneLink base</span><input id="ab" value="${esc(ac.onelink_base)}" placeholder="https://belong.onelink.me/AbCd/partner"><span>Media source (optional)</span><input id="am" value="${esc(ac.media_source)}"></div>
    <div style="display:flex;gap:8px;margin-top:12px"><button id="asave" class="primary">Save</button><button id="async">Sync now</button></div><div id="ares2" class="mut" style="margin-top:10px">${ac.last_sync ? `Last sync ${esc(ac.last_sync.replace('T', ' '))}: ${esc(ac.last_sync_result)}` : 'Not synced yet.'}</div></div>
  <div class="panel" style="max-width:640px"><h3>Slack alerts</h3>
    <div class="note">Get a Slack message when a partner replies, opts out, or applies. Create an incoming webhook for a channel (Slack → Apps → Incoming Webhooks) and paste its URL. Messages contain the firm name and a link to the CRM only.</div>
    <div class="fields"><span>Webhook URL</span><input id="nw" type="password" autocomplete="off" placeholder="${nc.configured ? '•••• saved — paste a new one to replace, or clear to turn off' : 'https://hooks.slack.com/services/…'}"></div>
    <div style="display:flex;gap:8px;margin-top:12px"><button id="nsave" class="primary">Save</button><button id="ntest" ${nc.configured ? '' : 'disabled'}>Send test</button><button id="noff" class="ghost" ${nc.configured ? '' : 'disabled'}>Turn off</button></div></div>
  <div class="panel" style="max-width:640px"><h3>Replies</h3><div class="mut" style="margin-bottom:10px">Checked automatically every 10 minutes while the server runs. A real reply moves a partner to Replied; bounces and auto-replies are logged without changing stage. Sent today: ${c.sent_today}${c.daily_cap ? ` of ${c.daily_cap}` : ''}.</div>
    <button id="schk" ${c.configured ? '' : 'disabled'}>Check replies now</button><span id="schkres" class="mut" style="margin-left:10px"></span></div>`;
  $('#ssave').onclick = async () => { await api('/api/mail/config', {body: {address: $('#sa').value, password: $('#sp').value, from_name: $('#sn').value, daily_cap: $('#sc').value, delay: $('#sd').value}}); toast('Saved'); settings(); };
  $('#stest').onclick = async () => { $('#sres').textContent = 'Saving and testing…'; await api('/api/mail/config', {body: {address: $('#sa').value, password: $('#sp').value, from_name: $('#sn').value, daily_cap: $('#sc').value, delay: $('#sd').value}}); const r = await api('/api/mail/test-connection', {body: {}}); $('#sres').innerHTML = `Send (SMTP): ${esc(r.smtp)}<br>Inbox (IMAP): ${esc(r.imap)}`; };
  $('#tmadd').onclick = async () => { try { await api('/api/team', {body: {name: $('#tmn').value}}); settings(); } catch (e) { toast(e.message); } };
  $$('.tmdel').forEach(b => b.onclick = async () => { await api('/api/team/' + b.dataset.id, {method: 'DELETE'}); settings(); });
  $$('.tgdel').forEach(b => b.onclick = async () => { if (confirm('Delete this tag from every partner?')) { await api('/api/tags/' + b.dataset.id, {method: 'DELETE'}); settings(); } });
  $('#ccadd').onclick = async () => { try { await api('/api/mail/cc', {body: {label: $('#ccl').value, email: $('#cce').value, auto: $('#cca').checked}}); toast('Added'); settings(); } catch (e) { toast(e.message); } };
  $$('.ccdel').forEach(b => b.onclick = async () => { await api('/api/mail/cc/' + b.dataset.id, {method: 'DELETE', body: {}}); settings(); });
  $$('.ccauto').forEach(b => b.onchange = async () => { await api('/api/mail/cc/' + b.dataset.id, {method: 'PATCH', body: {auto: b.checked}}); toast('Saved'); });
  $('#ysave').onclick = async () => { await api('/api/youform/config', {body: {relay_url: $('#yu').value, relay_token: $('#yt').value}}); toast('Saved'); settings(); };
  $('#ypull').onclick = async () => { $('#yres').textContent = 'Pulling…'; try { const r = await api('/api/youform/pull', {body: {}}); $('#yres').textContent = `${r.pulled} pulled: ${r.linked} linked, ${r.new_partner} new partners, ${r.review} to review, ${r.duplicate} duplicates`; refreshSide(); } catch (e) { $('#yres').textContent = e.message; } };
  $('#asave').onclick = async () => { try { await api('/api/attribution/config', {body: {onelink_base: $('#ab').value, media_source: $('#am').value}}); toast('Saved'); settings(); } catch (e) { toast(e.message); } };
  $('#async').onclick = async () => { $('#ares2').textContent = 'Syncing from ClickHouse… (up to a minute)'; try { await api('/api/attribution/sync', {body: {}}); for (;;) { await new Promise(r => setTimeout(r, 1500)); const j = await api('/api/attribution/sync'); if (j.state === 'done') { $('#ares2').textContent = j.result.summary; break; } if (j.state === 'error') { $('#ares2').textContent = j.error; break; } } } catch (e) { $('#ares2').textContent = e.message; } };
  $('#nsave').onclick = async () => { if (!$('#nw').value.trim()) return toast('Paste a webhook URL first'); try { await api('/api/notify/config', {body: {webhook_url: $('#nw').value}}); toast('Saved'); settings(); } catch (e) { toast(e.message); } };
  $('#ntest').onclick = async () => { try { await api('/api/notify/test', {body: {}}); toast('Test sent — check Slack'); } catch (e) { toast(e.message); } };
  $('#noff').onclick = async () => { await api('/api/notify/config', {body: {webhook_url: ''}}); toast('Slack alerts off'); settings(); };
  $('#schk').onclick = async () => { $('#schkres').textContent = 'Checking…'; try { const r = await api('/api/mail/check-replies', {body: {}}); $('#schkres').textContent = `${r.replies} new replies, ${r.unsubscribed || 0} opt-outs, ${r.auto} auto-replies, ${r.bounces} bounces`; refreshSide(); } catch (e) { $('#schkres').textContent = e.message; } };
}

/* ---------------- funnel analytics ---------------- */
async function funnel() {
  const d = await api('/api/analytics');
  const pct = (n, t) => t ? Math.round(n / t * 1000) / 10 + '%' : '—';
  const colors = ['--s0', '--s1', '--s2', '--s3', '--s4', '--s5'];
  const max = Math.max(...d.reached.map(r => r.n), 1);
  const tmax = Math.max(...d.trend.map(t => Math.max(t.sent, t.replies)), 1);
  const att = d.attention;
  const list = (rows, key, fmt) => rows.length ? rows.map(r => `<div class="row"><a class="grow" href="#/partner/${r.id}">${esc(r.name)}</a><small class="${key === 'follow_up' ? 'due' : ''}">${fmt(r)}</small></div>`).join('') : '<div class="mut">None</div>';
  view.innerHTML = `<h1>Funnel</h1><div class="sub">From outreach to onboarding — what's working and what's stuck</div>
  <div class="kpis">
    <div class="kpi"><div class="n">${d.emailed}</div><div class="l">Partners emailed</div></div>
    <div class="kpi"><div class="n">${pct(d.replied, d.emailed)}</div><div class="l">Reply rate (${d.replied} replied)</div></div>
    <div class="kpi ${d.bounced ? 'warn' : ''}"><div class="n">${d.bounced}</div><div class="l">Bounced${d.emailed ? ' · ' + pct(d.bounced, d.emailed) : ''}</div></div>
    <div class="kpi"><div class="n">${d.applications}</div><div class="l">Applications${d.applications_pending ? ` · ${d.applications_pending} to review` : ''}</div></div>
    <div class="kpi"><div class="n">${d.unsubscribed}</div><div class="l">Unsubscribed</div></div>
    <div class="kpi"><div class="n">${d.dnc}</div><div class="l">Do-not-contact total</div></div>
  </div>
  <div class="cols"><div>
    <div class="panel funnel"><h3>Pipeline funnel (reached this stage or further)</h3>${d.reached.map((r, i) => `<div class="row"><span class="lab">${sdot(r.stage)}${r.stage}</span>
      <div class="bar"><i style="width:${r.n / max * 100}%;background:var(${colors[i]})"></i></div><span class="num">${r.n}</span><span class="num mut" style="width:52px">${i ? pct(r.n, d.reached[i - 1].n) : ''}</span></div>`).join('')}
      <div class="mut" style="margin-top:6px">${d.not_interested} marked Not interested. Percentages are step-to-step conversion.</div></div>
    <div class="panel"><h3>Last 14 days</h3><div style="display:flex;align-items:flex-end;gap:6px;height:90px">${d.trend.map(t => `<div style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;gap:2px;height:100%" title="${t.d}: ${t.sent} sent, ${t.replies} replies">
      <div style="height:${t.replies / tmax * 100}%;background:var(--s5);min-height:${t.replies ? 2 : 0}px"></div><div style="height:${t.sent / tmax * 100}%;background:var(--s1);min-height:${t.sent ? 2 : 0}px"></div></div>`).join('')}</div>
      <div class="mut" style="margin-top:6px"><i class="dot" style="background:var(--s1)"></i>Sent <i class="dot" style="background:var(--s5);margin-left:12px"></i>Replies</div></div>
    <div class="panel"><h3>By template</h3>${d.by_template.length ? `<table><thead><tr><th>Template</th><th class="r">Sent</th><th class="r">Replied</th><th class="r">Rate</th><th class="r">Bounced</th></tr></thead><tbody>${d.by_template.map(t => `<tr><td>${esc(t.name)}</td><td class="r">${t.sent}</td><td class="r">${t.replied}</td><td class="r">${pct(t.replied, t.sent)}</td><td class="r">${t.bounced}</td></tr>`).join('')}</tbody></table>` : '<div class="mut">No emails sent yet.</div>'}</div>
    <div class="panel"><h3>By segment</h3>${d.by_segment.length ? `<table><thead><tr><th>Segment</th><th class="r">Emailed</th><th class="r">Replied</th><th class="r">Rate</th></tr></thead><tbody>${d.by_segment.map(t => `<tr><td>${esc(t.name)}</td><td class="r">${t.sent}</td><td class="r">${t.replied}</td><td class="r">${pct(t.replied, t.sent)}</td></tr>`).join('')}</tbody></table>` : '<div class="mut">No emails sent yet.</div>'}</div>
    <div class="panel"><h3>By region</h3>${d.by_region.length ? `<table><thead><tr><th>Region</th><th class="r">Emailed</th><th class="r">Replied</th><th class="r">Rate</th></tr></thead><tbody>${d.by_region.map(t => `<tr><td>${esc(t.name)}</td><td class="r">${t.sent}</td><td class="r">${t.replied}</td><td class="r">${pct(t.replied, t.sent)}</td></tr>`).join('')}</tbody></table>` : '<div class="mut">No emails sent yet.</div>'}</div>
    <div class="panel"><h3>Why partners were lost</h3>${d.lost_reasons.length ? d.lost_reasons.map(r => `<div class="row"><span class="grow">${esc(r.name)}</span><span>${r.n}</span></div>`).join('') : '<div class="mut">No one marked Not interested yet.</div>'}</div>
  </div><div>
    <div class="panel"><h3>Replied — waiting on you</h3>${list(att.replied_waiting, 'x', r => ago(r.replied_at))}</div>
    <div class="panel"><h3>Overdue follow-ups</h3>${list(att.overdue, 'follow_up', r => fdate(r.follow_up))}</div>
    <div class="panel"><h3>In discussion — no next step set</h3>${list(att.stale_discussion, 'x', r => ago(r.last_activity))}</div>
  </div></div>`;
}

/* ---------------- sequence ---------------- */
async function sequence() {
  const [sq, tl] = await Promise.all([api('/api/sequence'), api('/api/templates')]);
  const steps = sq.steps;
  const tplOpts = sel => tl.rows.map(t => `<option value="${t.id}" ${t.id === sel ? 'selected' : ''}>${esc(t.name)}</option>`).join('');
  view.innerHTML = `<h1>Follow-up emails</h1><div class="sub">Automatic follow-ups for partners who haven't replied. Nothing sends until you review and confirm it here.</div>
  <div class="note">A partner is due for follow-up <i>N</i> when they're still in <b>Contacted</b>, their last email was step <i>N−1</i>, enough days have passed, and they haven't replied or opted out. A reply, a stage change, or do-not-contact removes them from the sequence automatically.</div>
  <div class="panel"><h3>Cadence</h3><div id="stepsed">${steps.map((s, i) => `<div class="sel2" data-i="${i}"><span class="mut" style="width:90px">Follow-up ${i + 1}</span> after <input class="sd" type="number" min="1" value="${s.delay_days}" style="width:60px"> days, send
      <select class="st">${tplOpts(s.template_id)}</select><button class="ghost rm">Remove</button></div>`).join('')}</div>
    <div style="display:flex;gap:8px;margin-top:8px"><button id="addstep" ${steps.length >= 6 ? 'disabled' : ''}>+ Add step</button><button id="savesteps">Save cadence</button></div></div>
  ${steps.map(s => `<div class="panel"><h3>Follow-up ${s.step_no} · ${s.due.length} due</h3>${s.due.length ? s.due.slice(0, 15).map(x => `<div class="row"><a class="grow" href="#/partner/${x.id}">${esc(x.name)}</a><small>${esc(x.segment)} · last email ${x.days_since}d ago</small></div>`).join('') + (s.due.length > 15 ? `<div class="mut" style="padding:6px 0">…and ${s.due.length - 15} more</div>` : '') +
    `<div style="margin-top:10px"><button class="primary rev" data-s="${s.step_no}">Review &amp; send ${s.due.length}</button></div>` : '<div class="mut">Nobody due right now.</div>'}</div>`).join('')}`;
  const readSteps = () => $$('#stepsed .sel2').map(r => ({delay_days: +$('.sd', r).value, template_id: +$('.st', r).value}));
  $('#addstep').onclick = async () => { const cur = readSteps(); cur.push({delay_days: 7, template_id: cur.length ? cur[cur.length - 1].template_id : tl.rows[0].id}); await api('/api/sequence', {body: {steps: cur}}); sequence(); };
  $$('.rm').forEach(b => b.onclick = async () => { const cur = readSteps(); cur.splice(+b.parentElement.dataset.i, 1); await api('/api/sequence', {body: {steps: cur}}); sequence(); });
  $('#savesteps').onclick = async () => { try { await api('/api/sequence', {body: {steps: readSteps()}}); toast('Cadence saved'); sequence(); refreshSide(); } catch (e) { toast(e.message); } };
  $$('.rev').forEach(b => b.onclick = () => { const s = steps.find(x => x.step_no === +b.dataset.s); bulkEmailDialog(s.due.map(x => x.id), {step: s.step_no, templateId: s.template_id, onDone: sequence}); });
}

/* ---------------- applications ---------------- */
let appTab = 'review';
async function applications() {
  const d = await api('/api/applications' + (appTab === 'review' ? '?status=review' : ''));
  const pendingN = appTab === 'review' ? d.rows.length : (await api('/api/applications?status=review')).rows.length;
  view.innerHTML = `<div class="top"><div><h1>Applications</h1><div class="sub">Partners who filled in the Belong Affiliate Partner form</div></div><span class="sp"></span>
    <button id="apull">Pull from relay</button><label class="ck" style="cursor:pointer"><input type="file" id="acsv" accept=".csv" hidden><button id="acsvb">Import Youform CSV</button></label>
    <div class="seg"><button data-t="review" class="${appTab === 'review' ? 'on' : ''}">Needs review ${pendingN}</button><button data-t="all" class="${appTab === 'all' ? 'on' : ''}">All</button></div></div><div id="ares" class="mut" style="margin-bottom:10px"></div>
  ${d.rows.length ? d.rows.map(a => `<div class="app" data-id="${a.id}"><h4>${esc(a.name || '—')}${a.company ? ' · ' + esc(a.company) : ''}</h4>
    <div class="kv">${esc(a.email)} ${a.phone ? '· ' + esc(a.phone) : ''} · ${ago(a.received_at)}</div>
    <div class="kv">${esc([a.applying_as, a.clients, a.products].filter(Boolean).join(' · '))}</div>
    ${a.profile ? `<div class="kv"><a href="${esc(a.profile)}" target="_blank" rel="noopener">${esc(a.profile)}</a></div>` : ''}
    ${a.status === 'review' ? `<div style="margin-top:8px"><b style="font-size:12px" class="mut">POSSIBLE MATCHES</b>${a.candidates.length ? a.candidates.map(p => `<div class="cand"><div class="grow"><a href="#/partner/${p.id}">${esc(p.name)}</a> <span class="conf ${p.confidence}">${esc(p.confidence)}</span>
        <div class="kv">${esc(p.email || '')} · ${esc(p.city || '')} · ${esc(p.stage)} · ${esc(p.why)}</div></div><button class="lk" data-p="${p.id}">This is them</button></div>`).join('') : '<div class="kv">None found.</div>'}
      <div class="acts"><input class="srch" placeholder="Search another partner…" style="min-width:220px"><button class="cp">Create new partner</button><button class="ghost ig">Ignore</button></div><div class="sres"></div></div>`
    : `<div class="kv" style="margin-top:6px">${a.status === 'ignored' ? 'Ignored' : `${a.status === 'new_partner' ? 'New partner created' : 'Linked to'} <a href="#/partner/${a.partner_id}">${esc(a.partner_name || '')}</a>${a.match_method ? ' · ' + esc(a.match_method) : ''}`}</div>`}</div>`).join('')
    : `<p class="mut">${appTab === 'review' ? 'Nothing waiting for review.' : 'No applications yet. Import a Youform CSV or connect the relay in Settings.'}</p>`}`;
  $$('.seg button').forEach(b => b.onclick = () => { appTab = b.dataset.t; applications(); });
  const done = msg => { toast(msg); applications(); refreshSide(); };
  $('#acsvb').onclick = e => { e.preventDefault(); $('#acsv').click(); };
  $('#acsv').onchange = async e => { const f = e.target.files[0]; if (!f) return; try { const r = await api('/api/applications/import-csv', {body: {csv: await f.text()}}); done(`${r.rows} rows: ${r.linked} linked, ${r.new_partner} new, ${r.review} to review, ${r.duplicate} already imported`); } catch (err) { toast(err.message); } };
  $('#apull').onclick = async () => { $('#ares').textContent = 'Pulling…'; try { const r = await api('/api/youform/pull', {body: {}}); done(`${r.pulled} pulled: ${r.linked} linked, ${r.new_partner} new, ${r.review} to review`); } catch (err) { $('#ares').textContent = err.message; } };
  $$('.app').forEach(card => {
    const id = card.dataset.id, link = async pid => { await api(`/api/applications/${id}/link`, {body: {partner_id: pid}}); done('Linked — partner moved to Applied'); };
    $$('.lk', card).forEach(b => b.onclick = () => link(+b.dataset.p));
    const cp = $('.cp', card); if (cp) cp.onclick = async () => { try { await api(`/api/applications/${id}/create-partner`, {body: {}}); done('New partner created'); } catch (err) { toast(err.message); } };
    const ig = $('.ig', card); if (ig) ig.onclick = async () => { await api(`/api/applications/${id}/ignore`, {body: {}}); done('Ignored'); };
    const sr = $('.srch', card); if (sr) { let t; sr.oninput = () => { clearTimeout(t); t = setTimeout(async () => {
      const q = sr.value.trim(); const box = $('.sres', card); if (q.length < 2) { box.innerHTML = ''; return; }
      const r = await api('/api/partners?' + new URLSearchParams({q, limit: 6}));
      box.innerHTML = r.rows.map(p => `<div class="cand"><div class="grow">${esc(p.name)} <span class="kv">${esc(p.email || '')} · ${esc(p.stage)}</span></div><button data-p="${p.id}">Link</button></div>`).join('');
      $$('button', box).forEach(b => b.onclick = () => link(+b.dataset.p)); }, 250); }; }
  });
}

/* ---------------- performance (attribution) ---------------- */
async function performance() {
  const d = await api('/api/attribution/summary');
  const t = d.totals, pct = (n, m) => m ? Math.round(n / m * 100) + '%' : '—';
  view.innerHTML = `<div class="top"><div><h1>Partner performance</h1><div class="sub">Clients each partner has brought through their own link</div></div><span class="sp"></span>
    <span class="mut" id="psync">${d.last_sync ? 'Last sync ' + esc(ago(d.last_sync)) : 'Not synced yet'}</span><button id="pnow" ${d.configured ? '' : 'disabled'}>Sync now</button></div>
  ${!d.configured ? '<div class="note warn">Set the AppsFlyer OneLink base in <a href="#/settings">Settings → Partner attribution</a> so each partner gets a link. Until partners share their links and clients install through them, this page stays empty.</div>' : ''}
  <div class="kpis">${d.steps.map((s, i) => `<div class="kpi"><div class="n">${t[s.key] || 0}</div><div class="l">${s.label}${i ? ' · ' + pct(t[s.key] || 0, t.installed) : ''}</div></div>`).join('')}
    <div class="kpi"><div class="n">${d.partners_with_clients}</div><div class="l">Partners with clients (of ${d.partners_applied_plus} applied+)</div></div></div>
  ${d.rows.length ? `<table><thead><tr><th>Partner</th><th>Country</th><th>Code</th><th>Stage</th>${d.steps.map(s => `<th class="r">${s.label}</th>`).join('')}</tr></thead><tbody>
    ${d.rows.map(r => `<tr class="link" data-id="${r.id}"><td><b>${esc(r.name)}</b></td><td>${r.region === 'Foreign' ? `<span class="region Foreign">${esc(r.country)}</span>` : 'India'}</td><td class="mut">${esc(r.code)}</td><td>${sdot(r.stage)}${esc(r.stage)}</td>${d.steps.map(s => `<td class="r">${r[s.key] || 0}</td>`).join('')}</tr>`).join('')}</tbody></table>`
    : '<p class="mut">No attributed clients yet. Copy a partner\'s link from their page, share it, and clients who install through it will appear here after the next sync (hourly, or "Sync now").</p>'}
  <div class="mut" style="margin-top:14px;font-size:12px">Counts are customers who installed via the partner's link. "MF transaction" is a first mutual-fund transaction event; F&amp;O trading isn't in ClickHouse, so it isn't counted here.</div>`;
  $$('tr.link').forEach(tr => tr.onclick = () => location.hash = '#/partner/' + tr.dataset.id);
  const now = $('#pnow'); if (now) now.onclick = async () => { now.disabled = true; $('#psync').textContent = 'Syncing… (up to a minute)'; try { await api('/api/attribution/sync', {body: {}}); for (;;) { await new Promise(r => setTimeout(r, 1500)); const j = await api('/api/attribution/sync'); if (j.state === 'done') { toast(j.result.summary); break; } if (j.state === 'error') { toast(j.error); break; } } } catch (e) { toast(e.message); } performance(); };
}






/* ---------------- missing emails ---------------- */
const rf = {q: '', segment: '', stage: '', show: 'needs'};
async function reach() {
  const p = new URLSearchParams({show: rf.show, q: rf.q, segment: rf.segment, stage: rf.stage});
  const d = await api('/api/reach?' + p);
  const pct = d.total ? Math.round(d.reachable * 100 / d.total) : 0;
  view.innerHTML = `<div class="top"><div><h1>Missing emails</h1><div class="sub" style="margin:0">Partners who can't be emailed yet. Find an address, type it in, press Enter.</div></div><span class="sp"></span>
    <input id="rq" placeholder="Search name, contact, city…" value="${esc(rf.q)}" style="min-width:220px">
    <select id="rseg"><option value="">All segments</option>${['Corporate', 'Individual', 'Bank'].map(x => `<option ${rf.segment === x ? 'selected' : ''}>${x}</option>`).join('')}</select>
    <select id="rstg"><option value="">All stages</option>${STAGES.map(x => `<option ${rf.stage === x ? 'selected' : ''}>${x}</option>`).join('')}</select>
    <select id="rshow"><option value="needs" ${rf.show === 'needs' ? 'selected' : ''}>Missing or invalid</option><option value="missing" ${rf.show === 'missing' ? 'selected' : ''}>Missing only</option><option value="invalid" ${rf.show === 'invalid' ? 'selected' : ''}>Invalid format only</option></select></div>
  <div class="kpis" style="grid-template-columns:repeat(auto-fit,minmax(170px,1fr))"><div class="panel" style="margin:0"><h3>Can be emailed</h3><div style="font-size:22px;font-weight:700">${d.reachable} <small class="mut">of ${d.total} · ${pct}%</small></div></div>
    <div class="panel" style="margin:0"><h3>No address</h3><div style="font-size:22px;font-weight:700">${d.missing}</div></div><div class="panel" style="margin:0"><h3>Address looks wrong</h3><div style="font-size:22px;font-weight:700">${d.invalid}</div></div></div>
  <div class="hint" style="margin:6px 0 12px">Archived and do-not-contact partners are left out. ${d.matching > d.shown ? `Showing the first ${d.shown} of ${d.matching}.` : ''}</div>
  ${d.rows.length ? `<div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap"><button id="rsaveall" class="primary">Save all I've typed</button><button id="rcopy" title="Copies id, name, city, website for a spreadsheet; fill the last column and paste it back below">Copy list for a spreadsheet</button></div>
  <table><thead><tr><th>Partner</th><th>Contact / phone</th><th>Find it</th><th>Email</th></tr></thead><tbody>${d.rows.map(r => `<tr><td><a href="#/partner/${r.id}"><b>${esc(r.name)}</b></a><br><small class="mut">${esc([r.segment, r.stage, r.city, r.country && r.country !== 'India' ? r.country : ''].filter(Boolean).join(' · '))}</small></td>
    <td>${esc(r.contact_person || '')}${r.phone ? `<br><small class="mut">${esc(r.phone)}</small>` : ''}</td>
    <td style="white-space:nowrap">${/^https?:\/\//i.test(r.website || '') ? `<a href="${esc(r.website)}" target="_blank" rel="noopener">Website</a> · ` : ''}${/^https?:\/\//i.test(r.linkedin || '') ? `<a href="${esc(r.linkedin)}" target="_blank" rel="noopener">LinkedIn</a> · ` : ''}<a href="https://www.google.com/search?q=${encodeURIComponent([r.name, r.city, 'contact email'].filter(Boolean).join(' '))}" target="_blank" rel="noopener">Search</a></td>
    <td><input class="reml" data-id="${r.id}" placeholder="${r.kind === 'invalid' ? '' : 'name@company.com'}" value="${r.kind === 'invalid' ? esc(r.email) : ''}" style="width:260px;${r.kind === 'invalid' ? 'border-color:var(--warn)' : ''}">${r.kind === 'invalid' ? ' <small class="down">check this</small>' : ''}</td></tr>`).join('')}</tbody></table>`
    : '<div class="note ok">Everyone in this view has a valid email address.</div>'}
  <div class="panel" style="margin-top:16px"><h3>Fill many at once</h3><div class="hint" style="margin-bottom:8px">Paste rows from a spreadsheet: partner number, then the email, one per line (tab or comma between). Use “Copy list for a spreadsheet” to get the numbers.</div>
    <textarea id="rpaste" rows="5" placeholder="123&#9;name@company.com" style="width:100%"></textarea><div style="margin-top:8px"><button id="rimport">Import these</button></div></div>`;
  const reload = () => reach();
  const save = async rows => {
    if (!rows.length) return toast('Nothing to save yet');
    try { const r = await api('/api/reach/emails', {body: {rows}}); toast(`Saved ${r.saved}` + (r.errors.length ? `. ${r.errors.length} not saved: ${r.errors[0].why}` : '') + (r.warnings.length ? `. Note: ${r.warnings[0].name} — ${r.warnings[0].why}` : '')); refreshSide(); reload(); }
    catch (e) { toast(e.message); }
  };
  $('#rq').oninput = e => { clearTimeout(reach.t); reach.t = setTimeout(() => { rf.q = e.target.value; reach().then(() => { const i = $('#rq'); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }); }, 300); };
  $('#rseg').onchange = e => { rf.segment = e.target.value; reload(); }; $('#rstg').onchange = e => { rf.stage = e.target.value; reload(); }; $('#rshow').onchange = e => { rf.show = e.target.value; reload(); };
  $$('.reml').forEach(i => i.onkeydown = e => { if (e.key === 'Enter') save([{id: +i.dataset.id, email: i.value}]); });
  const sa = $('#rsaveall'); if (sa) sa.onclick = () => save($$('.reml').filter(i => i.value.trim()).map(i => ({id: +i.dataset.id, email: i.value})));
  const rc = $('#rcopy'); if (rc) rc.onclick = async () => { const tsv = d.rows.map(r => [r.id, r.name, r.city || '', r.website || '', ''].join('\t')).join('\n'); try { await navigator.clipboard.writeText(tsv); toast('Copied. Paste into a spreadsheet, fill the last column, then paste id + email back below.'); } catch { toast('Could not copy. Allow clipboard access and try again.'); } };
  $('#rimport').onclick = () => { const rows = $('#rpaste').value.split('\n').map(l => l.split(/\t|,/).map(x => x.trim())).filter(a => /^\d+$/.test(a[0]) && a[1]).map(a => ({id: +a[0], email: a[1]})); rows.length ? save(rows) : toast('No usable rows. Each line needs a partner number and an email.'); };
}

/* ---------------- send log ---------------- */
async function sends() {
  const d = await api('/api/sends?limit=80');
  const KIND = {bulk: 'Bulk send', followup: 'Follow-up', single: 'Single email', test: 'Test email'};
  view.innerHTML = `<div class="top"><div><h1>Send log</h1><div class="sub" style="margin:0">Every send: who it went to, who was skipped and why, and anything that failed.</div></div></div>
  ${d.rows.length ? `<table><thead><tr><th>Started</th><th>What</th><th>By</th><th>Template</th><th>Sent</th><th>Skipped</th><th>Failed</th><th>Result</th></tr></thead><tbody>${d.rows.map(r => `<tr class="link sj" data-id="${esc(r.id)}"><td>${esc(r.started.replace('T', ' ').slice(0, 16))}</td><td>${KIND[r.kind] || esc(r.kind)}</td><td>${esc(whoLabel(r.by))}</td><td style="white-space:normal">${esc(r.template || '')}</td><td>${r.sent}<small class="mut"> of ${r.total}</small></td><td>${r.skipped || ''}</td><td class="${r.failed ? 'down' : ''}">${r.failed || ''}</td>
    <td><i class="dot ${r.state === 'running' ? 'st2' : r.state === 'stopped' || r.failed ? 'st6' : 'st5'}"></i>${r.state === 'running' ? 'Running…' : r.state === 'stopped' ? esc(r.stopped_reason || 'Stopped') : r.failed ? 'Finished with failures' : 'Done'}</td></tr><tr class="sjd" id="sj-${esc(r.id)}" hidden><td colspan="8"></td></tr>`).join('')}</tbody></table>` : '<div class="mut">No sends yet. They appear here as soon as you send.</div>'}`;
  $$('.sj').forEach(tr => tr.onclick = async () => {
    const row = $('#sj-' + tr.dataset.id), cell = row.firstElementChild;
    if (!row.hidden) { row.hidden = true; return; }
    row.hidden = false; cell.innerHTML = '<div class="mut">Loading…</div>';
    try {
      const j = await api('/api/sends/' + tr.dataset.id), dt = j.details || {};
      const why = {}; (dt.skipped || []).forEach(x => (why[x.why] = why[x.why] || []).push(x.name || '—'));
      cell.innerHTML = `<div style="padding:6px 4px;white-space:normal">${(dt.sent || []).length ? `<div style="margin-bottom:8px"><b>Sent (${j.sent})</b>: ${dt.sent.map(x => `<a href="#/partner/${x.id}">${esc(x.name)}</a> <small class="mut">${esc(x.to)}</small>`).join(' · ')}</div>` : ''}
        ${Object.keys(why).length ? `<div style="margin-bottom:8px"><b>Skipped (${j.skipped})</b><br>${Object.entries(why).map(([k, v]) => `<span class="chip">${esc(k)}: ${v.length}</span> ${esc(v.slice(0, 12).join(', '))}${v.length > 12 ? '…' : ''}`).join('<br>')}</div>` : ''}
        ${(dt.failed || []).length ? `<div style="margin-bottom:4px"><b class="down">Failed (${j.failed})</b><br>${dt.failed.map(x => `${esc(x.name)}: <span class="down">${esc(x.why)}</span>`).join('<br>')}</div>` : ''}
        ${j.stopped_reason ? `<div class="note warn">${esc(j.stopped_reason)}</div>` : ''}${!(dt.sent || []).length && !Object.keys(why).length && !(dt.failed || []).length ? '<div class="mut">No details recorded.</div>' : ''}</div>`;
    } catch (e) { cell.innerHTML = `<div class="note warn">${esc(e.message)}</div>`; }
  });
}

/* ---------------- change history + undo ---------------- */
const whoLabel = a => !a || a === 'system' ? 'automatic' : a;
const showVal = v => v === null || v === undefined || v === '' ? '(empty)' : String(v).length > 40 ? String(v).slice(0, 40) + '…' : String(v);
function changesHtml(groups, showPartner) {
  const rows = groups.filter(g => g.undoable);
  if (!rows.length) return '<div class="mut">No recent edits to undo.</div>';
  return rows.map(g => `<div class="row" style="align-items:flex-start"><div class="grow" style="white-space:normal">${showPartner ? `<a href="#/partner/${g.partner_id}"><b>${esc(g.partner || 'Deleted partner')}</b></a><br>` : ''}${g.changes.filter(x => !x.undone).map(x => `<span class="chip">${esc(x.label)}: ${esc(showVal(x.old))} → ${esc(showVal(x.new))}</span>`).join(' ')}<br><small class="mut">${esc(g.ts.replace('T', ' ').slice(0, 16))} · by ${esc(whoLabel(g.actor))}</small></div><button class="ghost undo" data-ids="${g.changes.filter(x => !x.undone).map(x => x.id).join(',')}">Undo</button></div>`).join('');
}
function wireUndo(host, after) {
  host.querySelectorAll('.undo').forEach(b => b.onclick = async () => {
    b.disabled = true;
    try { const r = await api('/api/changes/undo', {body: {ids: b.dataset.ids.split(',').map(Number)}}); toast(r.undone ? 'Change undone' + (r.skipped.length ? '. ' + r.skipped[0] : '') : (r.skipped[0] || 'Nothing to undo')); after(); refreshSide(); }
    catch (e) { toast(e.message); b.disabled = false; }
  });
}


/* ---------------- send review + size warning ---------------- */
const fdur = sec => sec < 90 ? `${Math.max(1, Math.round(sec))} sec` : sec < 5400 ? `${Math.round(sec / 60)} min` : `${(sec / 3600).toFixed(1)} hours`;
function sizeWarn(sizes, count, est) {
  const big = Object.entries(sizes || {}).filter(([, v]) => v.mb >= 3).sort((a, b) => b[1].mb - a[1].mb);
  if (!big.length) return '';
  const [name, v] = big[0], vbig = v.mb >= 10;
  return `<div class="note warn"><b>${vbig ? 'Very large' : 'Large'} email: about ${v.mb} MB each</b> (${esc(name)}${v.attachments ? `, ${v.attachments} attachment${v.attachments === 1 ? '' : 's'} totalling ${v.attachment_mb} MB` : ', mostly pasted pictures'}).
    ${count > 1 ? `Sending ${count} will take about <b>${fdur(est)}</b> and the page must stay open. ` : `This email will take about <b>${fdur(est)}</b> to send. `}Big emails are slower, more likely to land in spam, and some mail systems reject anything over 10–25 MB.
    ${vbig ? '<b>Strongly consider sharing the file as a link (Drive) instead.</b>' : 'Consider sharing the file as a link if the recipients matter.'}</div>`;
}
function reviewHtml(pl, st) {
  const rv = pl.review; if (!rv) return '';
  const rows = rv.rows;
  return `<div class="panel" style="margin:10px 0;padding:12px"><h3 style="margin:0 0 8px">Review before sending</h3>
    ${rv.samples.length ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">${rv.samples.map((x, i) => `<button type="button" class="ghost rvtab ${i === st.sample ? 'on' : ''}" data-i="${i}" style="padding:3px 10px;font-size:12px;${i === st.sample ? 'border-color:var(--brand);color:var(--brand)' : ''}">Sample ${i + 1}: ${esc(x.name.slice(0, 22))}</button>`).join('')}</div>
      <div class="hint" id="rvmeta" style="margin-bottom:6px"></div><iframe id="rvframe" class="prev" sandbox style="height:300px"></iframe>` : '<div class="mut">No email to preview.</div>'}
    ${rows.length ? `<div style="margin-top:12px"><b>${rv.flagged} of ${rv.checked} need a second look.</b> <span class="mut">Untick anyone to leave them out of this send.</span></div>
      <div class="chg" style="max-height:210px;margin-top:6px">${rows.map(r => `<label class="row" style="align-items:flex-start;cursor:pointer"><input type="checkbox" class="rvck" data-id="${r.id}" ${r.excluded ? '' : 'checked'} style="margin-top:4px"><span class="grow" style="white-space:normal"><b>${esc(r.name)}</b> <small class="mut">${esc(r.email)} · ${esc(r.stage)}</small><br>${r.flags.map(f => `<span class="chip">${esc(f)}</span>`).join(' ')}</span></label>`).join('')}</div>`
      : '<div class="mut" style="margin-top:10px">Nobody is flagged: no recent emails, no earlier replies or bounces, no duplicate addresses, all greetings filled.</div>'}
    ${rv.generic_greeting ? `<div class="hint" style="margin-top:8px">${rv.generic_greeting} of ${rv.checked} have no contact name, so their greeting will read “Hi there”.</div>` : ''}
    ${pl.sendable >= 10 ? `<label class="ck" style="margin-top:10px"><input type="checkbox" id="rvack" ${st.ack ? 'checked' : ''}> I've looked at the sample emails and the flagged list</label>` : ''}</div>`;
}
function wireReview(pl, st, redraw) {
  const rv = pl.review; if (!rv) return;
  const show = () => { const x = rv.samples[st.sample], fr = $('#rvframe'); if (!x || !fr) return; fr.srcdoc = cidFix(x.html); $('#rvmeta').innerHTML = `To <b>${esc(x.to)}</b> · Subject: <b>${esc(x.subject)}</b> · Template: ${esc(x.template)}`; };
  show();
  $$('.rvtab').forEach(b => b.onclick = () => { st.sample = +b.dataset.i; redraw(); });
  $$('.rvck').forEach(b => b.onchange = () => { b.checked ? st.excluded.delete(+b.dataset.id) : st.excluded.add(+b.dataset.id); redraw(); });
  const a = $('#rvack'); if (a) a.onchange = () => { st.ack = a.checked; const sb = $('#bsend'); if (sb) sb.disabled = !(pl.configured && pl.sendable && (pl.sendable < 10 || st.ack)); };
}

/* ---------------- backup helpers: back-up-now row, restore, compare ---------------- */
async function backupNowRow(host) {
  if (!host) return;
  const draw = async () => {
    const st = await api('/api/backup/status').catch(() => null);
    host.innerHTML = `<span><i class="dot ${st && st.state === 'ok' ? 'st5' : st && st.state === 'bad' ? 'st6' : 'st2'}"></i>${esc(st ? st.label : 'Backup status unknown')}</span><button class="ghost" type="button" id="bknow2">Back up now</button>`;
    host.querySelector('#bknow2').onclick = async e => { e.target.disabled = true; e.target.textContent = 'Backing up…'; try { const r = await api('/api/backup/run', {body: {}}); toast(r.ok ? 'Backup done' : 'Backup failed: ' + r.error); } catch (x) { toast(x.message); } draw(); refreshSide(); };
  };
  draw();
}
const signed = n => (n > 0 ? '+' : '') + n;
const diffCls = n => n > 0 ? 'up' : n < 0 ? 'down' : 'mut';
async function restoreDialog(name, label) {
  openModal(`<h2>Restore everything from ${esc(label)}</h2><div id="rbody"><div class="mut">Checking the backup…</div></div>`);
  let pv;
  try { pv = await api('/api/backup/preview', {body: {name}}); } catch (e) { $('#rbody').innerHTML = `<div class="note warn">${esc(e.message)}</div><div class="acts"><button id="rc">Close</button></div>`; $('#rc').onclick = closeModal; return; }
  $('#rbody').innerHTML = `<div class="note warn">This replaces <b>all</b> CRM data (partners, notes, emails, tasks, templates) with this backup. Anything added since then will disappear from the CRM. Your current data is saved first as a safety copy, so you can undo this by restoring that copy. Passwords and settings are not touched.</div>
    ${pv.integrity !== 'ok' ? `<div class="note warn">This backup failed its check (${esc(pv.integrity)}). Don't restore it.</div>` : ''}
    <table><thead><tr><th></th><th>In this backup</th><th>Now</th><th>Change</th></tr></thead><tbody>${pv.rows.map(r => `<tr><td>${esc(r.label)}</td><td>${r.backup}</td><td>${r.now}</td><td class="${diffCls(r.diff)}">${r.diff ? signed(r.diff) : 'same'}</td></tr>`).join('')}</tbody></table>
    <div class="fields" style="margin-top:12px"><span>Type RESTORE</span><input id="rtype" placeholder="RESTORE" autocomplete="off"></div>
    <div class="acts"><button id="rc" class="ghost">Cancel</button><button id="rgo" class="primary" disabled>Restore everything</button></div>`;
  $('#rc').onclick = closeModal;
  $('#rtype').oninput = () => { $('#rgo').disabled = $('#rtype').value.trim() !== 'RESTORE' || pv.integrity !== 'ok'; };
  $('#rgo').onclick = async () => { const b = $('#rgo'); b.disabled = true; b.textContent = 'Restoring…'; try { const r = await api('/api/backup/restore', {body: {name, confirm: 'RESTORE'}}); closeModal(); toast(`Restored ${r.partners} partners. Your previous data was saved as ${r.safety}.`); setTimeout(() => location.reload(), 1500); } catch (e) { toast(e.message); b.textContent = 'Restore everything'; b.disabled = false; } };
}
async function compareDialog(snaps, a, b = 'live') {
  const opt = (v, sel) => snaps.map(s => `<option value="${esc(s.file)}" ${s.file === v ? 'selected' : ''}>${esc(s.label)}</option>`).join('');
  openModal(`<h2>What changed</h2><div class="fields"><span>From</span><select id="ca">${opt(a)}</select><span>To</span><select id="cb"><option value="live">Now (live data)</option>${opt(b)}</select></div><div id="cbody" class="chg"><div class="mut">Comparing…</div></div><div class="acts"><button id="cx" class="ghost">Close</button></div>`);
  $('#cb').value = b; $('#cx').onclick = closeModal;
  const draw = async () => {
    const A = $('#ca').value, B = $('#cb').value;
    if (A === B) { $('#cbody').innerHTML = '<div class="mut">Pick two different points in time.</div>'; return; }
    let r; try { r = await api('/api/backup/compare', {body: {a: A, b: B}}); } catch (e) { $('#cbody').innerHTML = `<div class="note warn">${esc(e.message)}</div>`; return; }
    const names = (list, n, back) => list.length ? `<div style="margin:6px 0 12px"><b>${n}</b>: ${list.map(x => `<span class="chip">${esc(x.name)}${back ? ` <a href="#" class="bring" data-id="${x.id}" title="Bring this partner back from the earlier copy">bring back</a>` : ''}</span>`).join(' ')}${n > list.length ? ' …' : ''}</div>` : '';
    $('#cbody').innerHTML = `${r.warnings.map(w => `<div class="note warn">${esc(w)}</div>`).join('')}
      <table><thead><tr><th></th><th>${esc(r.from_label)}</th><th>${esc(r.to_label)}</th><th>Change</th></tr></thead><tbody>${r.counts.map(x => `<tr><td>${esc(x.label)}</td><td>${x.before}</td><td>${x.after}</td><td class="${diffCls(x.diff)}">${x.diff ? signed(x.diff) : 'same'}</td></tr>`).join('')}</tbody></table>
      ${names(r.added, r.added_n, false).replace('<b>' + r.added_n + '</b>', '<b>Partners added (' + r.added_n + ')</b>')}${names(r.removed, r.removed_n, A !== 'live').replace('<b>' + r.removed_n + '</b>', '<b>Partners removed (' + r.removed_n + ')</b>')}
      ${r.changed_partners ? `<div style="margin:10px 0 4px"><b>${r.changed_partners} partner${r.changed_partners === 1 ? '' : 's'} edited</b> · ${Object.entries(r.by_field).map(([k, v]) => `${v} × ${esc(k.replace(/_/g, ' '))}`).join(', ')}</div>
      <table><thead><tr><th>Partner</th><th>Field</th><th>Was</th><th>Now</th></tr></thead><tbody>${r.examples.map(x => `<tr><td><a href="#/partner/${x.id}" onclick="closeModal()">${esc(x.name)}</a></td><td>${esc(x.field.replace(/_/g, ' '))}</td><td>${esc(x.from) || '—'}</td><td>${esc(x.to) || '—'}</td></tr>`).join('')}</tbody></table>` : '<div class="mut" style="margin-top:8px">No partner details were edited.</div>'}`;
    $$('#cbody .bring').forEach(l => l.onclick = async e => { e.preventDefault(); if (!confirm('Bring this partner back, with their notes, emails, tasks and tags?')) return; try { const x = await api('/api/backup/restore-partner', {body: {name: A, partner_id: +l.dataset.id}}); toast(`${x.name} is back`); closeModal(); location.hash = '#/partner/' + x.id; } catch (er) { toast(er.message); } });
  };
  $('#ca').onchange = draw; $('#cb').onchange = draw; draw();
}
function restorePartnerDialog(name, label) {
  openModal(`<h2>Bring back one partner</h2><div class="hint" style="margin-bottom:8px">Searching the backup from ${esc(label)}. Everything else in the CRM stays as it is.</div><input id="rq" placeholder="Partner name or email…" style="width:100%"><div id="rlist" style="margin-top:8px"></div><div class="acts"><button id="rc" class="ghost">Close</button></div>`);
  $('#rc').onclick = closeModal;
  const load = async () => {
    const q = $('#rq').value.trim(); if (q.length < 2) { $('#rlist').innerHTML = '<div class="mut">Type at least 2 letters.</div>'; return; }
    const r = (await api('/api/backup/partners', {body: {name, q}})).rows;
    $('#rlist').innerHTML = r.map(x => `<div class="row"><div class="grow">${esc(x.name)}<br><small>${esc(x.stage)} · ${esc([x.email, x.city, x.country].filter(Boolean).join(' · '))} · ${x.in_live ? 'still in the CRM' : x.erased ? 'permanently deleted earlier' : 'missing from the CRM'}</small></div><button data-id="${x.id}" data-live="${x.in_live ? 1 : 0}" data-er="${x.erased ? 1 : 0}">${x.in_live ? 'Replace with this version' : 'Bring back'}</button></div>`).join('') || '<div class="mut">No matches in that backup.</div>';
    $$('#rlist button').forEach(b => b.onclick = async () => {
      const live = b.dataset.live === '1', er = b.dataset.er === '1';
      if (live && !confirm('Overwrite this partner\'s current details, notes, emails and tasks with the backed-up version? A safety copy is taken first.')) return;
      if (er && !confirm('This partner was permanently deleted on purpose. Bring them back anyway?')) return;
      b.disabled = true;
      try { const x = await api('/api/backup/restore-partner', {body: {name, partner_id: +b.dataset.id, replace: live, allow_erased: er}}); closeModal(); toast(`${x.name} ${x.replaced ? 'restored to the backed-up version' : 'is back'}`); location.hash = '#/partner/' + x.id; refreshSide(); } catch (e) { toast(e.message); b.disabled = false; }
    });
  };
  let t; $('#rq').oninput = () => { clearTimeout(t); t = setTimeout(load, 250); }; $('#rq').focus();
}

/* ---------------- backups ---------------- */
const HEALTH = {ok: ['Up to date', 'st5'], stale: ['Out of date', 'st2'], failed: ['Failed', 'st6'], off: ['Not set up', 'st0']};
async function backups() {
  const [d, sn, chg] = await Promise.all([api('/api/gdrive'), api('/api/backup/snapshots'), api('/api/changes?limit=40&hours=168')]);
  const snaps = sn.rows;
  const h = d.health, allOk = h.local.state === 'ok' && h.drive.state === 'ok' && h.sheets.state === 'ok';
  const card = (title, sub, x) => `<div class="panel" style="margin:0"><h3>${title}</h3><div style="font-size:16px;font-weight:650"><i class="dot ${HEALTH[x.state][1]}"></i>${HEALTH[x.state][0]}</div>
    <div class="mut" style="margin-top:4px">${x.when ? 'Last: ' + esc(ago(x.when)) : ''}</div><div class="hint" style="margin-top:6px;white-space:normal">${esc(x.detail || sub)}</div></div>`;
  view.innerHTML = `<div class="top"><div><h1>Backups</h1><div class="sub" style="margin:0">Keeps your data safe outside this Mac, so a broken laptop doesn't lose your pipeline.</div></div><span class="sp"></span>
      <button id="bnow" class="primary" ${d.configured ? '' : 'disabled'} title="${d.configured ? '' : 'Connect Google first'}">Back up everything now</button></div>
  <div class="note ${allOk ? 'ok' : d.configured ? '' : 'warn'}">${allOk ? 'Everything is backed up, locally and on Google.' : d.configured ? 'Some backups are out of date. Check the cards below.' : '<b>Your data exists only on this Mac.</b> Connect Google below to keep a copy in your Drive plus a Sheet and dashboard you can open if this Mac fails.'}</div>
  ${d.autostart ? '' : '<div class="note warn"><b>The CRM is not set to start by itself.</b> Backups only run while it is running, so after a restart they would silently stop. In Terminal run: <code>python3 ~/partner-crm/autostart.py install</code> (it starts the CRM at login and restarts it if it ever stops).</div>'}
  <div class="kpis" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))">${card('On this Mac', 'Daily copy of the database and documents', h.local)}${card('Google Drive', 'Database, documents and app code', h.drive)}${card('Google Sheet + dashboard', 'Partners, pipeline, onboarding, commercials', h.sheets)}</div>
  <div id="bprog"></div>
  ${d.configured ? `<div class="panel"><h3>Google connection</h3>
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">${d.root_url ? `<a href="${esc(d.root_url)}" target="_blank" rel="noopener"><button>Open Drive backup folder</button></a>` : ''}${d.sheet_url ? `<a href="${esc(d.sheet_url)}" target="_blank" rel="noopener"><button>Open Sheet + dashboard</button></a>` : ''}<button id="gtest">Test connection</button><button id="gverify" title="Asks Google whether every backed-up file is really there and the database copy matches">Verify Drive copy</button></div>
      ${d.last_verify ? `<div class="hint" style="margin:-4px 0 10px">Last check ${esc(ago(d.last_verify))}: ${esc(d.last_verify_result)}</div>` : ''}
      <div class="hint" style="margin:0 0 10px">Restore test (monthly, automatic): ${d.last_drill ? `${esc(ago(d.last_drill))}. ${esc(d.last_drill_result)}` : 'not run yet'} <button id="gdrill" class="ghost" style="padding:2px 10px;font-size:12px;margin-left:6px" title="Downloads your Drive backup into a scratch folder, opens it and checks it. Nothing in the CRM changes.">Run restore test now</button></div>
      <div class="fields"><span>Automatic</span><label class="ck"><input type="checkbox" id="gen" ${d.enabled ? 'checked' : ''}> back up every day, refresh the Sheet every few hours</label>
        <span>Documents</span><label class="ck"><input type="checkbox" id="gdoc" ${d.include_documents ? 'checked' : ''}> include onboarding documents (KYC files) in Drive</label>
        <span>App code</span><label class="ck"><input type="checkbox" id="gcode" ${d.include_code ? 'checked' : ''}> include a copy of the CRM app, so a new Mac can run it</label>
        <span>Sheet refresh</span><span style="display:flex;gap:6px;align-items:center">every <input id="ghrs" type="number" min="1" value="${d.sheet_sync_hours}" style="width:70px"> hours</span>
        <span>Keep copies</span><span style="display:flex;gap:6px;align-items:center"><input id="gkeep" type="number" min="7" value="${d.keep_days}" style="width:70px"> days</span></div>
      <div style="display:flex;gap:8px;margin-top:12px"><button id="gsave" class="primary">Save</button><button id="gdisc" class="ghost" style="margin-left:auto">Disconnect Google</button></div>
      <div class="hint" style="margin-top:10px">If backups ever stop arriving for 36 hours, Google emails you from your own account. Passwords and API keys are never uploaded.</div></div>`
    : `<div class="panel"><h3>Connect Google (one-time, about 5 minutes)</h3>
      <div class="hint" style="margin-bottom:10px">This uses a small script that runs inside <b>your</b> Google account. It saves backups to a Drive folder it creates, fills a Google Sheet, and emails you if backups stop. Nobody else can use it: it only accepts requests carrying a secret key unique to your CRM.</div>
      <ol style="padding-left:20px;line-height:1.9;margin:0">
        <li>Click <button id="gcopy">Copy the script</button> (it already contains your secret key).</li>
        <li>Open <a href="https://script.google.com/home/projects/create" target="_blank" rel="noopener">script.google.com</a> and create a new project. Delete the sample code and paste the script. Save.</li>
        <li>Pick the function <b>setup</b> in the toolbar and click <b>Run</b>. Approve the permissions (Drive, Sheets, email). Open <i>Execution log</i> to see your new Drive folder and Sheet.</li>
        <li><b>Deploy → New deployment → Web app</b>. Set <i>Execute as: Me</i> and <i>Who has access: Anyone</i>. Click Deploy and copy the Web app URL.</li>
        <li>Paste the URL here and connect:<div style="display:flex;gap:8px;margin-top:6px"><input id="gurl" placeholder="https://script.google.com/macros/s/…/exec" style="flex:1"><button id="gconn" class="primary">Connect and test</button></div></li></ol></div>`}
  <div class="panel"><h3>On this Mac</h3><div class="fields"><span>Folder</span><input id="bkf" value="${esc(d.local.folder)}"><span>Keep for</span><span style="display:flex;gap:6px;align-items:center"><input id="bkd" type="number" min="7" value="${d.local.keep_days}" style="width:80px"> days</span><span>Automatic</span><label class="ck"><input type="checkbox" id="bke" ${d.local.enabled ? 'checked' : ''}> daily, and before any merge or delete</label></div>
    <div style="display:flex;gap:8px;margin-top:12px"><button id="bksave">Save</button><button id="bklocal">Back up on this Mac now</button></div></div>


  <div class="panel"><h3>Recent changes <small class="mut">last 7 days, undo any of them</small></h3><div id="chgl">${changesHtml(chg.rows, true)}</div>
    <div class="hint" style="margin-top:8px">Covers edits to a partner's stage, do-not-contact, owner, contact details and similar. "automatic" means the CRM did it by itself (for example an unsubscribe or bounce found in the mailbox). A field changed again since is left alone.</div></div>
  <div class="panel"><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><h3 style="margin:0">Restore and compare</h3><span style="flex:1"></span>
      <button id="chgnow" ${snaps.length ? '' : 'disabled'} title="Compares the live CRM with the backup from about a day ago">What changed since yesterday</button></div>
    <div class="hint" style="margin:6px 0 10px">Every restore saves your current data first, so it can be undone. To rebuild on a new Mac, use the steps at the bottom.</div>
    ${snaps.length ? `<table><thead><tr><th>Backup</th><th>Size</th><th></th></tr></thead><tbody>${snaps.slice(0, 25).map(s => `<tr><td>${esc(s.label)}</td><td>${Math.max(1, Math.round(s.size / 1024))} KB</td><td style="text-align:right;white-space:nowrap"><button class="ghost rcmp" data-f="${esc(s.file)}">Compare with now</button> <button class="ghost rone" data-f="${esc(s.file)}">Bring back one partner</button> <button class="rall" data-f="${esc(s.file)}">Restore everything</button></td></tr>`).join('')}</tbody></table>${snaps.length > 25 ? `<div class="hint">Showing the newest 25 of ${snaps.length}.</div>` : ''}` : '<div class="mut">No backups on this Mac yet. Click “Back up on this Mac now” above.</div>'}</div>
  <div class="panel"><h3>Protect your settings</h3>
    <div class="hint" style="margin-bottom:10px">Your Gmail app password, Slack webhook and Google backup key are not in normal backups. Turn this on to keep an <b>encrypted</b> copy of them with every backup (on this Mac and in Drive), so a restore on a new Mac needs no re-typing. It opens only with your passphrase, which is never stored anywhere: <b>if you forget it, the copy can't be opened</b>, so keep it in your password manager.</div>
    <div class="fields"><span>Status</span><div>${d.vault ? '<i class="dot st5"></i>On: settings are backed up encrypted' : '<i class="dot st2"></i>Off: after a restore you would re-enter passwords by hand'}</div>
      <span>${d.vault ? 'New passphrase' : 'Passphrase'}</span><input id="vpass" type="password" autocomplete="new-password" placeholder="At least 10 characters"></div>
    <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap"><button id="vset" class="primary">${d.vault ? 'Change passphrase' : 'Turn on'}</button>${d.vault ? '<button id="vtest" title="Type your passphrase above, then click, to confirm it opens the copy">Test my passphrase</button><button id="voff" class="ghost" style="margin-left:auto">Turn off</button>' : ''}</div></div>
  <div class="panel"><h3>Alerts</h3><div class="hint" style="margin-bottom:10px">When a backup fails, backups resume after a gap, or the monthly restore test finishes, you get an email (and Slack if connected in Settings).</div>
    <div class="fields"><span>Email to</span><input id="aemail" value="${esc(d.alert_email)}" placeholder="${esc(d.alert_default || 'your email')}"></div>
    <div style="margin-top:10px"><button id="asave">Save</button></div></div>
  <div class="panel"><h3>Recent backup runs</h3>${d.runs.length ? `<table><thead><tr><th>When</th><th>What</th><th>Result</th></tr></thead><tbody>${d.runs.map(r => `<tr><td>${esc(r.ts.replace('T', ' ').slice(0, 16))}</td><td>${{local: 'This Mac', drive: 'Google Drive', sheets: 'Google Sheet', drill: 'Restore test', restore: 'Restore'}[r.kind] || esc(r.kind)}</td><td style="white-space:normal"><i class="dot ${r.ok ? 'st5' : 'st6'}"></i>${esc(r.detail)}</td></tr>`).join('')}</tbody></table>` : '<div class="mut">No runs yet.</div>'}</div>
  <div class="panel"><h3>If this Mac fails: how to restore</h3><ol style="padding-left:20px;line-height:1.8;margin:0">
    <li>On the new Mac, open your Google Drive backup folder in the browser, right-click it and download it. Unzip it.</li>
    <li>Unzip <code>code/partner-crm-code-…zip</code> into your home folder. The app needs only Python 3 (already on a Mac).</li>
    <li>In Terminal: <code>python3 ~/partner-crm/restore.py --backup ~/Downloads/&lt;the folder you unzipped&gt;</code></li>
    <li>Start it with <code>cd ~/partner-crm &amp;&amp; python3 server.py</code> and open http://127.0.0.1:8765. Re-enter the Gmail app password and other keys in Settings.</li></ol>
    <div class="hint" style="margin-top:8px">The same steps are saved as README.md inside the Drive folder. Until the new Mac is ready you can keep working from the Google Sheet.</div></div>`;
  const poll = async () => {
    for (;;) {
      const j = await api('/api/gdrive/job');
      $('#bprog').innerHTML = j.state === 'running' ? '<div class="note">Backing up… this can take a minute or two.</div>' : '';
      if (j.state !== 'running') { if (j.state === 'error') toast(j.error); else if (j.result) { const bad = Object.entries(j.result).filter(([, v]) => v && v.ok === false); toast(bad.length ? 'Some parts failed: ' + bad.map(([k, v]) => k + ': ' + (v.error || (v.problems || []).join('; '))).join('; ') : (j.result.drill ? 'Restore test passed: your Drive backup rebuilds correctly' : 'Backup complete')); } return backups(); }
      await new Promise(r => setTimeout(r, 1500));
    }
  };
  const bn = $('#bnow'); if (bn) bn.onclick = async () => { bn.disabled = true; try { await api('/api/gdrive/run', {body: {parts: ['drive', 'sheets']}}); poll(); } catch (e) { toast(e.message); bn.disabled = false; } };
  if (d.job.state === 'running') poll();
  const gc = $('#gcopy'); if (gc) gc.onclick = async () => { try { await navigator.clipboard.writeText((await api('/api/gdrive/script')).script); toast('Script copied. Paste it into the new Apps Script project.'); } catch { toast('Could not copy. Allow clipboard access and try again.'); } };
  const cn = $('#gconn'); if (cn) cn.onclick = async () => { cn.disabled = true; try { await api('/api/gdrive/config', {body: {url: $('#gurl').value.trim(), enabled: true}}); await api('/api/gdrive/test', {body: {}}); toast('Connected. Running the first backup…'); await api('/api/gdrive/run', {body: {parts: ['drive', 'sheets']}}); backups(); } catch (e) { toast(e.message); await api('/api/gdrive/config', {body: {url: ''}}).catch(() => {}); cn.disabled = false; } };
  const gt = $('#gtest'); if (gt) gt.onclick = async () => { try { await api('/api/gdrive/test', {body: {}}); toast('Connected to Google'); } catch (e) { toast(e.message); } };
  const gv = $('#gverify'); if (gv) gv.onclick = async () => { gv.disabled = true; gv.textContent = 'Checking…'; try { const r = await api('/api/gdrive/verify', {body: {}}); toast(r.ok ? `Drive copy verified (${r.checked} items checked)` : 'Problems found: ' + r.problems.slice(0, 2).join('; ')); } catch (e) { toast(e.message); } backups(); };
  const gs = $('#gsave'); if (gs) gs.onclick = async () => { try { await api('/api/gdrive/config', {body: {enabled: $('#gen').checked, include_documents: $('#gdoc').checked, include_code: $('#gcode').checked, sheet_sync_hours: $('#ghrs').value, keep_days: $('#gkeep').value}}); toast('Saved'); backups(); } catch (e) { toast(e.message); } };
  const gd = $('#gdisc'); if (gd) gd.onclick = async () => { if (confirm('Disconnect Google? Backups already in your Drive stay there.')) { await api('/api/gdrive/config', {body: {url: '', enabled: false}}); backups(); } };
  const gdr = $('#gdrill'); if (gdr) gdr.onclick = async () => { gdr.disabled = true; gdr.textContent = 'Testing…'; try { await api('/api/gdrive/run', {body: {parts: ['drill']}}); poll(); } catch (e) { toast(e.message); gdr.disabled = false; } };
  wireUndo($('#chgl'), backups);
  $$('.rall').forEach(b => b.onclick = () => restoreDialog(b.dataset.f, snaps.find(x => x.file === b.dataset.f).label));
  $$('.rone').forEach(b => b.onclick = () => restorePartnerDialog(b.dataset.f, snaps.find(x => x.file === b.dataset.f).label));
  $$('.rcmp').forEach(b => b.onclick = () => compareDialog(snaps, b.dataset.f, 'live'));
  const cn2 = $('#chgnow'); if (cn2) cn2.onclick = () => { const old = snaps.find(x => (Date.now() - new Date(x.at)) / 3600000 >= 12) || snaps[snaps.length - 1]; compareDialog(snaps, old.file, 'live'); };
  $('#vset').onclick = async () => { try { await api('/api/backup/vault', {body: {passphrase: $('#vpass').value}}); toast('Encrypted settings backup is on. Write the passphrase down somewhere safe.'); backups(); } catch (e) { toast(e.message); } };
  const vt = $('#vtest'); if (vt) vt.onclick = async () => { try { await api('/api/backup/vault', {body: {action: 'check', passphrase: $('#vpass').value}}); toast('That passphrase opens your encrypted copy.'); } catch (e) { toast(e.message); } };
  const vo = $('#voff'); if (vo) vo.onclick = async () => { if (confirm('Stop backing up your settings? Copies already in Drive stay there until they are replaced.')) { await api('/api/backup/vault', {body: {action: 'remove'}}); backups(); } };
  $('#asave').onclick = async () => { try { await api('/api/backup/config', {body: {alert_email: $('#aemail').value}}); toast('Saved'); } catch (e) { toast(e.message); } };
  $('#bksave').onclick = async () => { try { await api('/api/backup/config', {body: {folder: $('#bkf').value, keep_days: $('#bkd').value, enabled: $('#bke').checked}}); toast('Saved'); backups(); } catch (e) { toast(e.message); } };
  $('#bklocal').onclick = async () => { $('#bklocal').disabled = true; const r = await api('/api/backup/run', {body: {}}); toast(r.ok ? 'Backup done' : 'Backup failed: ' + r.error); backups(); };
}

/* ---------------- onboarding ---------------- */
const OB_STATUS = ['Collecting', 'In review', 'On hold', 'Rejected', 'Onboarded'];
const ITEM_ST = {pending: 'Pending', received: 'Received', verified: 'Verified', rejected: 'Rejected', na: 'N/A'};
const fsize = n => n < 1024 ? n + ' B' : n < 1048576 ? Math.round(n / 1024) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
const obchip = s => `<span class="chip ob-${esc(s.replace(' ', ''))}">${esc(s)}</span>`;

async function onboarding(arg) {
  if (arg) return onboardingDetail(+arg);
  const {rows, counts} = await api('/api/onboarding');
  const f = prefs.obf || '';
  const shown = rows.filter(r => !f || r.status === f);
  view.innerHTML = `<div class="top"><div><h1>Onboarding</h1><div class="sub" style="margin:0">Due diligence, signed agreement and AppsFlyer link, per distributor</div></div><span class="sp"></span><button id="obstd">Standard commercials</button><button class="primary" id="obnew">Start onboarding</button></div>
  <div class="seg" style="margin-bottom:14px"><button data-f="" class="${f ? '' : 'on'}">All ${rows.length}</button>${OB_STATUS.map(s => `<button data-f="${s}" class="${f === s ? 'on' : ''}">${s} ${counts[s]}</button>`).join('')}</div>
  ${shown.length ? `<table><thead><tr><th>Partner</th><th>Type</th><th>Status</th><th>Documents</th><th>Commercials</th><th>Agreement</th><th>AppsFlyer link</th><th>Started</th></tr></thead><tbody>
  ${shown.map(r => `<tr class="link" data-id="${r.partner_id}"><td>${esc(r.name)}<br><small class="mut">${r.region === 'Foreign' ? `<span class="region Foreign">${esc(r.country)}</span> ` : ''}${esc(r.city || '')}</small></td><td>${esc(r.entity_type)}</td><td>${obchip(r.status)}</td>
    <td><div class="bar" style="width:90px;display:inline-block;vertical-align:middle"><i style="width:${r.progress.total ? r.progress.done / r.progress.total * 100 : 0}%;background:var(--s5)"></i></div> <small>${r.progress.done}/${r.progress.total}</small></td>
    <td><span class="chip cm-${esc(r.commercials)}">${esc(r.commercials)}</span></td><td>${r.has_agreement ? '✓' : '<span class="mut">—</span>'}</td><td>${r.has_link ? '✓' : '<span class="mut">—</span>'}</td><td><small>${fdate(r.started_at)}</small></td></tr>`).join('')}</tbody></table>`
    : `<div class="mut">${rows.length ? 'Nothing with this status.' : 'No one in onboarding yet. Click “Start onboarding” and pick a partner, or open a partner and start it from there.'}</div>`}`;
  $$('.seg button').forEach(b => b.onclick = () => { prefs.obf = b.dataset.f; savePrefs(); onboarding(); });
  $$('tr.link').forEach(tr => tr.onclick = () => location.hash = '#/onboarding/' + tr.dataset.id);
  $('#obstd').onclick = standardCardDialog;
  $('#obnew').onclick = () => {
    const m = openModal(`<h2>Start onboarding</h2><input id="obq" placeholder="Search partners by name, email or contact" style="width:100%"><div class="mut" style="margin:6px 0">Furthest along first. Starting moves earlier-stage partners to In discussion.</div><div id="oblist"></div>`);
    const load = async () => {
      const {rows} = await api('/api/onboarding/candidates?q=' + encodeURIComponent($('#obq').value));
      $('#oblist').innerHTML = rows.length ? rows.map(r => `<div class="row"><div class="grow">${esc(r.name)}<br><small>${sdot(r.stage)}${esc(r.stage)} · ${esc([r.segment, r.city].filter(Boolean).join(' · '))}</small></div><button data-p="${r.id}">Start</button></div>`).join('') : '<div class="mut">No matches.</div>';
      $$('#oblist button', m).forEach(b => b.onclick = async () => { await api('/api/onboarding/start', {body: {partner_id: +b.dataset.p}}); location.hash = '#/onboarding/' + b.dataset.p; });
    };
    let t; $('#obq').oninput = () => { clearTimeout(t); t = setTimeout(load, 200); }; load(); $('#obq').focus();
  };
}

async function onboardingDetail(id) {
  const d = await api('/api/onboarding/' + id).catch(() => null);
  if (!d) { view.innerHTML = '<a class="crumb" href="#/onboarding">← Onboarding</a><p class="mut">Onboarding hasn\'t been started for this partner.</p>'; return; }
  const {onboarding: o, partner: p, items, blockers, progress: pg, link_check: lc} = d;
  const done = o.status === 'Onboarded';
  const patch = async (b, msg) => { try { await api('/api/onboarding/' + id, {method: 'PATCH', body: b}); toast(msg || 'Saved'); } catch (e) { toast(e.message); } };
  const refresh = () => onboardingDetail(id);
  view.innerHTML = `<a class="crumb" href="#/onboarding">← Onboarding</a>
  <div class="ph"><div><h1>${esc(p.name)}</h1><div class="sub" style="margin:0"><a href="#/partner/${id}">Partner page</a> · ${esc([o.entity_type, p.city, p.contact_person].filter(Boolean).join(' · '))} · <span class="region ${d.region}">${d.region === 'Foreign' ? esc(d.country) : 'Indian'}</span></div></div><span style="flex:1"></span>
    ${obchip(o.status)}
    ${done ? '<button id="reopen">Reopen</button>' : `<select id="obstatus">${['Collecting', 'In review', 'On hold', 'Rejected'].map(s => `<option ${s === o.status ? 'selected' : ''}>${s}</option>`).join('')}</select><button class="primary" id="complete">Mark onboarded</button>`}</div>
  ${done ? `<div class="note">Onboarded on ${esc((o.onboarded_at || '').slice(0, 10))}. Documents are locked; Reopen to change them.</div>`
    : blockers.length ? `<div class="note warn"><b>To onboard:</b><ul style="margin:4px 0 0;padding-left:18px">${blockers.map(b => `<li>${esc(b)}</li>`).join('')}</ul></div>` : '<div class="note">All checks done. Ready to mark onboarded.</div>'}
  <div class="pg"><div>
    <h3>Details</h3><div class="fields">
      <span>Type</span><select data-k="entity_type" ${done ? 'disabled' : ''}>${['Individual', 'Corporate', 'Bank'].map(s => `<option ${s === o.entity_type ? 'selected' : ''}>${s}</option>`).join('')}</select>
      <span>Reviewer</span><input data-k="reviewer" value="${esc(o.reviewer)}" placeholder="Who verified">
      <span>Agreement date</span><input type="date" data-k="agreement_date" value="${esc(o.agreement_date)}" ${done ? 'disabled' : ''}>
      <span>Notes</span><textarea data-k="notes" rows="3" placeholder="Internal notes">${esc(o.notes)}</textarea></div>
    <div class="info"><h3>Distributor code</h3>${d.distributor_code ? `<b>${esc(d.distributor_code)}</b>` : '<div class="mut">Assigned automatically when you mark this partner onboarded.</div>'}</div>
    <div class="info"><h3>AppsFlyer link</h3>
      <input id="aflink" style="width:100%" value="${esc(o.appsflyer_link)}" placeholder="https://app.getbelong.com/a0PJ/Name  or  https://…onelink.me/…?pid=…&c=…">
      <div style="display:flex;gap:6px;margin-top:6px"><button id="afsave">Save link</button>${o.appsflyer_link ? '<button id="afcopy">Copy</button>' : ''}${!o.appsflyer_link && d.generated_link ? '<button id="afgen">Use generated link</button>' : ''}</div>
      ${lc ? (lc.trackable ? `<div class="note" style="margin-top:8px">Trackable: installs through this link count for code <b>${esc(lc.code)}</b>.</div>` : `<div class="note warn" style="margin-top:8px">${lc.issues.map(esc).join('<br>')}</div>`)
        : '<div class="mut" style="margin-top:6px">Optional: you can onboard without it and add it any time, even after onboarding. Paste the partner\'s link; its c= value becomes their referral code so installs are counted on Performance.</div>'}</div>
    <div class="info"><h3>History</h3><div class="tl">${d.log.map(a => `<div class="ev"><div class="h">${esc(a.ts.replace('T', ' ').slice(0, 16))}</div><div class="b">${esc(a.body)}</div></div>`).join('')}</div></div>
  </div><div>
    <div class="card-panel" id="commbox"></div>
    <h3>Checklist · ${pg.done}/${pg.total} required verified</h3>
    ${items.map(it => `<div class="ck-item st-${it.status}" data-key="${esc(it.key)}">
      <div class="ckh"><div class="grow"><b>${esc(it.label)}</b> ${it.required ? '' : '<small class="mut">optional</small>'}</div>
        <select class="its" ${done ? 'disabled' : ''}>${Object.entries(ITEM_ST).map(([k, v]) => `<option value="${k}" ${k === it.status ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
      ${it.files.map(f => `<div class="fl"><a href="/api/onboarding/files/${f.id}?inline=1" target="_blank" rel="noopener">${esc(f.orig_name)}</a><small class="mut">${fsize(f.size)} · ${fdate(f.uploaded_at)}</small><a href="/api/onboarding/files/${f.id}" download>Download</a>${done ? '' : `<button class="ghost rmf" data-f="${f.id}">Remove</button>`}</div>`).join('')}
      ${done ? '' : `<div class="drop"><label class="upl">Upload<input type="file" multiple hidden></label><span class="mut">or drop files here</span><input class="itn" placeholder="Note" value="${esc(it.note)}">
        ${it.key.startsWith('x_') ? '<button class="ghost rmi">Delete item</button>' : `<label class="ck"><input type="checkbox" class="itr" ${it.required ? 'checked' : ''} ${it.key === 'agreement' ? 'disabled' : ''}> required</label>`}</div>`}</div>`).join('')}
    ${done ? '' : '<div style="margin-top:12px"><input id="newitem" placeholder="Add a document to this checklist" style="width:260px"> <button id="additem">Add</button></div>'}
  </div></div>`;
  const others = (await api('/api/onboarding/partners-with-commercials')).rows.filter(x => x.id !== id);
  commEditor($('#commbox'), d.commercials, d.commercial_options, {locked: done, partners: others,
    save: b => api(`/api/onboarding/${id}/commercials`, {method: 'PUT', body: b}),
    status: (s, by) => api(`/api/onboarding/${id}/commercials/status`, {body: {status: s, by}}),
    copyFrom: src => api(`/api/onboarding/${id}/commercials/copy`, {body: {from: src}}),
    done: () => { refresh(); refreshSide(); }});
  dirtyBar($('.pg .fields'), '[data-k]', async body => { await api('/api/onboarding/' + id, {method: 'PATCH', body}); toast('Saved'); refresh(); });
  const os = $('#obstatus'); if (os) os.onchange = async () => { await patch({status: os.value}, 'Status updated'); refresh(); };
  const cm = $('#complete'); if (cm) cm.onclick = async () => { try { await api(`/api/onboarding/${id}/complete`, {body: {}}); toast('Onboarded'); refresh(); refreshSide(); } catch (e) { toast(e.message); } };
  const ro = $('#reopen'); if (ro) ro.onclick = async () => { const r = prompt('Why reopen? (optional)'); if (r === null) return; await api(`/api/onboarding/${id}/reopen`, {body: {reason: r}}); refresh(); refreshSide(); };
  const sv = $('#afsave'); if (sv) sv.onclick = async () => { try { await api('/api/onboarding/' + id, {method: 'PATCH', body: {appsflyer_link: $('#aflink').value}}); toast('Link saved'); refresh(); } catch (e) { toast(e.message); } };
  const cp = $('#afcopy'); if (cp) cp.onclick = async () => { try { await navigator.clipboard.writeText(o.appsflyer_link); toast('Copied'); } catch { $('#aflink').select(); } };
  const gen = $('#afgen'); if (gen) gen.onclick = () => { $('#aflink').value = d.generated_link; $('#afsave').click(); };
  const ai = $('#additem'); if (ai) ai.onclick = async () => { const label = $('#newitem').value.trim(); if (!label) return; await api(`/api/onboarding/${id}/items`, {body: {label}}); refresh(); };
  $$('.ck-item').forEach(row => {
    const key = row.dataset.key, ip = (b, m) => api(`/api/onboarding/${id}/items/${encodeURIComponent(key)}`, {method: 'PATCH', body: b}).then(() => { if (m) toast(m); }, e => { toast(e.message); refresh(); throw e; });
    row.querySelector('.its')?.addEventListener('change', async e => { try { await ip({status: e.target.value}, 'Saved'); refresh(); refreshSide(); } catch {} });
    row.querySelector('.itn')?.addEventListener('change', e => ip({note: e.target.value}, 'Saved').catch(() => {}));
    row.querySelector('.itr')?.addEventListener('change', async e => { try { await ip({required: e.target.checked}); refresh(); } catch {} });
    row.querySelector('.rmi')?.addEventListener('click', async () => { try { await api(`/api/onboarding/${id}/items/${encodeURIComponent(key)}`, {method: 'DELETE'}); refresh(); } catch (e) { toast(e.message); } });
    $$('.rmf', row).forEach(b => b.onclick = async () => { if (!confirm('Remove this file?')) return; try { await api('/api/onboarding/files/' + b.dataset.f, {method: 'DELETE'}); refresh(); } catch (e) { toast(e.message); } });
    const up = async files => {
      let ok = 0;
      for (const f of files) {
        try {
          const r = await fetch(`/api/onboarding/${id}/files?item=${encodeURIComponent(key)}&name=${encodeURIComponent(f.name)}`, {method: 'POST', headers: hdrs(), body: f});
          const j = await r.json(); if (!r.ok) { staleCheck(j); throw new Error(j.error); } ok++;
        } catch (e) { toast(`${f.name}: ${e.message}`); }
      }
      if (ok) { toast(ok + ' uploaded'); refresh(); }
    };
    const fi = row.querySelector('input[type=file]'); if (fi) fi.onchange = () => up([...fi.files]);
    row.ondragover = e => { if (fi) { e.preventDefault(); row.classList.add('over'); } };
    row.ondragleave = () => row.classList.remove('over');
    row.ondrop = e => { if (!fi) return; e.preventDefault(); row.classList.remove('over'); up([...e.dataTransfer.files]); };
  });
}

/* ---------------- commercials editor (per partner, and the standard rate card) ---------------- */
function commEditor(host, com, opt, o) {
  // com: {status, terms_text, currency, ...}; opt: {frequencies, currencies}; o: {standard, locked, save(body), status(s, by), copyFrom(src), partners[], done()}
  const draw = () => {
    host.innerHTML = `${o.standard ? '' : `<div class="cm-head"><b>Commercials</b><span class="chip cm-${esc(com.status)}">${esc(com.status)}</span>${com.based_on ? `<small class="mut">started from ${com.based_on === 'standard' ? 'the standard text' : 'another partner'}</small>` : ''}<span style="flex:1"></span>
      ${o.locked ? '' : `<select id="cm_copy"><option value="">Copy commercials from…</option><option value="standard">Standard text</option>${(o.partners || []).map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select>`}</div>`}
    ${com.status === 'Agreed' && !o.standard ? `<div class="note ok">Agreed${com.agreed_by ? ' (confirmed by ' + esc(com.agreed_by) + ')' : ''} on ${esc((com.agreed_on || '').slice(0, 10))}. Changing anything below sends it back to Draft so it gets re-confirmed.</div>` : ''}
    <textarea id="cm_text" rows="9" style="width:100%;font:13.5px/1.5 inherit" placeholder="Type the commercials exactly as agreed with this partner. For example:&#10;• GIFT City mutual funds: 30% of Belong's revenue, paid monthly&#10;• GIFT Nifty futures: ₹500 per funded client&#10;• AIFs: 0.4% trail on AUM per year, first 24 months" ${o.locked ? 'disabled' : ''}>${esc(com.terms_text || '')}</textarea>
    <div class="fields" style="margin-top:14px"><span>Currency</span><select id="cm_cur" ${o.locked ? 'disabled' : ''}>${[...new Set([...opt.currencies, com.currency || 'INR'])].map(x => `<option ${x === (com.currency || 'INR') ? 'selected' : ''}>${x}</option>`).join('')}</select>
      ${o.standard ? '' : `<span>Effective from</span><input id="cm_from" type="date" value="${esc(com.effective_from || '')}" ${o.locked ? 'disabled' : ''}><span>Valid until</span><input id="cm_until" type="date" value="${esc(com.valid_until || '')}" ${o.locked ? 'disabled' : ''}>`}
      <span>Payout</span><select id="cm_freq" ${o.locked ? 'disabled' : ''}><option value="">Choose…</option>${opt.frequencies.map(x => `<option ${x === com.payout_frequency ? 'selected' : ''}>${x}</option>`).join('')}</select>
      <span>Paid within</span><span style="display:flex;gap:6px;align-items:center"><input id="cm_days" type="number" min="0" value="${com.payment_days ?? ''}" style="width:90px" ${o.locked ? 'disabled' : ''}> days of period end</span>
      <span>Minimum payout</span><input id="cm_min" type="number" min="0" step="any" value="${com.min_payout ?? ''}" placeholder="None" ${o.locked ? 'disabled' : ''}>
      <span>Tax / TDS</span><input id="cm_tax" value="${esc(com.tax_note || '')}" placeholder="e.g. TDS as applicable; partner provides invoice" ${o.locked ? 'disabled' : ''}>
      <span>Other terms</span><textarea id="cm_notes" rows="2" placeholder="Lock-in, clawbacks, exclusivity, anything else agreed" ${o.locked ? 'disabled' : ''}>${esc(com.notes || '')}</textarea></div>
    ${o.locked ? '' : `<div class="cm-actions"><button id="cm_save" class="primary">${o.standard ? 'Save standard text' : 'Save commercials'}</button>
      ${o.standard ? '' : `${com.status !== 'Proposed' && com.status !== 'Agreed' ? '<button id="cm_prop">Mark as proposed</button>' : ''}${com.status !== 'Agreed' ? '<button id="cm_agree">Mark as agreed…</button>' : ''}
      <span id="cm_by" hidden><input id="cm_who" placeholder="Who confirmed this with the partner?" style="width:240px"> <button id="cm_ok" class="primary">Confirm agreed</button></span>`}</div>`}`;
    if (o.locked) return;
    const read = () => ({terms_text: $('#cm_text').value, currency: $('#cm_cur').value, payout_frequency: $('#cm_freq').value, payment_days: $('#cm_days').value, min_payout: $('#cm_min').value, tax_note: $('#cm_tax').value, notes: $('#cm_notes').value,
      ...(o.standard ? {} : {effective_from: $('#cm_from').value, valid_until: $('#cm_until').value})});
    const save = async msg => { try { await o.save(read()); if (msg) toast(msg); return true; } catch (e) { toast(e.message); return false; } };
    $('#cm_save').onclick = async () => { if (await save('Commercials saved')) o.done && o.done(); };
    const cp = $('#cm_copy'); if (cp) cp.onchange = async () => { if (!cp.value) return; if ($('#cm_text').value.trim() && !confirm('Replace what you have typed with the copied commercials?')) { cp.value = ''; return; } try { await o.copyFrom(cp.value); toast('Copied. Adjust it for this partner.'); o.done && o.done(); } catch (e) { toast(e.message); cp.value = ''; } };
    const pr = $('#cm_prop'); if (pr) pr.onclick = async () => { if (!await save()) return; try { await o.status('Proposed'); toast('Marked as proposed'); o.done && o.done(); } catch (e) { toast(e.message); } };
    const ag = $('#cm_agree'); if (ag) ag.onclick = () => { $('#cm_by').hidden = false; $('#cm_who').focus(); };
    const ok = $('#cm_ok'); if (ok) ok.onclick = async () => { if (!await save()) return; try { await o.status('Agreed', $('#cm_who').value); toast('Commercials agreed'); o.done && o.done(); } catch (e) { toast(e.message); } };
  };
  draw();
}

async function standardCardDialog() {
  const s = await api('/api/onboarding/standard');
  const m = openModal(`<h2>Standard commercials</h2><div class="sub" style="margin-bottom:12px">Optional starting text for new onboardings. Each partner's copy is then edited on their own page, so changing this never alters terms already set for a partner.</div><div id="stdbox"></div><div class="acts"><button id="stdclose" class="ghost">Close</button></div>`);
  $('#stdclose').onclick = closeModal;
  commEditor($('#stdbox', m), s, s.options, {standard: true, save: b => api('/api/onboarding/standard', {method: 'PUT', body: b}), done: () => closeModal()});
}

/* ---------------- add partner ---------------- */
async function addPartnerDialog() {
  const {types, countries} = await api('/api/partners/types');
  const team = (await api('/api/team')).rows;
  const sources = ['Referral', 'Event / conference', 'LinkedIn', 'Inbound enquiry', 'Cold outreach', 'Existing relationship', 'Other'];
  const m = openModal(`<h2>Add a partner</h2><div class="sub" style="margin-bottom:14px">Only the name is required. You can fill in the rest later.</div>
    <div class="form2" id="apf">
      <label class="w2"><span>Firm or person name <span class="req">*</span></span><input id="a_name" placeholder="e.g. Acme Wealth LLP"></label>
      <label>Type<select id="a_category">${types.map(t => `<option ${t === 'Other' ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></label>
      <label>Segment<select id="a_segment"><option>Corporate</option><option selected>Individual</option><option>Bank</option></select></label>
      <label>Contact person<input id="a_contact_person"></label><label>Designation<input id="a_designation"></label>
      <label>Email<input id="a_email" type="email"></label><label>Phone / WhatsApp<input id="a_phone"></label>
      <label>Country<input id="a_country" list="a_ctys" value="India"><datalist id="a_ctys">${countries.map(c => `<option value="${esc(c)}">`).join('')}</datalist></label><label>City<input id="a_city"></label><label>Website<input id="a_website" placeholder="https://"></label>
      <label>How did you find them?<input id="a_source" list="a_srcs" placeholder="Referral, event, LinkedIn…"><datalist id="a_srcs">${sources.map(s => `<option value="${esc(s)}">`).join('')}</datalist></label>
      <label>Owner<select id="a_owner"><option value="">Unassigned</option>${team.map(m => `<option ${m.name === actor() ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}</select></label>
      <label class="w2">Notes<textarea id="a_remarks" rows="2" placeholder="Anything worth remembering"></textarea></label></div>
    <div id="a_dups"></div>
    <div class="acts"><button id="a_cancel" class="ghost">Cancel</button><button id="a_add" class="primary">Add partner</button></div>`);
  $('#a_name').focus();
  $('#a_cancel').onclick = closeModal;
  const go = async force => {
    const body = {force};
    ['name', 'category', 'segment', 'country', 'contact_person', 'designation', 'email', 'phone', 'city', 'website', 'source', 'owner', 'remarks'].forEach(k => body[k] = $('#a_' + k).value);
    try {
      const r = await api('/api/partners', {body});
      if (r.duplicates) {
        $('#a_dups').innerHTML = `<div class="note warn"><b>This looks like a partner you already have:</b>${r.duplicates.map(x => `<div class="row" style="border:0;padding:4px 0"><a class="grow" href="#/partner/${x.id}">${esc(x.name)}</a><small>${esc(x.stage)} · ${esc(x.why)}</small></div>`).join('')}
          <div style="margin-top:6px"><button id="a_force">Add as a new partner anyway</button></div></div>`;
        $('#a_force').onclick = () => go(true);
        return;
      }
      toast('Partner added'); closeModal(); location.hash = '#/partner/' + r.id; refreshSide();
    } catch (e) { toast(e.message); }
  };
  $('#a_add').onclick = () => go(false);
  m.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.tagName === 'INPUT') go(false); });
}

route();

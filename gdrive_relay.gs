/**
 * Belong Partner CRM: Google Drive + Sheets backup relay.
 *
 * What it does (all inside YOUR Google account):
 *   - receives backup files from the CRM and stores them in a Drive folder
 *   - mirrors CRM tables into a Google Sheet and builds a Dashboard tab that keeps working when your Mac is off
 *   - emails you if no backup has arrived for 36 hours
 *
 * Setup (once):
 *   1. script.google.com -> New project -> delete the sample code -> paste this whole file -> Save.
 *   2. Choose the function "setup" in the toolbar and click Run. Approve the permissions (Drive, Sheets, email).
 *      Open View -> Logs to see the links to the Drive folder and the Sheet it created.
 *   3. Deploy -> New deployment -> type "Web app" -> Execute as: Me -> Who has access: Anyone -> Deploy.
 *   4. Copy the Web app URL and paste it into the CRM (Backups page). Click Test connection.
 *
 * The TOKEN below is a long random secret shared only with your CRM. Anyone without it is rejected.
 */
const TOKEN = '__TOKEN__';
const ROOT_NAME = 'Belong Partner CRM Backup';
const SHEET_NAME = 'Belong Partner CRM: Mirror and Dashboard';
const STALE_HOURS = 36;
const MAX_FILE_BYTES = 16 * 1024 * 1024;

function props_() { return PropertiesService.getScriptProperties(); }

function root_() {
  const id = props_().getProperty('ROOT_ID');
  if (id) { try { const f = DriveApp.getFolderById(id); if (!f.isTrashed()) return f; } catch (e) {} }
  const f = DriveApp.createFolder(ROOT_NAME);
  props_().setProperty('ROOT_ID', f.getId());
  return f;
}

function sub_(parent, name) {
  const it = parent.getFoldersByName(name);
  return it.hasNext() ? it.next() : parent.createFolder(name);
}

// "documents/12 - Acme/PAN/pan.pdf" -> [folder, filename], creating folders on the way
function resolvePath_(path) {
  const parts = String(path).split('/').filter(function (p) { return p; });
  let folder = root_();
  for (let i = 0; i < parts.length - 1; i++) folder = sub_(folder, parts[i].substring(0, 120));
  return [folder, parts[parts.length - 1].substring(0, 200)];
}

function sheet_() {
  const id = props_().getProperty('SHEET_ID');
  if (id) { try { return SpreadsheetApp.openById(id); } catch (e) {} }
  const ss = SpreadsheetApp.create(SHEET_NAME);
  props_().setProperty('SHEET_ID', ss.getId());
  DriveApp.getFileById(ss.getId()).moveTo(root_());
  return ss;
}

function setup() {
  const folder = root_();
  const ss = sheet_();
  installTriggers_();
  Logger.log('Drive folder: ' + folder.getUrl());
  Logger.log('Google Sheet:  ' + ss.getUrl());
  Logger.log('Now deploy as a Web app (Deploy > New deployment) and paste the URL into the CRM.');
}

function installTriggers_() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('dailyCheck').timeBased().everyDays(1).atHour(8).create();
}

function out_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function hex_(bytes) {
  return bytes.map(function (b) { return ('0' + (b & 0xff).toString(16)).slice(-2); }).join('');
}

function readJson_(name, fallback) {
  const it = root_().getFilesByName(name);
  if (!it.hasNext()) return fallback;
  try { return JSON.parse(it.next().getBlob().getDataAsString()); } catch (e) { return fallback; }
}

function writeJson_(name, obj) {
  const it = root_().getFilesByName(name);
  const text = JSON.stringify(obj);
  if (it.hasNext()) { const f = it.next(); f.setContent(text); return f; }
  return root_().createFile(name, text, 'application/json');
}

function doGet(e) {
  if (!e || !e.parameter || e.parameter.token !== TOKEN) return out_({ ok: false, error: 'bad token' });
  return out_(info_());
}

function info_() {
  const ss = sheet_();
  return { ok: true, root_url: root_().getUrl(), sheet_url: ss.getUrl(), status: readJson_('status.json', {}), now: new Date().toISOString() };
}

function doPost(e) {
  try {
    const b = JSON.parse(e.postData.contents);
    if (b.token !== TOKEN) return out_({ ok: false, error: 'bad token' });
    switch (b.action) {
      case 'ping': return out_(info_());
      case 'begin_upload': return out_(beginUpload_(b));
      case 'upload_chunk': return out_(uploadChunk_(b));
      case 'finish_upload': return out_(finishUpload_(b));
      case 'get_manifest': return out_({ ok: true, manifest: readJson_('manifest.json', {}) });
      case 'put_manifest': writeJson_('manifest.json', b.manifest); return out_({ ok: true });
      case 'put_status': writeJson_('status.json', b.status); return out_({ ok: true });
      case 'put_text': return out_(putText_(b));
      case 'sheet_write': return out_(sheetWrite_(b));
      case 'dashboard': return out_(buildDashboard_(b));
      case 'prune': return out_(prune_(b));
      case 'verify': return out_(verify_(b));
      case 'trash_prefix': return out_(trashPrefix_(b));
      case 'latest': return out_(latest_(b));
      case 'download': return out_(download_(b));
      default: return out_({ ok: false, error: 'unknown action' });
    }
  } catch (err) {
    return out_({ ok: false, error: String(err) });
  }
}

// ---------- chunked file upload ----------
function beginUpload_(b) {
  const tmpRoot = sub_(root_(), '_uploads');
  const id = Utilities.getUuid();
  tmpRoot.createFolder(id);
  return { ok: true, id: id };
}

function tmpFolder_(id) {
  const it = sub_(root_(), '_uploads').getFoldersByName(String(id));
  if (!it.hasNext()) throw new Error('unknown upload');
  return it.next();
}

function uploadChunk_(b) {
  const f = tmpFolder_(b.id);
  const name = ('000000' + b.i).slice(-6);
  const bytes = Utilities.base64Decode(b.data);
  f.createFile(Utilities.newBlob(bytes, 'application/octet-stream', name));
  return { ok: true, size: bytes.length };
}

function finishUpload_(b) {
  const tmp = tmpFolder_(b.id);
  const files = [];
  const it = tmp.getFiles();
  while (it.hasNext()) files.push(it.next());
  files.sort(function (x, y) { return x.getName() < y.getName() ? -1 : 1; });
  if (files.length !== b.total) throw new Error('expected ' + b.total + ' chunks, got ' + files.length);
  let all = [];
  files.forEach(function (f) { all = all.concat(f.getBlob().getBytes()); });
  if (all.length > MAX_FILE_BYTES) throw new Error('file too large for the relay');
  if (all.length !== b.size) throw new Error('size mismatch: ' + all.length + ' vs ' + b.size);
  const md5 = hex_(Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, all));
  if (md5 !== b.md5) throw new Error('checksum mismatch');
  const pf = resolvePath_(b.path);
  const existing = pf[0].getFilesByName(pf[1]);
  while (existing.hasNext()) existing.next().setTrashed(true);
  const created = pf[0].createFile(Utilities.newBlob(all, b.mime || 'application/octet-stream', pf[1]));
  tmp.setTrashed(true);
  return { ok: true, id: created.getId(), size: all.length, md5: md5 };
}

function putText_(b) {
  const pf = resolvePath_(b.path);
  const existing = pf[0].getFilesByName(pf[1]);
  while (existing.hasNext()) existing.next().setTrashed(true);
  pf[0].createFile(pf[1], String(b.text || ''), b.mime || 'text/plain');
  return { ok: true };
}

function prune_(b) {
  const cutoff = new Date().getTime() - Number(b.keep_days || 30) * 86400000;
  let n = 0;
  (b.folders || ['snapshots']).forEach(function (name) {
    const it = sub_(root_(), name).getFiles();
    while (it.hasNext()) {
      const f = it.next();
      if (f.getDateCreated().getTime() < cutoff) { f.setTrashed(true); n++; }
    }
  });
  // abandoned partial uploads
  const tmp = sub_(root_(), '_uploads').getFolders();
  while (tmp.hasNext()) { const t = tmp.next(); if (t.getDateCreated().getTime() < new Date().getTime() - 86400000) t.setTrashed(true); }
  return { ok: true, pruned: n };
}

// Look up a file by its path without creating anything
function findFile_(path) {
  const parts = String(path).split('/').filter(function (p) { return p; });
  let folder = root_();
  for (let i = 0; i < parts.length - 1; i++) {
    const it = folder.getFoldersByName(parts[i].substring(0, 120));
    if (!it.hasNext()) return null;
    folder = it.next();
  }
  const fit = folder.getFilesByName(parts[parts.length - 1].substring(0, 200));
  return fit.hasNext() ? fit.next() : null;
}

// Does Drive really hold what the CRM thinks it uploaded? Checks presence + size of each file and the checksum of the newest snapshot.
function verify_(b) {
  const missing = [], wrong = [];
  (b.files || []).forEach(function (f) {
    const file = findFile_(f.path);
    if (!file) missing.push(f.path);
    else if (file.getSize() !== f.size) wrong.push(f.path);
  });
  let snap = null;
  if (b.snapshot) {
    const file = findFile_(b.snapshot.path);
    if (!file) snap = { path: b.snapshot.path, found: false, ok: false };
    else {
      const md5 = hex_(Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, file.getBlob().getBytes()));
      snap = { path: b.snapshot.path, found: true, md5: md5, ok: md5 === b.snapshot.md5 };
    }
  }
  return { ok: true, missing: missing, wrong: wrong, snapshot: snap, checked: (b.files || []).length };
}

// Newest file in a folder (used by the monthly restore test to find the latest database copy)
function latest_(b) {
  const it = sub_(root_(), String(b.folder)).getFiles();
  let best = null;
  while (it.hasNext()) { const f = it.next(); if (!best || f.getDateCreated().getTime() > best.getDateCreated().getTime()) best = f; }
  if (!best) return { ok: true, found: false };
  return { ok: true, found: true, name: best.getName(), size: best.getSize(), created: best.getDateCreated().toISOString() };
}

// Read one slice of a file (base64) so the CRM can download it in pieces
function download_(b) {
  const file = findFile_(b.path);
  if (!file) return { ok: false, error: 'not found: ' + b.path };
  const bytes = file.getBlob().getBytes();
  const off = Number(b.offset || 0), len = Number(b.length || 1500000);
  const slice = bytes.slice(off, off + len);
  return { ok: true, size: bytes.length, offset: off, data: Utilities.base64Encode(slice) };
}

// Remove every item in <parent> whose name starts with <prefix> (used when a partner is permanently deleted)
function trashPrefix_(b) {
  let n = 0;
  const parent = sub_(root_(), String(b.parent));
  const prefix = String(b.prefix);
  const fi = parent.getFolders();
  while (fi.hasNext()) { const f = fi.next(); if (f.getName().indexOf(prefix) === 0) { f.setTrashed(true); n++; } }
  const it = parent.getFiles();
  while (it.hasNext()) { const f = it.next(); if (f.getName().indexOf(prefix) === 0) { f.setTrashed(true); n++; } }
  return { ok: true, trashed: n };
}

// ---------- Sheet mirror ----------
function sheetWrite_(b) {
  const ss = sheet_();
  let sh = ss.getSheetByName(b.tab);
  if (!sh) sh = ss.insertSheet(b.tab);
  const cols = b.header.length;
  if (b.first) {
    sh.clearContents();
    if (sh.getMaxColumns() < cols) sh.insertColumnsAfter(sh.getMaxColumns(), cols - sh.getMaxColumns());
    sh.getRange(1, 1, 1, cols).setNumberFormat('@').setValues([b.header]).setFontWeight('bold').setBackground('#e9eefc');
    sh.setFrozenRows(1);
  }
  const rows = b.rows || [];
  if (rows.length) {
    const start = Math.max(sh.getLastRow(), 1) + 1;
    if (sh.getMaxRows() < start + rows.length) sh.insertRowsAfter(sh.getMaxRows(), start + rows.length - sh.getMaxRows() + 50);
    // plain text format so nothing typed by a partner can run as a formula
    const range = sh.getRange(start, 1, rows.length, cols);
    range.setNumberFormat('@');
    range.setValues(rows);
  }
  return { ok: true, rows: rows.length };
}

function colLetter_(n) {
  let s = '';
  while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); }
  return s;
}

function col_(ss, tab, header) {
  const sh = ss.getSheetByName(tab);
  if (!sh) return null;
  const hs = sh.getRange(1, 1, 1, Math.max(sh.getLastColumn(), 1)).getValues()[0];
  const i = hs.indexOf(header);
  return i < 0 ? null : tab.indexOf(' ') >= 0 ? "'" + tab + "'!" + colLetter_(i + 1) + ':' + colLetter_(i + 1) : tab + '!' + colLetter_(i + 1) + ':' + colLetter_(i + 1);
}

function buildDashboard_(b) {
  const ss = sheet_();
  let d = ss.getSheetByName('Dashboard');
  if (d) ss.deleteSheet(d);
  d = ss.insertSheet('Dashboard', 0);
  const blank = ss.getSheetByName('Sheet1');          // the empty default tab Google adds to a new spreadsheet
  if (blank && ss.getSheets().length > 1) ss.deleteSheet(blank);
  d.setHiddenGridlines(true);
  const stage = col_(ss, 'Partners', 'Stage'), arch = col_(ss, 'Partners', 'Archived'), region = col_(ss, 'Partners', 'Region');
  const seg = col_(ss, 'Partners', 'Segment'), country = col_(ss, 'Partners', 'Country');
  const obStatus = col_(ss, 'Onboarding', 'Status'), cmStatus = col_(ss, 'Onboarding', 'Commercials');
  const taskDone = col_(ss, 'Tasks', 'Done'), taskDue = col_(ss, 'Tasks', 'Due');
  const emStatus = col_(ss, 'Emails', 'Status');
  const live = '(' + arch + '<>"Yes")';
  d.getRange('A1').setValue('Belong Partner CRM: dashboard').setFontSize(18).setFontWeight('bold');
  d.getRange('A2').setFormula('="Data as of "&Meta!B1&"  ·  mirror of the CRM, refreshed automatically"').setFontColor('#5d6585');
  if (ss.getSheetByName('Backup health')) {
    d.getRange('A3').setFormula('="Backups: "&IFERROR(INDEX(\'Backup health\'!B:B,2),"n/a")&"  ·  last restore test: "&IFERROR(INDEX(\'Backup health\'!B:B,6),"n/a")').setFontColor('#5d6585');
  }

  // KPIs
  const kp = [
    ['Partners (active)', '=COUNTIFS(' + stage + ',"<>",' + arch + ',"<>Yes")-1'],
    ['Contacted', '=COUNTIFS(' + stage + ',"Contacted",' + arch + ',"<>Yes")'],
    ['Replied or further', '=COUNTIFS(' + stage + ',"Replied",' + arch + ',"<>Yes")+COUNTIFS(' + stage + ',"Applied",' + arch + ',"<>Yes")+COUNTIFS(' + stage + ',"In discussion",' + arch + ',"<>Yes")+COUNTIFS(' + stage + ',"Onboarded",' + arch + ',"<>Yes")'],
    ['Onboarded', '=COUNTIFS(' + stage + ',"Onboarded",' + arch + ',"<>Yes")'],
    ['Emails sent', '=COUNTIF(' + emStatus + ',"sent")'],
    ['Open tasks', '=COUNTIF(' + taskDone + ',"No")']
  ];
  kp.forEach(function (k, i) {
    const col = 1 + i * 2;
    d.getRange(4, col).setValue(k[0]).setFontColor('#5d6585').setFontSize(10);
    d.getRange(5, col).setFormula(k[1]).setFontSize(22).setFontWeight('bold').setFontColor('#2447c9');
  });

  // Stage funnel
  const stages = ['New', 'Contacted', 'Replied', 'Applied', 'In discussion', 'Onboarded', 'Not interested'];
  d.getRange('A8').setValue('Pipeline by stage').setFontWeight('bold');
  stages.forEach(function (s, i) {
    d.getRange(9 + i, 1).setValue(s);
    d.getRange(9 + i, 2).setFormula('=COUNTIFS(' + stage + ',A' + (9 + i) + ',' + arch + ',"<>Yes")');
  });
  d.getRange('D8').setValue('By region').setFontWeight('bold');
  ['Indian', 'Foreign'].forEach(function (r, i) {
    d.getRange(9 + i, 4).setValue(r);
    d.getRange(9 + i, 5).setFormula('=COUNTIFS(' + region + ',D' + (9 + i) + ',' + arch + ',"<>Yes")');
  });
  d.getRange('D12').setValue('By segment').setFontWeight('bold');
  ['Corporate', 'Individual', 'Bank'].forEach(function (r, i) {
    d.getRange(13 + i, 4).setValue(r);
    d.getRange(13 + i, 5).setFormula('=COUNTIFS(' + seg + ',D' + (13 + i) + ',' + arch + ',"<>Yes")');
  });
  d.getRange('G8').setValue('Onboarding').setFontWeight('bold');
  ['Collecting', 'In review', 'On hold', 'Rejected', 'Onboarded'].forEach(function (r, i) {
    d.getRange(9 + i, 7).setValue(r);
    d.getRange(9 + i, 8).setFormula('=COUNTIF(' + obStatus + ',G' + (9 + i) + ')');
  });
  d.getRange('J8').setValue('Commercials').setFontWeight('bold');
  ['Draft', 'Proposed', 'Agreed'].forEach(function (r, i) {
    d.getRange(9 + i, 10).setValue(r);
    d.getRange(9 + i, 11).setFormula('=COUNTIF(' + cmStatus + ',J' + (9 + i) + ')');
  });

  // Charts
  d.insertChart(d.newChart().setChartType(Charts.ChartType.BAR).addRange(d.getRange('A9:B15')).setPosition(18, 1, 0, 0)
    .setOption('title', 'Pipeline by stage').setOption('legend', { position: 'none' }).setOption('colors', ['#2447c9']).setOption('width', 520).setOption('height', 300).build());
  d.insertChart(d.newChart().setChartType(Charts.ChartType.PIE).addRange(d.getRange('D9:E10')).setPosition(18, 6, 0, 0)
    .setOption('title', 'Indian vs foreign partners').setOption('colors', ['#2447c9', '#7a55e0']).setOption('width', 380).setOption('height', 300).build());

  // Tasks due soonest + recent activity (live formulas on the mirrored tabs)
  d.getRange('A40').setValue('Open tasks, soonest first').setFontWeight('bold');
  d.getRange('A41').setFormula('=IFERROR(QUERY(Tasks!A:F,"select A,B,C,D,E where F = \'No\' order by D limit 15",1),"No open tasks")');
  d.getRange('A58').setValue('Latest activity').setFontWeight('bold');
  d.getRange('A59').setFormula('=IFERROR(QUERY(Activities!A:F,"select A,B,C,D,E order by B desc limit 15",1),"No activity yet")');
  if (ss.getSheetByName('Backup log')) {
    d.getRange('A76').setValue('Recent backups').setFontWeight('bold');
    d.getRange('A77').setFormula('=IFERROR(QUERY(\'Backup log\'!A:E,"select A,B,C,E,D order by A desc limit 12",1),"No backups yet")');
  }
  d.setColumnWidths(1, 12, 120);
  ss.setActiveSheet(d);
  return { ok: true };
}

// ---------- daily health check ----------
function dailyCheck() {
  const st = readJson_('status.json', null);
  const owner = Session.getEffectiveUser().getEmail();
  let stale = true, last = 'never';
  if (st && st.last_backup_utc) {
    last = st.last_backup_utc;
    stale = (new Date().getTime() - new Date(st.last_backup_utc).getTime()) > STALE_HOURS * 3600 * 1000;
  }
  if (stale) {
    MailApp.sendEmail(owner, 'Belong Partner CRM: backup has stopped arriving',
      'The CRM has not sent a backup to Google Drive for over ' + STALE_HOURS + ' hours (last: ' + last + ').\n\n' +
      'If your Mac is off or broken, your data is safe in this Drive folder:\n' + root_().getUrl() + '\n\n' +
      'The Sheet mirror and Dashboard are here:\n' + sheet_().getUrl() + '\n\nRestore steps are in README.md in the Drive folder.');
  }
}

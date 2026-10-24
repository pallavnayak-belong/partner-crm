/**
 * Youform -> Partner CRM relay  (Google Apps Script web app)
 *
 * WHY: Youform only sends webhooks to a public HTTPS address; the CRM runs on your Mac.
 * This script catches each submission, stores it in a sheet tab called "Inbox", and hands
 * it to the CRM the next time the CRM pulls. Nothing is deleted, so no application is lost
 * if your Mac is off.
 *
 * ONE-TIME SETUP
 *  1. Go to script.google.com -> New project. Paste this whole file over the default code.
 *  2. Project Settings (gear icon) -> Script properties -> Add property:
 *       TOKEN = a long random string (e.g. 40 random characters). Keep it secret.
 *  3. Deploy -> New deployment -> type "Web app".
 *       Execute as: Me      Who has access: Anyone
 *     Click Deploy and authorise when asked. Copy the "Web app URL" (ends in /exec).
 *  4. In Youform: your form -> Integrate -> Webhook -> connect, and set the URL to:
 *       <Web app URL>?token=<TOKEN>
 *  5. In the CRM: Settings -> Youform applications -> paste the Web app URL (without ?token)
 *     and the TOKEN, Save, then "Pull now".
 *
 * NOTES
 *  - Apps Script answers POSTs with a redirect; Youform may show that as a non-200 or retry.
 *    That is harmless: the row is stored first, and duplicates are ignored (by submission_id here,
 *    and again in the CRM).
 *  - Anyone who has the URL AND the token can post or read, so don't share either.
 *  - After editing this script you must Deploy -> Manage deployments -> edit -> New version.
 */
var SHEET_NAME = 'Inbox';   // columns: received_at | payload_json | synced | submission_id

function _sheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet() || SpreadsheetApp.create('Youform Relay Inbox');
  var sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) {
    sh = ss.insertSheet(SHEET_NAME);
    sh.appendRow(['received_at', 'payload_json', 'synced', 'submission_id']);
    sh.setFrozenRows(1);
  }
  return sh;
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function _authorised(e) {
  var token = PropertiesService.getScriptProperties().getProperty('TOKEN');
  return !!token && e && e.parameter && e.parameter.token === token;
}

function doPost(e) {
  if (!_authorised(e)) return _json({ ok: false, error: 'unauthorized' });
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    var body = (e.postData && e.postData.contents) || '';
    var payload;
    try { payload = JSON.parse(body); } catch (err) { return _json({ ok: false, error: 'bad json' }); }
    var sid = String(payload.submission_id || payload.event_id || '');
    var sh = _sheet();
    if (sid && sh.getLastRow() > 1) {
      var found = sh.getRange(2, 4, sh.getLastRow() - 1, 1).createTextFinder(sid).matchEntireCell(true).findNext();
      if (found) return _json({ ok: true, duplicate: true });
    }
    sh.appendRow([new Date().toISOString(), body, '', sid]);
    return _json({ ok: true });
  } finally {
    lock.releaseLock();
  }
}

function doGet(e) {
  if (!_authorised(e)) return _json({ ok: false, error: 'unauthorized' });
  var action = e.parameter.action;
  var sh = _sheet();
  if (action === 'pull') {
    var rows = [];
    var last = sh.getLastRow();
    if (last > 1) {
      var data = sh.getRange(2, 1, last - 1, 3).getValues();
      for (var i = 0; i < data.length && rows.length < 200; i++) {
        if (String(data[i][2]).toUpperCase() === 'TRUE') continue;
        try { rows.push({ id: i + 2, payload: JSON.parse(data[i][1]) }); } catch (err) { /* skip malformed */ }
      }
    }
    return _json({ ok: true, rows: rows });
  }
  if (action === 'ack') {
    var ids = String(e.parameter.ids || '').split(',');
    ids.forEach(function (id) {
      var n = parseInt(id, 10);
      if (n >= 2 && n <= sh.getLastRow()) sh.getRange(n, 3).setValue('TRUE');
    });
    return _json({ ok: true });
  }
  return _json({ ok: false, error: 'unknown action' });
}

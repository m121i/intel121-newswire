/**
 * Circuit newswire pinger.
 *
 * This is the one piece of the JI Newswire worth copying exactly: its scheduling. A
 * time-driven Apps Script trigger fires on Google's servers, reliably, indefinitely.
 * GitHub's own cron is throttled on public repos — measured firing every two to six
 * hours instead of every fifteen minutes — which is why the newswire's cadence kept
 * failing until this existed.
 *
 * It does nothing else: no fetching, no scoring, no posting. It tells the GitHub
 * workflow to run, and that workflow does the work. So none of the Apps Script
 * constraints that made the JI Newswire hard to debug (logs visible only in this editor,
 * the 9KB property ceiling, the runtime limit) apply to anything that matters here.
 *
 * SETUP — one click:
 *   Select `setup` in the function dropdown above and press Run. Approve the permission
 *   prompt. That installs four triggers — the newswire every 15 minutes, and the briefing
 *   at 6am, midday and 6pm Eastern — and pings the newswire once immediately.
 *
 * To stop either one: delete its trigger (clock icon, left sidebar). pingNewswire drives
 * the newswire; briefMorning / briefMidday / briefEvening drive the briefing.
 */

const REPO = 'm121i/intel121-newswire';
const WORKFLOW = 'poll.yml';        // the newswire
const EVERY_MINUTES = 15;

/**
 * The token lives in Script Properties, NOT here. This file is committed to a public
 * repository, and GitHub's push protection correctly refused a version with the literal
 * token in it.
 *
 * Set it once: Project Settings -> Script Properties -> add GH_TOKEN, a fine-grained
 * GitHub token with Actions read+write on this repo only. Its only power is starting
 * these two workflows; it cannot read code or secrets.
 */
const FALLBACK_TOKEN = '';

function token_() {
  const stored = PropertiesService.getScriptProperties().getProperty('GH_TOKEN');
  return (stored || FALLBACK_TOKEN).trim();  // trim: a pasted newline reads as a revoked token
}

/** Tell the workflow to run. Throws on anything but 204, so failures surface. */
function pingNewswire() {
  const url = 'https://api.github.com/repos/' + REPO + '/actions/workflows/' + WORKFLOW + '/dispatches';
  const response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token_(),
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify({ ref: 'main' }),
    muteHttpExceptions: true,
  });

  const code = response.getResponseCode();
  // Never fire-and-forget. A failed dispatch must turn the execution red, which is what
  // sends Google's failure email to whoever owns the trigger.
  if (code !== 204) {
    throw new Error('Dispatch failed: HTTP ' + code + ' ' + response.getContentText().slice(0, 300));
  }
  console.log('dispatched 204');
}

/**
 * The three-times-daily briefing: five stories an editor should not miss, in its own
 * channel. Separate workflow, separate webhook, separate channel from the newswire.
 *
 * Three named functions rather than one, because Apps Script's daily trigger takes an
 * hour, not a list of hours. atHour() follows this project's timezone
 * (America/New_York), so these track US daylight saving without intervention.
 */
function dispatch_(workflow) {
  const url = 'https://api.github.com/repos/' + REPO + '/actions/workflows/' + workflow + '/dispatches';
  const response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token_(),
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify({ ref: 'main' }),
    muteHttpExceptions: true,
  });
  const code = response.getResponseCode();
  if (code !== 204) {
    throw new Error(workflow + ' dispatch failed: HTTP ' + code + ' ' + response.getContentText().slice(0, 300));
  }
  console.log(workflow + ' dispatched 204');
}


/** Run this once. Installs the 15-minute trigger and pings the newswire immediately. */
function setup() {
  const handlers = ['pingNewswire'];
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (handlers.indexOf(t.getHandlerFunction()) !== -1) {
      ScriptApp.deleteTrigger(t);   // idempotent: re-running setup never stacks triggers
    }
  });

  ScriptApp.newTrigger('pingNewswire').timeBased().everyMinutes(EVERY_MINUTES).create();


  console.log('Trigger installed: newswire every ' + EVERY_MINUTES + ' minutes.');
  pingNewswire();
}

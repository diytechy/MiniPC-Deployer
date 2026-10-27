// OtterTube landing page (stack/ottertube/landing; SR-125, LLR-1350). Reads the
// published manifest, /ottertube/update.json, and offers the exe it names.
//
// Every value taken from the manifest reaches the page through textContent or
// as a validated attribute (the link's href is built only from a file name that
// matches the exe route's own pattern): never innerHTML or any other markup
// sink. The signature is NOT checked here (a browser has no key); the installed
// app checks every update it takes, and this page only points at the file.
//
// Under node (tests/test_ottertube_updates.py) the functions are exported and
// nothing runs.
'use strict';

const EXE_RE = /^OtterTube-[0-9a-f]{7}\.exe$/;          // the Caddy exe route's pattern
const VERSION_RE = /^\d{1,6}\.\d{1,6}\.\d{1,6}$/;
const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$/;

/** What to show for the manifest's answer: { kind: 'release' | 'none' | 'error', ... }. Pure. */
function describeRelease(status, text) {
  if (status === 404) return { kind: 'none', message: 'No release published yet.' };
  if (status !== 200) return { kind: 'error', message: 'The hub did not answer with a release (HTTP ' + String(status) + ').' };
  let m;
  try { m = JSON.parse(text); } catch (e) { return { kind: 'error', message: "The hub's release file could not be read." }; }
  const ok = m !== null && typeof m === 'object' && !Array.isArray(m)
    && m.schema === 1 && m.product === 'OtterTube'
    && typeof m.version === 'string' && VERSION_RE.test(m.version)
    && typeof m.commit === 'string' && /^[0-9a-f]{40}$/.test(m.commit)
    && typeof m.dirty === 'boolean'
    && typeof m.builtAt === 'string' && ISO_RE.test(m.builtAt) && Number.isFinite(Date.parse(m.builtAt))
    && typeof m.file === 'string' && EXE_RE.test(m.file)
    && Number.isSafeInteger(m.size) && m.size >= 1
    && typeof m.sha256 === 'string' && /^[0-9a-f]{64}$/.test(m.sha256);
  if (!ok) return { kind: 'error', message: "The hub's release file is not one OtterTube would read." };
  return {
    kind: 'release',
    href: '/ottertube/' + m.file,
    file: m.file,
    version: m.version,
    built: m.builtAt.slice(0, 10),
    commit: m.commit.slice(0, 7),
    sizeMB: Math.max(1, Math.round(m.size / 1048576)),
    sha256: m.sha256,
  };
}

/** Put a description on the page: text only. */
function render(doc, r) {
  const status = doc.getElementById('status');
  const link = doc.getElementById('download');
  const details = doc.getElementById('details');
  if (r.kind !== 'release') {
    status.textContent = r.message;
    link.hidden = true;
    details.hidden = true;
    return;
  }
  status.textContent = 'OtterTube ' + r.version + ', built ' + r.built + '.';
  link.setAttribute('href', r.href);
  link.setAttribute('download', r.file);
  link.textContent = 'Download OtterTube ' + r.version;
  link.hidden = false;
  details.textContent = r.file + ', about ' + r.sizeMB + ' MB (commit ' + r.commit + '). SHA-256 ' + r.sha256 + '.';
  details.hidden = false;
}

/** Ask the hub, then render. fetchFn: the browser's fetch (or a test's). */
async function load(doc, fetchFn) {
  let r;
  try {
    const res = await fetchFn('/ottertube/update.json', { cache: 'no-store', credentials: 'omit', redirect: 'error' });
    r = describeRelease(res.status, res.status === 200 ? await res.text() : '');
  } catch (e) {
    r = { kind: 'error', message: 'The hub could not be reached.' };
  }
  render(doc, r);
  return r;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { describeRelease, render, load, EXE_RE };
} else if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => { load(document, window.fetch.bind(window)); });
}

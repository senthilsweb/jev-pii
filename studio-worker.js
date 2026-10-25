// Runs Pyodide + DuckDB + the jev_pii package off the main thread, so a slow Jev call never freezes the page.
// Protocol: main -> worker {id, cmd, ...args};  worker -> main {id, ok, result|error}  or  {id, progress} or {status}.
import { loadPyodide } from 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs';

let py;
const status = (s) => postMessage({ status: s });
globalThis.reportProgress = (id, done, total, col) => postMessage({ id, progress: { done, total, col } });

const BOOT = `
import json, time, duckdb, js
from jev_pii import MockJev, JevClient, register, pii_questions
from jev_pii.catalog import discover, build_silver

con = duckdb.connect()
_c = {}
INTERNAL = ('column_samples', 'column_catalog')   # scratch tables the discover step uses; hidden from the tree

def set_client(mode, key, url, secret, model, redact, max_calls=0):
    kw = {'api_url': url, 'proxy_secret': secret or None, 'cache_dir': None, 'timeout': 60}
    if model:
        kw['model'] = model
    c = MockJev() if mode == 'mock' else JevClient(key or None, **kw)
    _c['budget'] = register(con, c, redact=bool(redact), max_calls=int(max_calls) or None)
    _c['c'] = c
    return json.dumps({'client': c.name, 'model': getattr(c, 'model', '')})

def run_sql(sql):
    t = time.time()
    _c['budget'].reset()          # the row-level call cap applies per query
    try:
        cur = con.execute(sql)
        if cur.description is None:
            return json.dumps({'ok': True, 'cols': [], 'rows': [], 'ms': round((time.time() - t) * 1000)})
        rows = cur.fetchmany(1001)
        return json.dumps({'ok': True, 'cols': [d[0] for d in cur.description], 'rows': rows[:1000], 'more': len(rows) > 1000,
                           'ms': round((time.time() - t) * 1000)}, default=str)
    except Exception as e:
        return json.dumps({'ok': False, 'error': str(e)})

def tree():
    rows = con.execute("""SELECT t.table_name, t.table_type, c.column_name, c.data_type
        FROM information_schema.tables t JOIN information_schema.columns c USING (table_schema, table_name)
        WHERE t.table_schema = 'main' ORDER BY t.table_name, c.ordinal_position""").fetchall()
    return json.dumps([r for r in rows if r[0] not in INTERNAL])

def discover_table(table, n, rid):
    c = _c['c']
    before = getattr(c, 'calls', 0)
    cols = discover(con, table, int(n), progress=lambda d, t, col: js.reportProgress(rid, d, t, col))
    return json.dumps({'columns': cols, 'calls': getattr(c, 'calls', 0) - before, 'client': c.name})

def mask_table(table):
    have = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'pii_catalog'").fetchone()[0]
    if not have or not con.execute("SELECT count(*) FROM pii_catalog WHERE table_name = ?", [table]).fetchone()[0]:
        raise ValueError('Discover PII on this table first.')
    build_silver(con, [table], catalog='pii_catalog')
    return 'silver_' + table

def drop_object(name):
    kind = con.execute("SELECT table_type FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()
    con.execute(('DROP VIEW "%s"' if kind and kind[0] == 'VIEW' else 'DROP TABLE "%s"') % name)

def test_client():
    c = _c['c']
    try:
        a = c.ask({'table': 't', 'column': 'ping', 'sample_values': ['1']}, pii_questions())
        return json.dumps({'ok': True, 'client': c.name, 'detail': 'answered: ' + a['category']['choice']})
    except Exception as e:
        return json.dumps({'ok': False, 'error': str(e)})
`;

async function init() {
  status('loading Python…');
  py = await loadPyodide({ stdout: () => {}, stderr: (s) => console.warn(s) });
  status('loading DuckDB (about 20 MB, cached after the first visit)…');
  await py.loadPackage(['duckdb', 'numpy', 'pyyaml', 'pyodide-http']);
  await py.runPythonAsync('import pyodide_http; pyodide_http.patch_all()');   // urllib -> synchronous XHR (allowed in a worker)
  py.FS.mkdir('jev_pii');
  py.FS.mkdir('/data');
  for (const f of ['__init__', 'client', 'questions', 'udf', 'catalog', 'score']) {
    const r = await fetch(new URL(`jev_pii/${f}.py`, import.meta.url));
    if (!r.ok) throw new Error(`jev_pii/${f}.py: HTTP ${r.status}`);
    py.FS.writeFile(`jev_pii/${f}.py`, await r.text());
  }
  await py.runPythonAsync(BOOT);
  py.globals.get('set_client')('mock', '', '', '', '', false, 100);
}

const call = (name, ...args) => py.globals.get(name)(...args);

const handlers = {
  init: async () => { await init(); return 'ready'; },
  set_client: (a) => call('set_client', a.mode, a.key || '', a.url || '', a.secret || '', a.model || '', !!a.redact, a.maxCalls || 0),
  write_file: (a) => { py.FS.writeFile(a.path, a.bytes); return 'ok'; },
  run_sql: (a) => call('run_sql', a.sql),
  tree: () => call('tree'),
  discover: (a, id) => call('discover_table', a.table, a.n, id),
  mask: (a) => call('mask_table', a.table),
  drop: (a) => { call('drop_object', a.name); return 'ok'; },
  test_client: () => call('test_client'),
};

self.onmessage = async (e) => {
  const { id, cmd, ...args } = e.data;
  try {
    postMessage({ id, ok: true, result: await handlers[cmd](args, id) });
  } catch (err) {
    postMessage({ id, ok: false, error: String(err.message || err).split('\n').filter(Boolean).slice(-3).join('\n') });
  }
};

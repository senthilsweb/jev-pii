# Proposal: retire-keyless-jev-key — stop serving callers with the owner's Jev key

> Status: **PROPOSED** (2026-10-25). Owner: @senthilsweb.
> Checkpoint: **2026-11-01** (1 week). Hard stop: **2026-11-08** (2 weeks), unless explicitly extended below.
> Builds on: the `jev-proxy` optional keyless mode (`OPEN_SERVER_KEY`, added 2026-10-25).

## Why

`jev-proxy-senthil.fly.dev` was switched to **keyless mode** on 2026-10-25 so the studio at
`www.senthilsweb.com/jev-pii/` and curl/Postman could work with no credentials. In that mode **anyone who can reach
the proxy spends the owner's TypeSafe key**. It was a deliberate, temporary choice for a demo, bounded by:

- 20 calls per minute per client and 1,000 calls per UTC day (`OPEN_RATE_PER_MIN`, `OPEN_DAILY_MAX`);
- both limits are **per instance and in memory**, so a restart or scale-to-zero resets the daily count;
- `ALLOWED_ORIGINS` only restricts browsers; curl and Postman are not restricted by it.

A temporary exposure should have an end date. This change sets it.

## What changes

**Phase A — checkpoint, 2026-11-01.** Decide: retire on schedule, extend once to 2026-11-08, or retire now.
Evidence to look at first:
- `fly logs --app jev-proxy-senthil` for `open mode` lines and 429s (`rate limit for keyless use`, `daily budget`);
- TypeSafe usage for the key over the period (anything not from the owner's own tests is a signal to retire now);
- whether anyone other than the owner has used the studio.

**Phase B — retire, no later than 2026-11-08.**
1. `OPEN_SERVER_KEY=0` in `proxy/.env`, then `./deploy-fly.sh jev-proxy-senthil` (the script sends the `0`, so it
   really switches the mode off). Fastest path in an emergency: `fly secrets unset OPEN_SERVER_KEY --app jev-proxy-senthil`.
2. `fly secrets unset TYPESAFE_API_KEY --app jev-proxy-senthil`. The proxy then holds no key at all and is keyless in
   the original sense: every caller sends their own `Authorization` header. `PROXY_SECRET` has nothing left to guard
   and can be unset too.
3. Studio (`index.html`): Live mode requires the user's own key again; restore the validation and drop the
   "leave empty to use the proxy's own key" hint. The default endpoint stays (the proxy still works with a caller's key).
4. Docs: README and `proxy/README.md` say the keyless mode is retired; `.env.example` defaults stay `OPEN_SERVER_KEY=0`.

**Out of scope.** The keyless code stays in the proxy, off by default, for anyone who deploys their own. It is not removed.

## Risks and the one to settle first

- **The key is shared.** The same TypeSafe key is used by `zyn-bank-etl` (the bank-statements pipeline) and by local
  runs of this repo. **Revoking it at TypeSafe would break those.** Retiring the proxy's use of it needs only steps 1 and 2
  above (unset on Fly); do not revoke the key unless it is being rotated everywhere.
  Cleanest fix, ideally *now*: create a **dedicated key for the proxy** in the TypeSafe console, put it on Fly
  (`TYPESAFE_API_KEY` in `proxy/.env`, rerun the deploy script), and give it a low spend cap. Then usage is separable and
  revoking it later touches nothing else.
- **Studio visitors get a 401 after retirement** unless they bring a key. That is the intended behaviour; the page's
  Settings dialog already explains it.
- **Rollback:** put the key back in `proxy/.env`, set `OPEN_SERVER_KEY=1`, rerun the deploy script (about a minute).

## Acceptance criteria

1. `curl -X POST https://jev-proxy-senthil.fly.dev/v1/systemone -d '{}'` with no credentials returns **401**.
2. The same call with `Authorization: Bearer <a valid key>` returns **200**.
3. `fly secrets list --app jev-proxy-senthil` shows no `TYPESAFE_API_KEY` and no `OPEN_SERVER_KEY=1`.
4. The studio in Live mode with no key shows a clear "needs a key" message.
5. README and `proxy/README.md` updated; this change archived to `openspec/changes/archive/`.

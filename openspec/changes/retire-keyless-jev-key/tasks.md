# Tasks — retire-keyless-jev-key

Owner prerequisites (not code):
- [ ] Approve this proposal (`PROPOSED` → `APPROVED`)
- [ ] **Calendar reminders**: 2026-11-01 (checkpoint) and 2026-11-08 (hard stop)
- [ ] Create a **dedicated TypeSafe key for the proxy** with a low spend cap (recommended now; see Risks)
- [ ] Put it on Fly: `TYPESAFE_API_KEY=<new key>` in `proxy/.env`, rerun `./deploy-fly.sh jev-proxy-senthil`

Phase A — checkpoint (2026-11-01):
- [ ] Read `fly logs --app jev-proxy-senthil` for keyless usage and 429s
- [ ] Check TypeSafe usage for the proxy's key
- [ ] Decide: retire on schedule / extend once to 2026-11-08 / retire now. Record the decision at the bottom of this file

Phase B — retire (no later than 2026-11-08):
- [ ] `OPEN_SERVER_KEY=0` in `proxy/.env`; `./deploy-fly.sh jev-proxy-senthil` (emergency: `fly secrets unset OPEN_SERVER_KEY --app jev-proxy-senthil`)
- [ ] `fly secrets unset TYPESAFE_API_KEY --app jev-proxy-senthil` (and `PROXY_SECRET`)
- [ ] Studio: Live mode requires the user's own key again (`index.html`: restore the validation, drop the "leave empty" hint)
- [ ] README and `proxy/README.md`: keyless mode retired
- [ ] Verify the five acceptance criteria in `proposal.md`
- [ ] Do **not** revoke a key shared with `zyn-bank-etl` unless rotating it everywhere
- [ ] Commit + push; move this change to `openspec/changes/archive/`

Decision log:
- 2026-10-25: keyless mode enabled for the demo (20/min per client, 1,000/day). Retirement planned for 2026-11-01 to 2026-11-08.

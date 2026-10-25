# Spec delta: jev-proxy — keyless mode is retired for the owner's deployment

## MODIFIED Requirements

### Requirement: A caller with no credentials is served with the server-side key only when keyless mode is on

Keyless mode (`OPEN_SERVER_KEY=1`) SHALL remain available in the code and SHALL default to off. For the owner's
deployment (`jev-proxy-senthil`) it SHALL be switched off, and the server-side `TYPESAFE_API_KEY` removed, no later
than 2026-11-08.

#### Scenario: After retirement, no credentials
- **WHEN** a request reaches `/v1/systemone` with no `Authorization` and no `X-Proxy-Secret`
- **THEN** the proxy answers **401** and makes no upstream call

#### Scenario: After retirement, the caller's own key
- **WHEN** a request carries `Authorization: Bearer <valid key>`
- **THEN** the proxy forwards it and returns the upstream answer

## ADDED Requirements

### Requirement: Keyless use is bounded while it is on

While keyless mode is on, callers with no credentials SHALL be limited to `OPEN_RATE_PER_MIN` requests per minute per
client and `OPEN_DAILY_MAX` requests per UTC day in total, and exceeding either SHALL return **429**. A caller's own key
and a correct `X-Proxy-Secret` SHALL NOT count against those limits.

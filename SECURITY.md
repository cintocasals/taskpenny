# Security

## Reporting a problem

Please report security problems privately through GitHub's "Report a vulnerability" button on this repository
(Security tab), not in a public issue. We aim to answer within a few days.

## What Taskpenny does with your keys

- Keys are read from environment variables only. Taskpenny never writes them to disk, to run files or to logs.
- `taskpenny serve` listens on `127.0.0.1` by default. If you bind it to other interfaces (as the Docker image does
  inside its container), set `TASKPENNY_API_KEY`. With a key, everything except the page itself needs it: API clients
  send it as a Bearer token and the page asks for it once and keeps a session cookie (HttpOnly, SameSite=Strict,
  Secure behind https). Without a key, anyone who can reach the server can read your runs and spend your credit.
- POST requests must be JSON, so another website cannot start runs through your browser, and every run's budget
  is capped by `TASKPENNY_MAX_COST` (2 USD by default).
- Saved runs (`runs/*.json`) contain your requests and the answers. Treat that folder like the data you send.
- Requests go to the providers you configured (Vercel AI Gateway and/or the providers' own APIs, or Ollama on
  your machine). Their data policies apply.

## Supported versions

Only the latest release gets fixes while Taskpenny is below 1.0.

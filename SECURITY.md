# Security

## Reporting a problem

Please report security problems privately through GitHub's "Report a vulnerability" button on this repository
(Security tab), not in a public issue. We aim to answer within a few days.

## What Taskpenny does with your keys

- Provider keys are read from environment variables only. Taskpenny never writes them to disk, to run files or to
  logs. The server's own key can also be given as `--api-key`, but then it shows in the process list: prefer
  `TASKPENNY_API_KEY`.
- `taskpenny serve` listens on `127.0.0.1` by default. If you bind it to other interfaces (as the Docker image does
  inside its container), set `TASKPENNY_API_KEY`. With a key, everything except the page itself needs it: API clients
  send it as a Bearer token and the page asks for it once and keeps a session cookie (HttpOnly, SameSite=Strict,
  and Secure when a proxy in front sends `X-Forwarded-Proto: https`). Each sign-in gets its own random token;
  signing out ends it. Without a key, anyone who can reach the server can read your runs and spend your credit.
- Without a key, the server only answers requests addressed to localhost, an IP address or a one-word name, so a
  web page cannot reach it by pointing its own domain at your machine (DNS rebinding). Other names can be allowed
  with `TASKPENNY_ALLOWED_HOSTS`.
- POST requests must be JSON, so another website cannot start runs through your browser, and every run's budget
  is capped by `TASKPENNY_MAX_COST` (2 USD by default).
- Saved runs (`runs/*.json`) contain your requests and the answers. Treat that folder like the data you send.
- Requests go to the providers you configured (Vercel AI Gateway and/or the providers' own APIs, or Ollama on
  your machine). Their data policies apply.

## Supported versions

Only the latest release gets fixes while Taskpenny is below 1.0.

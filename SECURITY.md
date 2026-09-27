# Security

## Reporting a problem

Please report security problems privately through GitHub's "Report a vulnerability" button on this repository
(Security tab), not in a public issue. We aim to answer within a few days.

## What Taskpenny does with your keys

- Provider keys are read from environment variables only. Taskpenny never writes them to disk, to run files or to
  logs. The server's own key can also be given as `--api-key`, but then it shows in the process list: prefer
  `TASKPENNY_API_KEY`.
- `taskpenny serve` listens on `127.0.0.1` by default. If you bind it to other interfaces (as the Docker image does
  inside its container), set `TASKPENNY_API_KEY`, and make it long and random (`openssl rand -hex 24`). With a key,
  everything needs it except the page itself, its fonts, `/health`, `/api/login` and `/api/logout`: API clients
  send it as a Bearer token and the page asks for it once and keeps a session cookie (HttpOnly, SameSite=Strict,
  and Secure when a proxy in front sends `X-Forwarded-Proto: https`). Each sign-in gets its own random token that
  lasts 30 days; the server keeps only its SHA-256 hash (in `runs/.sessions.json`, readable by its owner only), so a
  restart does not sign anyone out, and "Sign out" on the page ends it everywhere. More than 20 wrong keys in a
  minute, from anywhere, pause key checks for the whole server for a minute. Without a key, anyone who can reach
  the server can read your runs and spend your credit.
- Put an https proxy in front of a server others can reach: the key and the session cookie must not travel in
  clear text. The proxy is also the place to limit the rate of `/api/login`.
- Without a key, the server only answers requests addressed to localhost, an IP address or a one-word name, so a
  web page cannot reach it by pointing its own domain at your machine (DNS rebinding). Other names can be allowed
  with `TASKPENNY_ALLOWED_HOSTS`.
- POST requests must be JSON, so another website cannot start runs through your browser. Every run's budget is a
  ceiling (each call reserves the most it can cost before it is made) and a request may ask for at most
  `TASKPENNY_MAX_COST` (2 USD by default). A slow client is dropped after two minutes without sending anything.
- Saved runs (`runs/*.json`) contain your requests and the answers. Treat that folder like the data you send.
- Requests go to Jev and the models through Vercel AI Gateway, and to any provider you call directly or to Ollama on
  your machine. Their data policies apply. The page and the demo page load nothing from other sites: their fonts
  (IBM Plex, SIL Open Font License) are served with them.

## Supported versions

Only the latest release gets fixes while Taskpenny is below 1.0.

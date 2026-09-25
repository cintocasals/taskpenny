# Security

## Reporting a problem

Please report security problems privately through GitHub's "Report a vulnerability" button on this repository
(Security tab), not in a public issue. We aim to answer within a few days.

## What SIAC does with your keys

- Keys are read from environment variables only. SIAC never writes them to disk, to run files or to logs.
- `siac serve` listens on `127.0.0.1` by default. If you bind it to other interfaces (as the Docker image does
  inside its container), set `SIAC_API_KEY`: anyone who can reach an open server can spend your credit.
- Saved runs (`runs/*.json`) contain your requests and the answers. Treat that folder like the data you send.
- Requests go to the providers you configured (Vercel AI Gateway and/or the providers' own APIs, or Ollama on
  your machine). Their data policies apply.

## Supported versions

Only the latest release gets fixes while SIAC is below 1.0.

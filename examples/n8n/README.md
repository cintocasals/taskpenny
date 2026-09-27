# Taskpenny from n8n

`taskpenny-from-n8n.json` is a small workflow: a request with three asks goes to Taskpenny through n8n's
**OpenAI Chat Model** node, and the answer comes back in one piece. The only change from calling OpenAI is the
base URL. It has no credentials in it.

We ran a similar request, with four asks, from our own n8n: it cost $0.003, and Taskpenny decided not to split
it because the parts would not have gone to cheaper models.

## Use it

1. Start Taskpenny where n8n can reach it. With Docker, put both on the same network; the workflow expects the
   container to be called `taskpenny`:

   ```bash
   docker build -t taskpenny https://github.com/cintocasals/taskpenny.git
   docker network create ai 2>/dev/null
   export TASKPENNY_API_KEY=$(openssl rand -hex 24)
   docker run -d --name taskpenny --network ai -e AI_GATEWAY_API_KEY -e TASKPENNY_API_KEY taskpenny
   docker network connect ai <your n8n container>
   ```

   Without Docker, set `TASKPENNY_API_KEY`, run `taskpenny serve --host 0.0.0.0` and use
   `http://<that machine>:8765/v1` below.

2. In n8n, **Import from file** and pick `taskpenny-from-n8n.json`.
3. Open the **Taskpenny** node and create an **OpenAI** credential: API key = your `TASKPENNY_API_KEY` (any text
   if you did not set one), Base URL = `http://taskpenny:8765/v1`, or `http://<that machine>:8765/v1` without
   Docker. The address lives only in the credential, so this is the one place to change it.
4. Run it. Each call is one Taskpenny run, so it can take longer than a plain model call: the node's timeout is
   15 minutes and it does not retry (`maxRetries: 0`), so a slow run is never paid twice. Do the same in any
   OpenAI client (`max_retries=0` in the Python SDK), or send an `Idempotency-Key` header: a retry with the same
   key gets the first run's answer instead of starting a new, paid run.
5. Read `taskpenny.status` in the answer (or the `X-Taskpenny-Status` header): `done` means every check passed;
   `unverified` means the answer is complete but a check did not pass or could not run, with the reasons in
   `taskpenny.warnings`; `partial` means it stopped early (for example, at the budget).

Model names: `taskpenny` uses every provider your keys reach; `taskpenny/<profile>` (for example
`taskpenny/anthropic`) limits it. Both the Chat Completions and the Responses API work.

# subot

A Subito.it watcher that queues new listings, has OpenRouter decide whether
they are relevant, and sends approved listings to ntfy.

## Setup

Create and edit the configuration:

```bash
cp config.example.json config.json
chmod 600 config.json
```

Add one or more Subito result URLs to `search_urls`. Filters such as location and
category are encoded in each URL's path; price and shipping filters appear in
its query string, for example `?ps=100&pe=500&shp=true`.

Set `use_llm` to `true` (the default) to have OpenRouter decide which listings
are relevant. In that mode, set `openrouter_api_key` and `openrouter_model`,
then write your decision instructions in the separate `llm_system_prompt` and
`llm_rules` fields. The model receives the complete listing, including its
Subito article URL, and can use OpenRouter web search and web fetch. It must
return exactly `true` or `false`. The four `llm_web_*` values bound search
results, fetches, and fetched content. `llm_max_retries` may be 0 through 3; a
value of 3 means four total attempts for a failing listing. Set
`llm_reasoning_effort` to `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`
to enable model reasoning at that effort; set it to `none` to explicitly disable
reasoning. Omit the setting (or set it to `null`) to leave reasoning out of the
request. The selected model must support reasoning. Do not use `none` with a
model that requires reasoning.

Set `use_llm` to `false` to skip OpenRouter entirely. Every newly discovered
listing is then sent directly to ntfy, and no OpenRouter or `llm_*` settings are
required.

Create a virtual environment and install the dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install .
```

Test the configuration without sending notifications:

```bash
python subot.py --once --dry-run
```

Run the bot continuously:

```bash
python subot.py
```

This single command supervises independent poller and worker processes. The
poller persists new listings to `state.sqlite3` before the worker optionally
calls OpenRouter, so slow searches or fetches never delay polling.

## Optional: user-level systemd service

Copy the user-service template (no root required):

```bash
mkdir -p ~/.config/systemd/user
cp systemd/subot.service ~/.config/systemd/user/subot.service
```

Replace `/absolute/path/to/subot` in the copied file, then enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now subot.service
```

Keep the service running after logout. Enabling lingering normally requires
administrator privileges:

```bash
sudo loginctl enable-linger "$USER"
```

Check its status:

```bash
systemctl --user status subot.service
```

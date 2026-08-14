# subot

A small Subito.it watcher that sends matching listings to an ntfy topic.

It periodically checks the configured Subito search, filters listings by price,
and sends new matches through ntfy. Notified listing IDs are saved so the same
listing is not sent twice.

## Setup

Create `config.json` from the example and replace the placeholder values:

```bash
cp config.example.json config.json
chmod 600 config.json
```

Install the dependency in a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Test the configuration without sending notifications or updating `seen.json`:

```bash
python subot.py --once --dry-run
```

Run the bot continuously:

```bash
python subot.py
```

Processed listing IDs are stored in `seen.json` to prevent duplicate
notifications.

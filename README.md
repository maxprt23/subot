# subot

A small Subito.it watcher that sends matching listings to an ntfy topic.

It periodically checks the configured Subito search, filters listings by price,
and sends new matches through ntfy. Notified listing IDs are saved so the same
listing is not sent twice.

## Run with Docker Compose

Create `config.json` from the example and replace the placeholder values:

```bash
cp config.example.json config.json
```

### Configuration permissions

The container runs as UID `10001`. On a local workstation, keep your ownership
and grant it read access:

```bash
chmod 600 config.json
sudo setfacl -m u:10001:r-- config.json
```

On a root-managed server:

```bash
chown 10001:10001 config.json
chmod 600 config.json
```

Build and start the bot in the background:

```bash
docker compose up --detach --build
```

Follow its output:

```bash
docker compose logs --follow subot
```

Stop the bot:

```bash
docker compose down
```

The configuration is mounted read-only from `config.json`. Processed listing IDs
are stored in the `subot-data` Docker volume, so they survive container rebuilds.

To test the configuration without notifying or updating stored state:

```bash
docker compose run --rm subot --once --dry-run
```

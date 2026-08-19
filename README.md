# subot

A Subito.it watcher that sends newly discovered listings to ntfy, optionally
filtering them with an LLM.

## Setup

Create and edit the configuration:

```bash
cp config.example.json config.json
chmod 600 config.json
```

Create your local notification rules, then edit the new file to set the
listings you want to receive:

```bash
cp prompts/rules.example.md prompts/rules.md
```

[`prompts/system.md`](prompts/system.md) contains the fixed model behavior and
safety policy.

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

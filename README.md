# subot

A Subito.it watcher that sends new listings matching a price range to ntfy.

## Setup

Create and edit the configuration:

```bash
cp config.example.json config.json
chmod 600 config.json
```

Create a virtual environment and install the dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
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

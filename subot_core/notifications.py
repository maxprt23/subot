"""Dispatch listing notifications to configured delivery channels."""

from . import ntfy, telegram
from .config import get_notification_channels


def notify(cfg, item):
    """Send a listing through every selected channel in configured order."""

    senders = {
        "ntfy": ntfy.notify,
        "telegram": telegram.notify,
    }
    for channel in get_notification_channels(cfg):
        senders[channel](cfg, item)

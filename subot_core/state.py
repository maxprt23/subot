import hashlib
import json
import os
import tempfile

from .models import SeenState


def search_key(search_url):
    return hashlib.sha256(search_url.encode("utf-8")).hexdigest()


def load_seen(path):
    if not os.path.exists(path):
        return SeenState()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("seen state has an unsupported format")

    listing_ids = data.get("listing_ids")
    initialized_searches = data.get("initialized_searches")
    if not isinstance(listing_ids, list) or not isinstance(
        initialized_searches, list
    ):
        raise ValueError("seen state is malformed")
    if any(not isinstance(value, str) for value in listing_ids):
        raise ValueError("seen state listing IDs must be strings")
    if any(not isinstance(value, str) for value in initialized_searches):
        raise ValueError("seen state search keys must be strings")

    return SeenState(set(listing_ids), set(initialized_searches))


def save_seen(path, state):
    directory = os.path.dirname(os.path.abspath(path))
    basename = os.path.basename(path)
    fd, temporary_path = tempfile.mkstemp(
        dir=directory,
        prefix=f".{basename}.",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": 1,
                    "listing_ids": sorted(state.listing_ids),
                    "initialized_searches": sorted(
                        state.initialized_searches
                    ),
                },
                f,
                indent=2,
            )
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise

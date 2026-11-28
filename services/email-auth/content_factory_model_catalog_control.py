"""Owner-managed catalogue-only provider keys and defensive UI snapshots.

Keys are stored in the existing encrypted vault. They are independent of n8n
credentials and never returned to a browser by these helpers.
"""
import re
import time

SLOTS = {'openai': 'content_factory_catalog_openai', 'gemini': 'content_factory_catalog_gemini'}
STALE_SECONDS = 36 * 60 * 60


def _valid_key(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[!-~]{8,2048}', value))


def save_catalog_key(vault, provider, key):
    if provider not in SLOTS or not _valid_key(key):
        raise ValueError('Invalid provider key')
    try:
        vault.save(SLOTS[provider], {'api_key': key})
    except Exception:
        raise RuntimeError('Could not save provider key') from None


def snapshot(vault, catalog, *, schedule_path=None, now=time.time):
    """Return no secrets; never make a failed optional catalog break the portal."""
    attempts = {}
    if schedule_path is not None:
        try:
            from content_factory_model_schedule import last_attempts
            attempts = last_attempts(schedule_path)
        except Exception:
            pass
    result = {}
    for provider, slot in SLOTS.items():
        item = {'status': 'unavailable', 'updated_at': None, 'ids': []}
        try:
            stored = vault.read(slot)
            if stored is None:
                item['status'] = 'not_configured'
            else:
                key = stored.get('api_key') if isinstance(stored, dict) else None
                if not _valid_key(key):
                    raise ValueError('Invalid stored key')
                if catalog is None:
                    raise ValueError('Catalogue unavailable')
                cached = catalog.read(provider, key)
                if cached is None:
                    item['status'] = 'pending'
                else:
                    updated, ids = cached['updated_at'], cached['ids']
                    if type(updated) is not int or not 0 <= updated <= int(now()) + 300 or not isinstance(ids, list):
                        raise ValueError('Invalid catalogue')
                    item.update(status='stale' if now() - updated > STALE_SECONDS else 'ready',
                                updated_at=updated, ids=list(ids))
        except Exception:
            pass
        item['last_attempt'] = attempts.get(provider)
        result[provider] = item
    return result

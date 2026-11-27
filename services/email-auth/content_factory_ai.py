"""Local drafts only; these settings are not sent to n8n or providers."""
import re

_MODELS = 'content_factory_ai_models'


def _looks_like_secret(value):
    # Credentials must never be stored or reflected as model identifiers. Check
    # each slash-delimited component so a prefixed identifier cannot hide one.
    return any(part.lower().startswith(('sk-', 'aiza')) for part in value.split('/'))


def save_models(vault, text_model, image_model, video_model):
    for value, pattern in ((text_model, r'[A-Za-z0-9][A-Za-z0-9._/-]{0,79}'),
                           (image_model, r'[A-Za-z0-9][A-Za-z0-9._/-]{0,79}'),
                           (video_model, r'(?:[A-Za-z0-9][A-Za-z0-9._/-]{0,79})?')):
        if not isinstance(value, str) or len(value) > 80 or not re.fullmatch(pattern, value) or _looks_like_secret(value):
            raise ValueError('Invalid model identifier')
    vault.save(_MODELS, {'text_model': text_model, 'image_model': image_model, 'video_model': video_model})


def snapshot(vault):
    values = vault.read(_MODELS) or {}
    if not isinstance(values, dict):
        raise ValueError('Invalid stored settings')
    models = {name: values.get(name, '') for name in ('text_model', 'image_model', 'video_model')}
    # Invalid or legacy data is not reflected into HTML.
    if any(not isinstance(v, str) or len(v) > 80 or not re.fullmatch(r'[A-Za-z0-9._/-]*', v) or _looks_like_secret(v) for v in models.values()):
        raise ValueError('Invalid stored settings')
    return {'ai_settings': models}

"""Compile reviewed routes against pinned vendor OpenAPI documents, offline.

python compile.py DIRECTORY_WITH_SOURCES --output services/email-auth/market_routes.jsonl
Input filenames and original SHA-256 digests are in sources.json. WB YAML
requires PyYAML at build time only. The service never downloads a specification.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


HERE = Path(__file__).resolve().parent
KEEP = ('type', 'format', 'enum', 'required', 'nullable', 'minimum', 'maximum',
        'exclusiveMinimum', 'exclusiveMaximum', 'minLength', 'maxLength', 'minItems',
        'maxItems', 'uniqueItems', 'pattern', 'default', 'minProperties', 'maxProperties')
AUTH = {'authorization', 'api-key', 'client-id', 'x-api-key'}


def deref(document, value):
    while '$ref' in value:
        reference = value['$ref']
        if not reference.startswith('#/components/'):
            raise ValueError(f'Unexpected reference {reference}')
        value = document
        for part in reference[2:].split('/'):
            value = value[part.replace('~1', '/').replace('~0', '~')]
    return value


def contract(document, path_item, operation):
    definitions = {}

    def schema(source):
        if '$ref' in source:
            name = source['$ref'].split('/')[-1]
            if not re.fullmatch(r'[\w.-]+', name):
                raise ValueError('Invalid schema definition')
            if name not in definitions:
                definitions[name] = {}
                definitions[name] = schema(deref(document, source))
            return {'$ref': '#/$defs/' + name}
        result = {k:source[k] for k in KEEP if k in source}
        if 'properties' in source:
            result['properties'] = {k:schema(v) for k,v in source['properties'].items() if not v.get('readOnly')}
            result['required'] = [k for k in result.get('required', []) if k in result['properties']]
        for key in ('additionalProperties','items','not'):
            if key in source:
                v = source[key]
                result[key] = schema(v) if isinstance(v,dict) else v
        for key in ('anyOf','oneOf','allOf'):
            if key in source:
                result[key] = [schema(x) for x in source[key]]
        return result

    groups = {}
    styles = {}
    required = []
    for ref in [*path_item.get('parameters',[]), *operation.get('parameters',[])]:
        p = deref(document,ref)
        name, kind = p['name'],p['in']
        if kind=='header' and name.lower() in AUTH:
            continue
        if kind not in ('header','path','query'):
            raise ValueError(f'Unsupported parameter {kind} {name}')
        group = groups.setdefault(kind,{'type':'object','properties':{},'required':[],'additionalProperties':False})
        group['properties'][name] = schema(p.get('schema',{}))
        if p.get('required') or kind=='path':
            group['required'].append(name)
            if kind not in required:
                required.append(kind)
        if kind=='query':
            styles[name] = [p.get('style','form'),p.get('explode',p.get('style','form')=='form')]
    content = {}
    if operation.get('requestBody'):
        body = deref(document,operation['requestBody'])
        for media, spec in body.get('content',{}).items():
            content[media] = schema(spec.get('schema',{}))
        if body.get('required'):
            required.append('body')
    return dict(parameters=groups,required=required,media=content,query_styles=styles,definitions=definitions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    sources = json.loads((HERE/'sources.json').read_text(encoding='utf-8'))
    docs = {}
    for key, src in sources.items():
        raw = (args.directory/src['filename']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=src['sha256']:
            raise ValueError(f'Source digest mismatch: {key}')
        if src['filename'].endswith('.json'):
            docs[key] = json.loads(raw)
        else:
            import yaml
            docs[key] = yaml.safe_load(raw)
    ledger = [json.loads(line) for line in (HERE/'review.jsonl').read_text(encoding='utf-8').splitlines()]
    by_id = {row['operation_id']:row for row in ledger}
    if len(by_id) != len(ledger):
        raise ValueError('Duplicate reviewed operation')
    with args.output.open('w', encoding='utf-8', newline='\n') as out:
        for entry in ledger:
            row = dict(entry)
            if 'alias_of' in entry:
                target = by_id[entry['alias_of']]
                if (target['market'] != entry['market'] or target['category'] not in ('read','write')
                        or 'alias_of' in target or entry['category'] != target['category']):
                    raise ValueError('Invalid alias target: '+entry['operation_id'])
                row = {**target, 'operation_id':entry['operation_id'], 'alias_of':target['operation_id'],
                       'lifecycle':'alias', 'legacy_route':{k:entry[k] for k in ('method','host','path')},
                       'review':entry['review']}
                if entry['market']=='ozon':
                    row.update(source=entry['source'],schema_date=entry['schema_date'])
                if 'required_values' in entry:
                    row['required_values'] = entry['required_values']
            if row['category'] in ('read','write'):
                source = sources[row['source']]
                document = docs[row['source']]
                path_item = document['paths'][row.get('official_path',row['path'])]
                operation = path_item[row['method'].lower()]
                servers = operation.get('servers',path_item.get('servers',document.get('servers',[])))
                if row['host'] not in {urlsplit(s['url']).hostname for s in servers}:
                    raise ValueError('Host differs from vendor operation: '+row['operation_id'])
                row['contract'] = contract(document,path_item,operation)
                if row['operation_id']=='wb_post_api_search_report_product_search_texts':
                    # The two overlapping intervals apply to different WB subscriptions.
                    # They are alternatives for the seller account, not exclusive JSON types.
                    limit = row['contract']['definitions']['TextLimit']
                    limit['anyOf'] = limit.pop('oneOf')
                row['schema_sha256'] = source['sha256']
            for replacement in row.get('replacement',[]):
                target = by_id.get(replacement.get('operation_id'))
                if not target or target['category'] not in ('read','write'):
                    raise ValueError('Missing executable replacement: '+row['operation_id'])
            ordered={'operation_id':row.pop('operation_id'),**row}
            print(json.dumps(ordered,ensure_ascii=False,separators=(',',':'),allow_nan=False),file=out)


if __name__=='__main__':
    main()

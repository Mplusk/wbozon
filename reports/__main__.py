"""Generate a read-only local owner report; never execute or transmit content."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import sys

from pipeline.candidate import Stage, blockers


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def freshness(value, now, max_age_hours):
    """Check dataset observation time, not the freshness of its individual sources."""
    try:
        t = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if t.utcoffset() is None:
            raise ValueError
        t = t.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 'unknown'
    if t > now + timedelta(minutes=5):
        return 'future'
    return 'stale' if now - t > timedelta(hours=max_age_hours) else 'recent'


def amount(value):
    # Only numeric calculator outputs are emitted, never raw user-provided text.
    if isinstance(value, bool) or value is None or len(str(value)) > 80:
        return None
    try:
        n = Decimal(str(value))
        return str(n) if n.is_finite() and abs(n) < Decimal('1e18') else None
    except (InvalidOperation, ValueError):
        return None


def next_steps(candidate, stage, calculation):
    """Describe completeness, not authorization or a verdict for later stages."""
    if stage is None:
        return {'demand': 'unknown', 'economics': 'unknown', 'rights': 'unknown',
                'owner_decisions': {name: 'unknown' for name in ('sample', 'launch', 'publication')}}
    demand = candidate.get('demand')
    if not isinstance(demand, dict) or not demand:
        demand_status = 'missing'
    else:
        demand_status = ('incomplete_or_below_threshold' if blockers({'stage': 'demand', 'demand': demand})
                         else 'recorded_unverified')
    if calculation is None:
        economics_status = 'missing'
    elif not calculation.get('valid'):
        economics_status = 'invalid'
    elif calculation.get('freshness') != 'recent':
        economics_status = 'not_current'
    elif not calculation.get('thresholds_passed'):
        economics_status = 'thresholds_failed'
    else:
        economics_status = 'recorded_unverified'
    rights = candidate.get('rights')
    rights_recorded = (isinstance(rights, dict) and rights.get('status') == 'settled'
                       and isinstance(rights.get('owner_evidence'), str)
                       and bool(rights['owner_evidence'].strip()))
    stages = list(Stage)
    decisions = {}
    approvals = candidate.get('approvals')
    approvals = approvals if isinstance(approvals, dict) else {}
    for name, gate in (('sample', Stage.SAMPLE_APPROVAL), ('launch', Stage.LAUNCH_APPROVAL),
                       ('publication', Stage.PUBLICATION_APPROVAL)):
        if stages.index(stage) < stages.index(gate):
            decisions[name] = 'not_yet_assessed'
            continue
        record = approvals.get(name)
        if (isinstance(record, dict) and record.get('approved') is True and record.get('channel') == 'telegram'
                and isinstance(record.get('evidence'), str) and bool(record['evidence'].strip())):
            decisions[name] = 'recorded_unverified'
        else:
            decisions[name] = 'record_missing'
    return {'demand': demand_status, 'economics': economics_status,
            'rights': 'owner_recorded_unverified' if rights_recorded else 'not_confirmed',
            'owner_decisions': decisions}


def generate(db_path, *, now=None, max_age_hours=24):
    """Return latest revision per candidate and its latest attached calculation.

    Source descriptions and marketplace text are not included in the report;
    numeric revision and calculation IDs can be used to inspect original records.
    The database opens read-only, and no evidence or decisions are authenticated.
    """
    if not isinstance(max_age_hours, (int, float)) or isinstance(max_age_hours, bool) or not 0 < max_age_hours <= 87600:
        raise ValueError('Неверный срок актуальности')
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError('Нужен часовой пояс')
    now = now.astimezone(timezone.utc)
    uri = Path(db_path).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        rows = conn.execute('''
            SELECT c.id, c.payload_json, c.observed_at,
                   e.id AS calculation_id, e.result_json, e.observed_at AS calculation_observed_at
            FROM candidate_revisions c
            LEFT JOIN economics_runs e ON e.id = (
                SELECT MAX(e2.id) FROM economics_runs e2 WHERE e2.candidate_revision_id = c.id
            )
            WHERE c.id IN (SELECT MAX(id) FROM candidate_revisions GROUP BY candidate_id)
            ORDER BY c.id
        ''').fetchall()
    items = []
    for row in rows:
        try:
            candidate = json.loads(row['payload_json'])
            if not isinstance(candidate, dict):
                raise ValueError
            stage = Stage(candidate.get('stage'))
            missing = blockers(candidate)
        except (ValueError, TypeError, KeyError):
            candidate, stage, missing = {}, None, ['Некорректные данные кандидата']
        candidate_freshness = freshness(row['observed_at'], now, max_age_hours)
        calculation = None
        if row['calculation_id'] is not None:
            try:
                result = json.loads(row['result_json'])
                if not isinstance(result, dict):
                    raise ValueError
                checks = result.get('checks')
                keys = ('margin', 'profit', 'profit_per_hour', 'break_even_ad', 'stress')
                if not isinstance(checks, dict) or not all(isinstance(checks.get(k), bool) for k in keys):
                    raise ValueError
                calculation = {
                    'id': row['calculation_id'],
                    'observed_at': timestamp(row['calculation_observed_at']),
                    'freshness': freshness(row['calculation_observed_at'], now, max_age_hours),
                    'profit_rub': amount(result.get('profit_rub')),
                    'margin_rate': amount(result.get('margin_rate')),
                    'thresholds_passed': all(checks[k] for k in keys),
                    'failed_checks': [k for k in keys if not checks[k]],
                }
                if calculation['profit_rub'] is None or calculation['margin_rate'] is None:
                    calculation['valid'] = False
                else:
                    calculation['valid'] = True
            except (ValueError, TypeError):
                calculation = {'id': row['calculation_id'], 'valid': False}
        gate = stage.value if stage in (Stage.SAMPLE_APPROVAL, Stage.LAUNCH_APPROVAL, Stage.PUBLICATION_APPROVAL) else None
        approval_pending = bool(gate and any(item.startswith('approvals.') for item in missing))
        items.append({
            'candidate_revision_id': row['id'], 'stage': stage.value if stage else None,
            'observed_at': timestamp(row['observed_at']), 'freshness': candidate_freshness,
            'blockers': missing, 'owner_decision_record_missing': gate if approval_pending else None,
            'calculation': calculation,
            'next_steps': next_steps(candidate, stage, calculation),
        })
    return {
        'generated_at': now.isoformat(), 'max_age_hours': max_age_hours,
        'candidate_count': len(items), 'candidates': items,
        'sales_orders_profit_and_stock': 'Данных нет в локальном хранилище; продажи, прибыль бизнеса и остатки не оценивались.',
        'note': ('Локальная сводка: записи и источники не проверены. Актуальность относится только к времени наблюдения пакета исходных данных, '
                 'не к каждому тарифу. Идентификаторы версии и расчёта позволяют найти источники в защищённой базе. '
                 'Блокировки относятся только к текущей стадии; следующие этапы указаны отдельно. '
                 'Подтверждения не удостоверены; внешних действий и отправки отчёта не было.'),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Локальная сводка кандидатов и расчётов без сети.')
    parser.add_argument('--db', type=Path, required=True, help='существующая база SQLite')
    parser.add_argument('--max-age-hours', type=float, default=24, help='порог устаревания наблюдений, по умолчанию 24 ч')
    args = parser.parse_args(argv)
    try:
        report = generate(args.db, max_age_hours=args.max_age_hours)
    except (ValueError, OSError, sqlite3.Error):
        print('Не удалось прочитать базу или неверно задан срок актуальности.', file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())

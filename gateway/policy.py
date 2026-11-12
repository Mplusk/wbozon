"""Pure policy checks for *trusted server state*, not an authorization boundary.

``evaluate(operation, proposal, state)`` returns ``allowed`` and reason codes.
``proposal`` contains the proposed action. ``state`` MUST come from protected,
server-maintained records tied to that exact proposal and item, including a
verified owner decision; callers cannot supply these facts via HTTP. In
particular booleans are not proof of identity or approval. The gateway must
separately enforce role authorization, read-only/stop mode, audit, idempotency,
transactional concurrency, and prohibit live adapters until all are integrated.

Every operation requires either ``owner_approved`` or explicit per-operation
``autonomy_enabled`` and ``first_month_completed`` from trusted configuration.
Defaults are absent/denied; this represents the first-month approval rule. A decision to enable autonomy
must be made externally by the owner. The policy never changes state or sends
requests, and does not authenticate the trusted state.

Operations and required fields (all prices are RUB; dates ISO 8601 with zone):
* price: proposal ``new_price``; state ``current_price``, ``minimum_price``,
  ``last_price_change_at`` (None only if server has confirmed no prior change),
  ``now``. Invariant minimum and 24-hour cooldown apply even with approval;
  changes over 5% require owner approval.
* advertising: proposal ``spend``, ``new_product_budget``; state
  ``product_spent``, ``current_product_budget``, ``approved_product_budget``,
  ``today_spent``, ``daily_budget``. The new budget cannot exceed the cap
  already approved in server state; any increase from the current budget
  additionally requires owner approval. Incremental spend must fit both limits.
* review: proposal ``review_id``; state ``review_id`` for the exact same review
  and its server-derived ``rating`` integer 1..5. Low ratings 1..3 need approval.
  A client-supplied rating is never accepted.
* print: proposal ``quantity``; state ``monthly_used``, ``monthly_limit`` and
  ``is_first_batch``. Exceeding the trusted monthly cap always fails; the first
  batch additionally needs owner approval. Increasing a cap requires a separate
  authenticated update to trusted state before invoking this function.
* publish: trusted state must contain true booleans ``reviewer_pass``,
  ``rights_settled`` and ``owner_approved``; independent reviewer identity and
  freshness must be enforced by the provider of this trusted state.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction


class _Invalid(ValueError):
    pass


def _number(value, *, positive=False, integer=False):
    if value is None or isinstance(value, (bool, list, dict)) or len(str(value)) > 80:
        raise _Invalid
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _Invalid from None
    if not n.is_finite() or n < 0 or n > Decimal('1e18') or (positive and n <= 0):
        raise _Invalid
    if integer and n != n.to_integral_value():
        raise _Invalid
    return Fraction(n)


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise _Invalid
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if d.utcoffset() is None:
            raise ValueError
        return d.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise _Invalid from None


def evaluate(operation, proposal, state):
    """Evaluate a supported operation; missing/invalid trusted inputs fail closed."""
    if not isinstance(operation, str) or not isinstance(proposal, dict) or not isinstance(state, dict):
        return {'allowed': False, 'reasons': ['invalid_input']}
    if operation not in {'price', 'advertising', 'review', 'print', 'publish'}:
        return {'allowed': False, 'reasons': ['unsupported_operation']}
    approved = state.get('owner_approved') is True
    autonomous = state.get('autonomy_enabled') is True and state.get('first_month_completed') is True
    reasons = []
    if not approved and not autonomous:
        reasons.append('owner_approval_required')
    try:
        if operation == 'price':
            new = _number(proposal.get('new_price'), positive=True)
            old = _number(state.get('current_price'), positive=True)
            floor = _number(state.get('minimum_price'), positive=True)
            now = _timestamp(state.get('now'))
            if 'last_price_change_at' not in state:
                raise _Invalid
            last = state['last_price_change_at']
            if last is not None:
                last = _timestamp(last)
                if last > now or now - last < timedelta(hours=24):
                    reasons.append('price_change_cooldown')
            if new < floor:
                reasons.append('below_minimum_price')
            # Multiplication instead of division avoids rounding at the boundary.
            if abs(new - old) * 100 > old * 5 and not approved:
                reasons.append('price_change_approval_required')
        elif operation == 'advertising':
            spend = _number(proposal.get('spend'))
            new_budget = _number(proposal.get('new_product_budget'))
            used = _number(state.get('product_spent'))
            current = _number(state.get('current_product_budget'))
            approved_cap = _number(state.get('approved_product_budget'))
            today = _number(state.get('today_spent'))
            daily_cap = _number(state.get('daily_budget'))
            if new_budget > current and not approved:
                reasons.append('budget_increase_approval_required')
            if new_budget > approved_cap or used + spend > new_budget or used + spend > approved_cap:
                reasons.append('product_budget_exceeded')
            if today + spend > daily_cap:
                reasons.append('daily_budget_exceeded')
        elif operation == 'review':
            review_id = proposal.get('review_id')
            trusted_id = state.get('review_id')
            if ('rating' in proposal or not isinstance(review_id, str) or not review_id.strip()
                    or len(review_id) > 128 or review_id != trusted_id):
                raise _Invalid
            rating = _number(state.get('rating'), positive=True, integer=True)
            if rating > 5:
                raise _Invalid
            if rating <= 3 and not approved:
                reasons.append('negative_review_approval_required')
        elif operation == 'print':
            quantity = _number(proposal.get('quantity'), positive=True, integer=True)
            used = _number(state.get('monthly_used'), integer=True)
            limit = _number(state.get('monthly_limit'), integer=True)
            first = state.get('is_first_batch')
            if not isinstance(first, bool):
                raise _Invalid
            if first and not approved:
                reasons.append('first_batch_approval_required')
            if quantity + used > limit:
                reasons.append('monthly_print_limit_exceeded')
        else:
            if not approved:
                reasons.append('publication_approval_required')
            if state.get('reviewer_pass') is not True:
                reasons.append('independent_review_required')
            if state.get('rights_settled') is not True:
                reasons.append('rights_confirmation_required')
    except _Invalid:
        reasons.append('missing_or_invalid_data')
    return {'allowed': not reasons, 'reasons': reasons}

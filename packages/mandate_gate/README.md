# mandate-gate

Deterministic, ordered authorization checks and exact-action approval binding for actions proposed by an AI model.

An agent framework lets a model *propose* a tool call. `mandate-gate` is the small piece that decides, outside the model, whether that specific action may run now under the authority a human granted, and whether a human approval really covers it. It has no dependencies.

## Install

```bash
pip install "mandate-gate @ git+https://github.com/vaikhariai/delegate#subdirectory=packages/mandate_gate"
```

Python 3.11 or newer, no dependencies. Not yet published to PyPI.

## What it gives you

- **`Gate`**: an ordered list of named checks. Each returns `None` (pass) or an `Outcome` (`deny`, `invalid`, `approval_required`, or an early `allow`). The first decisive outcome wins, so check order is explicit and testable. The `Decision` records which check decided and which passed.
- **`canonical_bytes` / `action_hash`** (rule `delegate-json-v1`): sorted keys, compact separators, UTF-8 without escaping, integers only (floats raise), SHA-256. Use it to bind an approval to exact content.
- **`check_binding`**: is this approval approved, unconsumed, unexpired, and for exactly this action ID, action hash, principal and authority revision? Returns a reason code or `None`.

## Example

```python
from mandate_gate import Check, Gate, deny, invalid, approval_required, check_binding, action_hash

gate = Gate([
    Check("types", lambda a, m, c: invalid("AMOUNT_NOT_INTEGER") if type(a.amount_minor) is not int else None),
    Check("authority", lambda a, m, c: None if m.active else deny("MANDATE_NOT_ACTIVE")),
    Check("bounds", lambda a, m, c: deny("OVER_LIMIT") if a.amount_minor > m.hard_limit_minor else None),
    Check("approval", lambda a, m, c: (approval_required(code) if (code := check_binding(
        c.approval, action_id=a.action_id, action_hash=action_hash(a.body()), principal=c.principal,
        authority_revision=m.revision, now=c.now)) else None) if a.amount_minor > m.auto_limit_minor else None),
])
decision = gate.evaluate(action, mandate_snapshot, context)
```

## One gate, other domains

The marketplace negotiation is the shipped product. The other rows are package examples with tests, to show the pattern carries over; there is no product behind them.

| Domain | Agent may do alone | Needs a person's approval of the exact action | Never, approval or not | Where |
| --- | --- | --- | --- | --- |
| Marketplace selling (Delegate) | Answer from public facts, counter inside the price range, decline | A reservation the buyer accepted | Price under the private minimum, share private details, reserve for a flagged buyer | `services/api/delegate/policy.py` (eleven checks) |
| Customer support refunds | Refund up to the auto limit; reply from fixed templates | A refund above the auto limit (manager) | Over the hard limit or the order total, another customer's order, a second refund, free-text replies | `examples/support_refund_gate.py` |
| Expense reimbursement | Reimburse small claims in allowed categories | Claims above the auto limit | Over the hard limit, other categories or currencies | `examples/expense_gate.py` |

Each example puts hard checks before the approval check, binds approval to the action hash and authority revision, and is covered in `tests/`.

## How to use it correctly

1. Build the action envelope on the server. The model proposes a payload; identity, scope and authority revision come from your server.
2. Take a fresh snapshot of authority and world state, and **re-run the gate inside the same transaction that performs the effect**, holding the lock that authority changes (revoke, edit) also take. An earlier "allow" is not permission later.
3. Never let an approval override a hard denial: put hard checks before the approval check.
4. Consume the approval in the same transaction as the effect.

## Limits

- It checks structured fields against structured authority. It does not understand free text, detect prompt injection in general, or make an unconstrained model safe. Keep the action vocabulary narrow and render outbound content from templates.
- The hash proves content consistency, not who created it or that a database was not altered.
- Concurrency guarantees come from how you call it (locks, transactions), not from the package.

## Tests

`uv run pytest packages/mandate_gate` runs the canonicalization vectors, ordering, approval binding cases and the example.

Licence: Apache-2.0.

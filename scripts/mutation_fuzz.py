"""Generated bad versions of one entry's input file, for the mutation planners' isolation fuzz tests."""

from __future__ import annotations

import json
import random

import mutation_cost

DEPTH = 100_000
SHALLOW = 900
# One value of each JSON kind, and numbers no measure holds.
KINDS = (None, True, 'x', [], {}, -1, 1.5)
HUGE = (1e308, 10**400, float('inf'), float('nan'), -1e308, 2**53 + 1)


def kind(v: object) -> str:
    if v is None or isinstance(v, bool):
        return repr(v)
    if isinstance(v, (int, float)):
        return 'number'
    return type(v).__name__


def paths(doc: object, prefix: tuple = ()) -> list[tuple]:
    """Every key path inside DOC's objects and arrays."""
    items = (
        doc.items() if isinstance(doc, dict) else enumerate(doc) if isinstance(doc, list) else ()
    )
    out = []
    for k, v in items:
        out.append((*prefix, k))
        out += paths(v, (*prefix, k))
    return out


def replaced(doc: object, path: tuple, value: object) -> object:
    copy = json.loads(json.dumps(doc))
    node = copy
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    return copy


def bad_inputs(valid: object, seed: int) -> list[tuple[str, bytes | None]]:
    """Labelled bad versions of VALID's file: missing, empty, random bytes, invalid UTF-8,
    deep nesting, huge values, oversize and wrong types; None is a missing file."""
    rng = random.Random(seed)
    text = json.dumps(valid).encode()
    cases: list[tuple[str, bytes | None]] = [('missing', None), ('empty', b''), ('blank', b' \n')]
    cases += [(f'random bytes {n}', rng.randbytes(n)) for n in (1, 7, 300, 5000)]
    for bad in (b'\xff', b'\xc0\xaf', b'\x80', b'\xed\xa0\x80', b'\xc3'):
        at = rng.randrange(len(text) + 1)
        cases.append((f'invalid UTF-8 {bad!r} at {at}', text[:at] + bad + text[at:]))
    cases += [
        ('deep arrays', b'[' * DEPTH + b']' * DEPTH),
        ('deep objects', b'{"a":' * DEPTH + b'1' + b'}' * DEPTH),
    ]
    every = paths(valid)
    for path in rng.sample(every, min(4, len(every))):
        marked = json.dumps(replaced(valid, path, '\x00deep')).encode()
        cases.append(
            (
                f'deep nesting at {path}',
                marked.replace(b'"\\u0000deep"', b'[' * DEPTH + b']' * DEPTH),
            )
        )
        # Deep enough to slow a recursive walk, shallow enough to parse.
        cases.append(
            (
                f'nesting {SHALLOW} at {path}',
                marked.replace(b'"\\u0000deep"', b'[' * SHALLOW + b']' * SHALLOW),
            )
        )
    cases += [
        ('huge number', b'1e999'),
        ('huge integer', b'9' * 5000),
        ('NaN', b'NaN'),
        ('oversize', text + b' ' * (mutation_cost.MAX_INPUT_BYTES + 1 - len(text))),
    ]
    numbers = [p for p in every if kind(dig(valid, p)) == 'number']
    for path in rng.sample(numbers, min(8, len(numbers))):
        value = rng.choice(HUGE)
        cases.append(
            (f'huge {value!r:.20} at {path}', json.dumps(replaced(valid, path, value)).encode())
        )
    cases += [
        (f'top-level {kind(v)}', json.dumps(v).encode()) for v in KINDS if kind(v) != kind(valid)
    ]
    for path in every:
        others = [v for v in KINDS if kind(v) != kind(dig(valid, path))]
        value = rng.choice(others)
        cases.append(
            (f'{kind(value)} at {path}', json.dumps(replaced(valid, path, value)).encode())
        )
    return cases


def dig(doc: object, path: tuple) -> object:
    for k in path:
        doc = doc[k]
    return doc


def usable(data: bytes | None) -> bool:
    """Whether DATA parses to a JSON object within mutation_cost.MAX_INPUT_BYTES, as the planners read it."""
    if data is None or len(data) > mutation_cost.MAX_INPUT_BYTES:
        return False
    try:
        return isinstance(json.loads(data.decode()), dict)
    except ValueError, RecursionError:
        return False

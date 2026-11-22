"""Bunotan engine: uniform random derangement using a CSPRNG."""
import secrets

_rng = secrets.SystemRandom()


class DrawError(ValueError):
    pass


def generate_assignments(participant_ids, rng=_rng):
    """Return {giver_id: recipient_id}. Uniform over all derangements
    (shuffle + reject any permutation with a fixed point)."""
    ids = list(participant_ids)
    if len(ids) != len(set(ids)):
        raise DrawError("Duplicate participants in draw list")
    if len(ids) < 2:
        raise DrawError("At least two approved participants are required")
    while True:
        perm = ids[:]
        rng.shuffle(perm)
        if all(g != r for g, r in zip(ids, perm)):
            return dict(zip(ids, perm))


def validate_assignments(participant_ids, mapping):
    """Return list of problems; empty list means valid."""
    ids = set(participant_ids)
    problems = []
    if set(mapping) != ids:
        problems.append("givers do not match participants exactly")
    if set(mapping.values()) != ids:
        problems.append("recipients do not match participants exactly")
    if len(set(mapping.values())) != len(mapping):
        problems.append("duplicate recipients")
    if any(g == r for g, r in mapping.items()):
        problems.append("self-assignment present")
    return problems

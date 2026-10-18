"""Compatibility entrypoint; unprepared baseline-only publication is forbidden."""
from .persistence import PersistenceError, main


def save(root, baseline):
    # Old callers cannot prove pre-computation refresh or Recovery completion.
    raise PersistenceError('PREPARED_RECOVERY_REQUIRED: use model511_adaptive.persistence')


if __name__ == '__main__':
    main()

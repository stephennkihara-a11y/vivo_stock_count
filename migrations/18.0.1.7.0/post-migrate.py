def migrate(cr, version):
    """Two-party flow: the per-rack second-person approval step is removed.

    Any rack still parked in the old `physical_review` state (awaiting a
    second-person approval) is moved to `pending_review` — "scanned, awaiting
    store reconcile" — so a live rack is not stranded when the approver actions
    disappear. The scanned lines are preserved; only the state changes.
    """
    cr.execute(
        "UPDATE vivo_count_section SET state = 'pending_review' "
        "WHERE state = 'physical_review'"
    )

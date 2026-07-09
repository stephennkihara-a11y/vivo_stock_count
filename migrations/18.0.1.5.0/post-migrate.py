"""Remove the `variance_rescan` section state (18.0.1.5.0).

The Variance Re-scan branch is gone: submitting the physical count always
routes a section to `pending_review`, where the auditor sees any variance.
Any section currently sitting in `variance_rescan` is moved to
`pending_review` so the auditor can resolve it — its scan lines,
`physical_total_qty` and `rescan_count` are preserved.

No scheduled/automated action referenced this state (the only cron is the
monthly shrinkage roll-up), so nothing else needs migrating.
"""


def migrate(cr, version):
    cr.execute(
        "UPDATE vivo_count_section SET state = 'pending_review' "
        "WHERE state = 'variance_rescan'"
    )

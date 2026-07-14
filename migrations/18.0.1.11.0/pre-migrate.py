def migrate(cr, version):
    """Unknown-barcode capture: product_id becomes OPTIONAL on count lines.

    The column was created NOT NULL back when every line required a SKU.
    Making the field ``required=False`` in Python does NOT, on its own,
    reliably drop an existing NOT NULL constraint on ``-u``, so drop it
    explicitly here (pre-migration, before the model schema is synced). This
    lets product-less "unknown" capture lines be inserted with product_id NULL.

    No data is touched — every existing line already has a product_id, so
    relaxing the constraint is safe and leaves current rows unchanged.
    """
    cr.execute(
        "ALTER TABLE vivo_count_line ALTER COLUMN product_id DROP NOT NULL"
    )

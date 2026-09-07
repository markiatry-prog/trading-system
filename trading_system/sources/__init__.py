"""Provider adapters.

Each module here translates ONE vendor's wire format into the canonical
records in `trading_system.market_data`, and nothing else. Adapters are
the only place a vendor's name appears; the feature engine never imports
from this package.
"""

"""BorrowBox — Python client for BorrowBox library audiobook catalogs."""

from borrowbox.auth import BorrowBoxClient
from borrowbox.catalog import BorrowBoxCatalog
from borrowbox.models import Availability, CatalogItem, Format, ProductDetail

__all__ = [
    "Availability",
    "BorrowBoxCatalog",
    "BorrowBoxClient",
    "CatalogItem",
    "Format",
    "ProductDetail",
]

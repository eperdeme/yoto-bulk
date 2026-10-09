"""Re-export from the standalone ``borrowbox`` package for backwards compatibility."""

from borrowbox import Availability, BorrowBoxCatalog, BorrowBoxClient, CatalogItem, Format, ProductDetail

__all__ = [
    "Availability",
    "BorrowBoxCatalog",
    "BorrowBoxClient",
    "CatalogItem",
    "Format",
    "ProductDetail",
]

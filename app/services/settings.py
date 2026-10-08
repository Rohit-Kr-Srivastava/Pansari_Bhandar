"""Business settings edited from the admin dashboard (stored as JSON in site_settings).

Defaults here are neutral placeholders. Nothing about the real shop (phone, address, history)
is invented: empty values simply hide the matching part of the website.
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.db.models import SiteSetting

SITE_KEY = "site"


class HomeRail(BaseModel):
    """A product row on the homepage, filled from one or more categories."""

    key: str
    title: str
    title_hi: str = ""
    category_slugs: list[str] = Field(default_factory=list)
    seasonal_only: bool = False
    enabled: bool = True


DEFAULT_RAILS = [
    HomeRail(
        key="daily",
        title="Daily essentials",
        title_hi="रोज़ का सामान",
        category_slugs=["grocery-ration", "household-daily-needs"],
    ),
    HomeRail(
        key="spices",
        title="Spices & Masala",
        title_hi="मसाले",
        category_slugs=["spices-masala", "whole-spices"],
    ),
    HomeRail(
        key="pansari",
        title="Pansari / Jadi-Buti",
        title_hi="पंसारी / जड़ी-बूटी",
        category_slugs=["pansari-jadi-buti"],
    ),
    HomeRail(
        key="puja", title="Puja essentials", title_hi="पूजा सामग्री", category_slugs=["puja-samagri"]
    ),
    HomeRail(
        key="shringar",
        title="Shringar & Ornaments",
        title_hi="श्रृंगार",
        category_slugs=["shringar-ornaments"],
    ),
    HomeRail(
        key="gifts",
        title="Gifts & Seasonal",
        title_hi="उपहार और त्योहार",
        category_slugs=["gifts", "seasonal-festival"],
    ),
]


class SiteSettings(BaseModel):
    # Identity and contact. Empty means "not shown".
    business_name: str = "Pansari Bhandar"
    business_name_hi: str = "पंसारी भंडार"
    tagline: str = "Spices, jadi-buti, puja samagri and daily needs"
    phone: str = ""
    whatsapp_number: str = ""  # digits with country code, e.g. 91XXXXXXXXXX
    email: str = ""
    address_line: str = ""
    locality: str = "Gomia"
    district: str = "Bokaro"
    state: str = "Jharkhand"
    pincode: str = ""
    map_url: str = ""
    latitude: str = ""
    longitude: str = ""
    opening_hours: list[str] = Field(default_factory=list)  # e.g. ["Mon-Sat 8:00 am - 9:00 pm"]
    established_year: str = ""
    story_title: str = "Our story"
    story: str = ""  # paragraphs separated by blank lines

    # Homepage
    announcement: str = ""
    announcement_enabled: bool = False
    hero_title: str = "Spices, puja samagri and daily needs, from our shop in Gomia"
    hero_subtitle: str = "Order online for local delivery, or pick up from the shop."
    hero_image: str = ""
    rails: list[HomeRail] = Field(default_factory=lambda: [r.model_copy() for r in DEFAULT_RAILS])
    why_points: list[str] = Field(
        default_factory=lambda: [
            "Fresh stock, weighed and packed at the shop",
            "Loose and packed sizes, from 50 g to 1 kg",
            "Local delivery and store pickup in Gomia",
            "Cash on delivery",
        ]
    )

    # Commerce rules
    require_product_approval: bool = True
    default_low_stock_threshold: int = Field(default=5, ge=0, le=10_000)
    max_quantity_per_item: int = Field(default=20, ge=1, le=99)
    # Optional store-wide offer: percent off when the subtotal reaches a minimum.
    offer_enabled: bool = False
    offer_min_subtotal_paise: int = Field(default=0, ge=0)
    offer_percent: int = Field(default=0, ge=0, le=50)
    offer_max_discount_paise: int = Field(default=0, ge=0)

    # SEO
    meta_description: str = (
        "Pansari Bhandar in Gomia, Jharkhand: spices and masala, jadi-buti, puja samagri, "
        "shringar items and daily needs. Order online for local delivery or store pickup."
    )

    @field_validator("whatsapp_number", "phone")
    @classmethod
    def _digits(cls, v: str) -> str:
        digits = "".join(ch for ch in v if ch in "0123456789+")
        return digits.lstrip("+")

    @property
    def story_paragraphs(self) -> list[str]:
        return [p.strip() for p in self.story.split("\n\n") if p.strip()]

    @property
    def full_address(self) -> str:
        parts = [self.address_line, self.locality, self.district, self.state, self.pincode]
        return ", ".join(p for p in parts if p)

    @property
    def has_offer(self) -> bool:
        return self.offer_enabled and self.offer_percent > 0


def load_site_settings(db: Session) -> SiteSettings:
    row = db.get(SiteSetting, SITE_KEY)
    return SiteSettings.model_validate(row.value if row else {})


def save_site_settings(db: Session, changes: dict[str, Any]) -> SiteSettings:
    """Validate and store. Raises pydantic.ValidationError on bad values."""
    current = load_site_settings(db).model_dump()
    current.update(changes)
    validated = SiteSettings.model_validate(current)
    row = db.get(SiteSetting, SITE_KEY)
    data = validated.model_dump(mode="json")
    if row is None:
        db.add(SiteSetting(key=SITE_KEY, value=data))
    else:
        row.value = data
    db.flush()
    return validated

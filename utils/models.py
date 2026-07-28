"""
Data models for scraper results.
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class AddressRecord:
    lives_at: str = ""
    city:     str = ""
    state:    str = ""
    zip_code: str = ""

    def to_dict(self) -> dict:
        return {
            "Lives At": self.lives_at,
            "City":     self.city,
            "State":    self.state,
            "ZIP":      self.zip_code,
        }


@dataclass
class PersonRecord:
    name:      str = ""
    age_year:  str = ""          # e.g. "71 yrs (1955)"
    addresses: List[AddressRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "Name":      self.name,
            "Age/Year":  self.age_year,
            "Addresses": [a.to_dict() for a in self.addresses],
        }


@dataclass
class ComplianceStatus:
    state_location: str = ""
    dnc_status:     str = ""   # "clean" / "flagged" / "unknown"
    litigator:      str = ""
    blacklist:      str = ""

    def to_dict(self) -> dict:
        return {
            "State/Location": self.state_location,
            "DNC Status":     self.dnc_status,
            "Litigator":      self.litigator,
            "Blacklist":      self.blacklist,
        }


@dataclass
class LookupResult:
    phone:      str = ""
    found:      bool = False          # True if owner info found
    compliance: Optional[ComplianceStatus] = None
    persons:    List[PersonRecord] = field(default_factory=list)
    error:      Optional[str] = None  # Human-readable error if scrape failed

    def to_dict(self) -> dict:
        return {
            "Phone":      self.phone,
            "Found":      self.found,
            "Compliance": self.compliance.to_dict() if self.compliance else {},
            "Persons":    [p.to_dict() for p in self.persons],
            "Error":      self.error,
        }

"""Domain enums for the PII security proxy."""

from enum import StrEnum


class PIIType(StrEnum):
    """Types of personal data the system can identify and mask."""

    PERSON_NAME = "PERSON_NAME"
    BIRTH_DATE = "BIRTH_DATE"
    BIRTH_PLACE = "BIRTH_PLACE"

    PASSPORT_NUMBER = "PASSPORT_NUMBER"
    CITIZENSHIP = "CITIZENSHIP"
    PASSPORT_ISSUER = "PASSPORT_ISSUER"
    PASSPORT_DIVISION_CODE = "PASSPORT_DIVISION_CODE"
    PASSPORT_ISSUE_DATE = "PASSPORT_ISSUE_DATE"

    DRIVER_LICENSE = "DRIVER_LICENSE"

    ADDRESS = "ADDRESS"
    COUNTRY = "COUNTRY"
    POSTAL_CODE = "POSTAL_CODE"
    CITY = "CITY"
    STREET = "STREET"
    HOUSE = "HOUSE"
    APARTMENT = "APARTMENT"

    EMAIL = "EMAIL"
    PHONE = "PHONE"
    INN = "INN"

    BANK_CARD = "BANK_CARD"
    CVV = "CVV"
    PIN = "PIN"
    CARDHOLDER_NAME = "CARDHOLDER_NAME"


class MaskingStrategy(StrEnum):
    """Strategies used to mask a detected PII span."""

    FULL_MASK = "FULL_MASK"
    PARTIAL_MASK = "PARTIAL_MASK"
    TOKENIZE = "TOKENIZE"
    SYNTHETIC = "SYNTHETIC"


class Operation(StrEnum):
    """Direction of processing for a given payload."""

    MASK = "MASK"
    DEMASK = "DEMASK"


class RestorationStateStatus(StrEnum):
    """Lifecycle status of a restoration state."""

    MASKED = "MASKED"
    DEMASKED = "DEMASKED"
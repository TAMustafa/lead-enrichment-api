from dataclasses import dataclass

from .models import Enrichment, Status


@dataclass(frozen=True)
class Decision:
    status: Status
    reasons: list[str]


def qualify(data: Enrichment, settings) -> Decision:
    if data.operational_status == "CLOSED_PERMANENTLY":
        return Decision(Status.REJECTED, ["BUSINESS_CLOSED_PERMANENTLY"])
    if data.premises_classification == "RESIDENTIAL":
        return Decision(Status.REJECTED, ["RESIDENTIAL_PREMISES"])
    review = []
    if settings.residential_check_enabled:
        if data.premises_check_status == "RESIDENTIAL":
            return Decision(Status.REJECTED, ["BAG_RESIDENTIAL_PREMISES"])
        if data.premises_check_status != "NON_RESIDENTIAL":
            review.append("PREMISES_CHECK_UNCERTAIN")
    if data.operational_status != "OPERATIONAL":
        review.append("OPERATIONAL_STATUS_UNCERTAIN")
    if data.premises_classification == "UNKNOWN":
        review.append("PREMISES_CLASSIFICATION_UNCERTAIN")
    if not data.business_types:
        review.append("BUSINESS_TYPE_MISSING")
    elif not set(data.business_types) & settings.allowed_types:
        return Decision(Status.REJECTED, ["BUSINESS_TYPE_NOT_ELIGIBLE"])
    if data.rating is None or data.review_count is None:
        review.append("REPUTATION_DATA_MISSING")
    if review:
        return Decision(Status.REVIEW_REQUIRED, review)
    reasons = []
    if data.rating < settings.min_rating:
        reasons.append("RATING_BELOW_THRESHOLD")
    if data.review_count < settings.min_reviews:
        reasons.append("INSUFFICIENT_REVIEWS")
    return (
        Decision(Status.REJECTED, reasons)
        if reasons
        else Decision(Status.QUALIFIED, ["ELIGIBILITY_CONFIRMED"])
    )

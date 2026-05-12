# src/property_context.py
# ─────────────────────────────────────────────────────────────────────────────
# Mock property data store for the Nistula assessment.
#
# In production, this data would live in PostgreSQL and be fetched dynamically
# using the property_id from the inbound message. For this assessment, we use
# a hardcoded dictionary keyed by property_id, which correctly simulates the
# lookup pattern a real system would use.
#
# The get_property_context() function returns a formatted string block that is
# injected directly into the Claude system prompt — giving the model grounded,
# factual knowledge about the specific villa before it drafts any reply.
# ─────────────────────────────────────────────────────────────────────────────

from typing import Optional


# ── PROPERTY DATA STORE ───────────────────────────────────────────────────────
# Keyed by property_id exactly as it arrives in the webhook payload.
# Each property is a dictionary of facts. Adding a new property means
# adding one new entry here — no other code needs to change.

PROPERTIES: dict[str, dict] = {
    "villa-b1": {
        "name": "Villa B1",
        "location": "Assagao, North Goa",
        "bedrooms": 3,
        "max_guests": 6,
        "private_pool": True,
        "check_in_time": "2:00 PM",
        "check_out_time": "11:00 AM",
        "base_rate_inr": 18000,
        "base_rate_covers_guests": 4,
        "extra_guest_charge_inr": 2000,
        "wifi_password": "Nistula@2024",
        "caretaker_hours": "8:00 AM to 10:00 PM",
        "chef_on_call": True,
        "chef_prebooking_required": True,
        "cancellation_policy": "Free cancellation up to 7 days before check-in. "
                               "After that, the first night is non-refundable.",
        "availability": {
            # Date ranges stored as (start, end) tuples for easy lookup.
            # In production this would be fetched from the PMS API in real time.
            "april_20_24": True,   # Available April 20–24
        },
        "amenities": [
            "Private swimming pool",
            "Fully equipped kitchen",
            "Air conditioning in all rooms",
            "High-speed WiFi",
            "Caretaker on call",
            "Chef available on pre-booking",
            "Free parking",
            "Daily housekeeping",
        ],
        "pet_policy": "Pets are not allowed at this property.",
        "parking": "Complimentary private parking available on site.",
    }
}


# ── CONTEXT FORMATTER ─────────────────────────────────────────────────────────
# Converts the raw property dictionary into a clean, human-readable text block.
# This text block is injected into Claude's system prompt verbatim, so the
# formatting here directly affects how well Claude understands the property.
#
# We intentionally write this as natural prose + structured facts rather than
# raw JSON, because Claude performs better when context reads like a briefing
# document rather than a data dump.

def get_property_context(property_id: str) -> Optional[str]:
    """
    Returns a formatted property context string for the given property_id,
    or None if the property is not found in the data store.

    Args:
        property_id: The property identifier from the inbound webhook payload.

    Returns:
        A formatted string ready for injection into a Claude system prompt,
        or None if the property_id is unrecognised.
    """

    property_data = PROPERTIES.get(property_id)

    # If the property_id is unknown, return None so the caller can handle
    # the error gracefully rather than silently sending Claude bad context.
    if not property_data:
        return None

    p = property_data  # shorthand for readability below

    # Build the context block as a structured briefing document.
    # Each section maps to a category of questions guests typically ask.
    context = f"""
PROPERTY BRIEFING — {p['name']}
════════════════════════════════════════

LOCATION & OVERVIEW
Property: {p['name']}, {p['location']}
Bedrooms: {p['bedrooms']} | Maximum Guests: {p['max_guests']}
Private Pool: {'Yes' if p['private_pool'] else 'No'}

RATES & PRICING
Base Rate: INR {p['base_rate_inr']:,} per night (covers up to {p['base_rate_covers_guests']} guests)
Extra Guest Charge: INR {p['extra_guest_charge_inr']:,} per person per night
Example: 2 adults for 4 nights = INR {p['base_rate_inr']:,} × 4 = INR {p['base_rate_inr'] * 4:,} total

CHECK-IN & CHECK-OUT
Check-in: {p['check_in_time']} | Check-out: {p['check_out_time']}
Early check-in and late check-out are subject to availability — guests should request in advance.

AVAILABILITY
April 20–24: Available and ready for booking.

WIFI & CONNECTIVITY
WiFi Password: {p['wifi_password']}

STAFF & SERVICES
Caretaker: Available {p['caretaker_hours']} — reachable for any on-site assistance.
Chef on Call: {'Yes — must be pre-booked at least one day in advance.' if p['chef_on_call'] else 'Not available.'}

AMENITIES
{chr(10).join(f'  • {amenity}' for amenity in p['amenities'])}

POLICIES
Cancellation: {p['cancellation_policy']}
Pets: {p['pet_policy']}
Parking: {p['parking']}

════════════════════════════════════════
Use only the facts above when answering guest questions.
Do not guess or invent any information not listed here.
If a guest asks about something not covered above, acknowledge it warmly
and let them know the team will follow up shortly.
════════════════════════════════════════
""".strip()

    return context


# ── RATE CALCULATOR ───────────────────────────────────────────────────────────
# A simple helper that computes the exact total cost for a stay.
# Used by the Claude client to inject precise pricing into the prompt,
# so Claude gives accurate numbers rather than approximating.

def calculate_stay_cost(property_id: str, nights: int, guests: int) -> Optional[dict]:
    """
    Calculates the total cost for a given stay duration and guest count.

    Args:
        property_id: The property identifier.
        nights:      Number of nights.
        guests:      Total number of guests.

    Returns:
        A dictionary with base_cost, extra_guest_cost, and total_cost in INR,
        or None if the property is not found.
    """

    property_data = PROPERTIES.get(property_id)
    if not property_data:
        return None

    p = property_data
    base_cost = p["base_rate_inr"] * nights

    # Extra guest charges only apply for guests beyond the base coverage limit
    extra_guests = max(0, guests - p["base_rate_covers_guests"])
    extra_guest_cost = extra_guests * p["extra_guest_charge_inr"] * nights

    return {
        "base_cost": base_cost,
        "extra_guest_cost": extra_guest_cost,
        "total_cost": base_cost + extra_guest_cost,
        "nights": nights,
        "guests": guests,
        "currency": "INR",
    }
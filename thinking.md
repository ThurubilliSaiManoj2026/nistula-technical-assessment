# Part 3 — Thinking Questions

## Question A — The Immediate Response

**The AI message sent at 3am:**

> "Vikram, I'm truly sorry — no hot water with guests arriving in a few
> hours is completely unacceptable, and I understand how stressful this
> moment is. Our team has been alerted right now and someone will reach
> you within the next 15 minutes to resolve this tonight. We are on this."

**Why this wording:** The reply opens with the guest's name and a
specific, sincere apology — not a generic acknowledgement. It names the
exact pressure the guest is under (guests in 4 hours) to show the system
genuinely understood the message, not just its category. It commits to a
concrete 15-minute window so the guest has a clear expectation to hold
onto. It deliberately avoids any mention of refunds — that is a human
decision, not an automated promise made at 3am.

---

## Question B — The System Design

Beyond sending the message, the platform executes this full response chain:

The inbound message is classified as complaint with action set to
escalate. Immediately, a row is written to the messages table with
direction='inbound', query_type='complaint', ai_confidence_score=0.0,
recommended_action='escalate', and the full raw payload stored in the
raw_inbound_payload JSONB column. The conversations table status column
is updated to 'escalated' and last_message_at is stamped. An urgent
WhatsApp and SMS alert fires to the caretaker on duty with the guest's
exact message and booking reference embedded — no time lost on
context-gathering. A simultaneous push notification reaches the property
manager's dashboard, pinning the conversation to the top of the
escalation queue.

A 30-minute response timer starts. If no human marks the incident as
acknowledged within that window, the platform escalates to the property
owner directly via the AI Voice Agent and sends the guest a proactive
follow-up committing to immediate human contact. Every action in this
chain is written as a timestamped outbound row in the messages table,
creating a complete SLA audit trail for post-incident review.

---

## Question C — The Learning

Three hot water complaints at Villa B1 within two months is a pattern,
not a coincidence. The system runs a nightly complaint pattern analyser
that queries the messages table for three or more rows where query_type
= 'complaint', property_id matches, and overlapping negative keywords
appear within a 60-day window. On detection, it automatically raises a
maintenance work order, adds a mandatory hot water system check to the
pre-arrival checklist for every future Villa B1 booking, and notifies
the property manager with the full complaint history as evidence.

What I would build to prevent the fourth complaint is a Pre-Stay Health
Protocol — an automated checklist running 24 hours before every
check-in, requiring physical caretaker sign-off on critical systems
including hot water, air conditioning, and power before the guest
arrives. Coupled with a Predictive Maintenance Scheduler that surfaces
properties ranked by complaint frequency from the messages table, this
moves the team from reactive fire-fighting to proactive quality control.
The fourth complaint never happens because the third triggered a boiler
inspection, not just a repair.
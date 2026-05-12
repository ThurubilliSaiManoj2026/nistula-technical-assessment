#Thinking Questions

## Question A — The Immediate Response

**The AI message sent at 3am:**

> "Vikram, I'm truly sorry — no hot water with guests arriving in a few hours
> is completely unacceptable, and I understand how stressful this moment is.
> Our team has been alerted right now and someone will reach you within the
> next 15 minutes to resolve this tonight. We are on this."

**Why this wording:** The reply opens with the guest's name and a specific,
sincere apology — not a generic "we're sorry for the inconvenience." It
acknowledges the exact pressure the guest is under (guests arriving in 4
hours) to show the AI genuinely understood the message, not just its
category. It commits to a concrete 15-minute response window, which gives
the guest a clear expectation to hold onto. It deliberately avoids any
mention of refunds — that is a human decision made with full context, not
an automated promise made at 3am.

---

## Question B — The System Design

Beyond sending the message, the platform executes this full response chain:

The inbound message is classified as a complaint with action set to
`escalate`. The system immediately pushes an urgent WhatsApp and SMS alert
to the caretaker on duty, embedding the guest's exact message, booking
reference, and villa name so no time is lost on context-gathering. A
simultaneous push notification fires to the property manager's dashboard,
marking the conversation as escalated and pinning it to the top of the
queue. The full incident is logged to the messages table with timestamp,
confidence score, query type, and raw payload — creating a complete audit
trail.

A 30-minute response timer starts. If no human marks the incident as
acknowledged within that window, the platform escalates to the property
owner directly via phone call using the AI Voice Agent, and sends the guest
a proactive follow-up message acknowledging the delay and committing to
immediate human contact. Every action in this chain is timestamped for SLA
tracking and post-incident review.

---

## Question C — The Learning

Three complaints about hot water at Villa B1 within two months is a pattern,
not a coincidence. The system should run a nightly complaint pattern
analyser that queries the messages table for three or more complaints
sharing the same property and overlapping keywords within a 60-day window.
On detection, it automatically raises a maintenance work order, adds a
mandatory hot water system check to the pre-arrival checklist for every
future booking at Villa B1, and notifies the property manager with the full
complaint history as evidence.

What I would build to prevent the fourth complaint is a Pre-Stay Health
Protocol — an automated checklist that runs 24 hours before every check-in,
requiring physical sign-off from the caretaker on critical systems including
hot water, air conditioning, and power. Coupled with a Predictive
Maintenance Scheduler that surfaces properties by complaint frequency, this
moves the team from reactive fire-fighting to proactive quality control. The
fourth complaint never happens because the third one triggered a boiler
inspection, not just a repair.